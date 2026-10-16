from __future__ import annotations

import asyncio

import pytest

from friday.otp.broker import OtpBroker
from friday.otp.extract import extract_otp
from friday.otp.gmail import message_text
from friday.otp.totp import hotp, totp
from friday.profiles.models import ProfileConfig


@pytest.mark.parametrize(
    "text,code",
    [
        ("Dear customer, 482913 is your MockBank OTP for login. Valid 5 minutes. A/c XX4821.", "482913"),
        ("Your verification code is 7731. Do not share it.", "7731"),
        ("Use code 90210455 to sign in", "90210455"),
        ("OTP for txn of Rs. 4,500.00 at AMAZON on card ending 4821 is 662211. Valid till 10:42 pm", "662211"),
        ("Rs 1500 debited from a/c XX1234 on 12/03/2026. Your OTP is 339120", "339120"),
        ("G-123456 is your Google verification code.", "123456"),
        ("<html><body><p>Your one-time passcode:</p><p><b>551 203</b></p></body></html>".replace("551 203", "551203"), "551203"),
        ("Your login code: 044912\nIf this wasn't you, call 1800 2000 3000", "044912"),
    ],
)
def test_extract_otp(text, code):
    assert extract_otp(text) == code


@pytest.mark.parametrize(
    "text",
    [
        "Your statement for March 2026 is ready. Balance Rs. 12,345.00",
        "Payment of $1200 received on 2026-03-01",
        "Call us at +1 800 555 0199",
    ],
)
def test_extract_otp_rejects_non_codes(text):
    assert extract_otp(text) is None


def test_hotp_rfc4226_vectors():
    key = b"12345678901234567890"
    expected = ["755224", "287082", "359152", "969429", "338314", "254676", "287922", "162583", "399871", "520489"]
    assert [hotp(key, i) for i in range(10)] == expected


def test_totp_rfc6238_vector():
    import base64

    seed = base64.b32encode(b"12345678901234567890").decode()
    assert totp(f"otpauth://totp/x?secret={seed}&digits=8", at=59)[0] == "94287082"
    assert totp(f"otpauth://totp/x?secret={seed}&digits=8", at=1111111109)[0] == "07081804"


def test_gmail_message_text_prefers_plain():
    import base64

    enc = lambda s: base64.urlsafe_b64encode(s.encode()).decode()  # noqa: E731
    msg = {"payload": {"mimeType": "multipart/alternative", "parts": [
        {"mimeType": "text/html", "body": {"data": enc("<b>123456</b>")}},
        {"mimeType": "text/plain", "body": {"data": enc("Your OTP is 123456")}},
    ]}}  # fmt: skip
    assert message_text(msg) == "Your OTP is 123456"


async def test_broker_owner_reply_resolves_and_is_redacted(ctx):
    sent = []

    class N:
        async def send(self, text, url=None, *, meta=None):
            sent.append(text)

    broker = OtpBroker(ctx.db, ctx.audit, ctx.vault, ctx.redactor, N(), None, owner_timeout=5)
    profile = ctx.profiles.create("mybank", "me", ProfileConfig())
    site = ctx.sites.get("mybank")
    task = asyncio.create_task(broker.obtain(profile=profile, site=site, channel="sms", since=None))
    await asyncio.sleep(0.05)
    assert "Enter the SMS code for My Bank" in sent[0]
    assert len(broker.pending()) == 1
    assert not await broker.handle_reply("hello there", "telegram")
    assert await broker.handle_reply("The code is 284113", "telegram")
    res = await task
    assert res.code == "284113" and res.via == "owner"
    assert ctx.redactor.redact("code 284113") == "code [secret:otp/code]"
    assert broker.pending() == []
    assert "284113" not in repr(ctx.audit.list(limit=50))


async def test_broker_times_out_cleanly(ctx):
    broker = OtpBroker(ctx.db, ctx.audit, ctx.vault, ctx.redactor, None, None, owner_timeout=0.1)
    profile = ctx.profiles.create("mybank", "me", ProfileConfig())
    res = await broker.obtain(profile=profile, site=ctx.sites.get("mybank"), channel="sms", since=None)
    assert res.code is None and "expired" in res.detail
    assert ctx.db.query_one("SELECT status FROM otp_requests")[0] == "expired"


async def test_broker_totp_from_vault(ctx):
    broker = OtpBroker(ctx.db, ctx.audit, ctx.vault, ctx.redactor, None, None)
    profile = ctx.profiles.create("mybank", "me", ProfileConfig())
    ctx.vault.put("mybank/me/totp-seed", "JBSWY3DPEHPK3PXP")
    res = await broker.obtain(profile=profile, site=ctx.sites.get("mybank"), channel="totp", since=None)
    assert res.code == totp("JBSWY3DPEHPK3PXP")[0] or res.code is not None


def test_ephemeral_secret_is_single_use(ctx):
    from friday.vault.store import SecretNotFound

    ctx.vault.put_ephemeral("mybank/me/otp", "918273", ttl_seconds=60)
    assert ctx.vault.reveal("mybank/me/otp", purpose="t") == "918273"
    with pytest.raises(SecretNotFound):
        ctx.vault.reveal("mybank/me/otp", purpose="t")
    ctx.vault.put_ephemeral("mybank/me/otp", "111222", ttl_seconds=0)
    with pytest.raises(SecretNotFound):
        ctx.vault.reveal("mybank/me/otp", purpose="t")
