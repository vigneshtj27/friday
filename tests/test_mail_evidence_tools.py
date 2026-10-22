from __future__ import annotations

import asyncio

import pytest

from friday.profiles.models import Site
from friday.tools.base import ApprovalDenied, Call
from friday.tools.google_tools import GmailScanExpenses, SheetsAddEvidence

from .fake_google import SID, FakeGoogle, install

EXPENSES = [["Date", "Description", "Amount", "Status"]]

SAFEKEY = {"id": "sk1", "threadId": "t1", "from": "AmericanExpress@welcome.americanexpress.com",
           "subject": "Your American Express SafeKey verification code", "date": "Tue, 20 Oct 2026",
           "body": "Your one-time verification code for your purchase of INR 4,599.00 at AMAZON on 20 Oct 2026 is 582913."}  # fmt: skip
ALERT = {"id": "bd1", "threadId": "t2", "from": "alerts@hdfcbank.net", "subject": "Transaction Alert",
         "date": "Mon, 19 Oct 2026", "body": "Rs.1,234.00 was spent on your HDFC Bank Card ending 1234 at FLIPKART on 19-10-2026."}  # fmt: skip
# from a configured sender, but no amount -> matched by the query, parses to nothing (unparsed)
MARKETING = {"id": "mk1", "threadId": "t3", "from": "AmericanExpress@welcome.americanexpress.com",
             "subject": "Your Membership Rewards summary", "date": "Mon, 19 Oct 2026",
             "body": "You have 5,000 Membership Rewards points waiting. No purchase needed. Explore offers."}


@pytest.fixture
def g(ctx):
    ctx.sites.upsert(Site(id="amex", name="American Express", login_url="https://americanexpress.com/",
                          domains=["americanexpress.com"], otp_senders=["AmericanExpress@welcome.americanexpress.com"]))  # fmt: skip
    ctx.profiles.create("amex", "me")
    fake = FakeGoogle(sheets={SID: {"Transactions": [list(r) for r in EXPENSES]}}, titles={SID: "Expenses 2026"},
                      mail=[SAFEKEY, ALERT, MARKETING])  # fmt: skip
    install(ctx, fake)
    ctx.prefs.set("expense_sheet", {"spreadsheet": SID, "tab": "Transactions", "include_credits": False, "column_map": None})
    return ctx, fake


def call_for(ctx, text="get my amex expenses from email"):
    return Call(ctx=ctx, owner_text=text, plan_id="p1")


async def approve_next(ctx, yes):
    for _ in range(200):
        if ctx.gate.pending():
            ctx.gate.decide(ctx.gate.pending()[0].id, yes)
            return
        await asyncio.sleep(0.01)
    raise AssertionError("no approval card appeared")


def assert_no_mail_writes(fake):
    assert fake.sent == [] and fake.drafts == {}
    assert not any(m.lower() == "post" and "/drafts" in p for m, p in fake.calls)
    assert not any("/send" in p for _, p in fake.calls)


async def test_scan_with_readonly_grant(g):
    ctx, fake = g
    out = await ctx.tools.call("gmail_scan_expenses", {"account": "amex.me", "start": "2026-10-01", "end": "2026-10-31"}, call_for(ctx))
    assert out["found"] == 2 and out["new"] == 2 and out["unparsed"] == 1  # SafeKey + alert, marketing skipped
    kinds = {r["kind"] for r in out["rows"]}
    assert kinds == {"otp", "alert"}
    assert out["account"] == "amex.me" and "unverified" in out["note"]
    # stored as email-status rows
    rows = ctx.transactions.list(status="email")
    assert len(rows) == 2 and all(r["source"] == "gmail:amex" and r["evidence_ref"] for r in rows)
    assert_no_mail_writes(fake)


async def test_scan_requires_gmail_grant(g):
    ctx, fake = g
    ctx.google.can = lambda cap: False
    with pytest.raises(Exception) as e:
        await ctx.tools.call("gmail_scan_expenses", {"account": "amex.me", "start": "2026-10-01", "end": "2026-10-31"}, call_for(ctx))
    assert "google_connect" in str(e.value) and "gmail" in str(e.value)
    assert ctx.transactions.list(status="email") == []


async def test_add_evidence_always_needs_approval_and_marks_pending(g):
    ctx, fake = g
    await ctx.tools.call("gmail_scan_expenses", {"account": "amex.me", "start": "2026-10-01", "end": "2026-10-31"}, call_for(ctx))

    # denial: nothing written
    task = asyncio.create_task(ctx.tools.call("sheets_add_evidence", {"account": "amex.me", "start": "2026-10-01", "end": "2026-10-31"}, call_for(ctx)))
    await approve_next(ctx, False)
    with pytest.raises(ApprovalDenied):
        await task
    assert len(ctx.sheets_rows(SID, "Transactions") if hasattr(ctx, "sheets_rows") else fake.sheets[SID]["Transactions"]) == 1  # header only

    # approval: rows land, each PENDING (Status column)
    task = asyncio.create_task(ctx.tools.call("sheets_add_evidence", {"account": "amex.me", "start": "2026-10-01", "end": "2026-10-31"}, call_for(ctx)))
    await approve_next(ctx, True)
    v = await task
    assert v["ok"] and v["appended"] == 2  # both SafeKey and alert are debit charges
    body = fake.sheets[SID]["Transactions"]
    assert any("PENDING" in str(c) for row in body[1:] for c in row)
    assert any("4,599" in str(c) or "4599" in str(c) for row in body[1:] for c in row)

    # a second run adds nothing
    task = asyncio.create_task(ctx.tools.call("sheets_add_evidence", {"account": "amex.me", "start": "2026-10-01", "end": "2026-10-31"}, call_for(ctx)))
    await approve_next(ctx, True)
    v2 = await task
    assert v2["appended"] == 0
    assert_no_mail_writes(fake)


async def test_add_evidence_suffix_when_no_status_column(ctx):
    no_status = [["Date", "Description", "Amount"]]
    ctx.sites.upsert(Site(id="amex", name="Amex", login_url="https://americanexpress.com/", domains=["americanexpress.com"]))
    ctx.profiles.create("amex", "me")
    fake = FakeGoogle(sheets={SID: {"T": [list(r) for r in no_status]}}, titles={SID: "E"}, mail=[SAFEKEY])
    install(ctx, fake)
    ctx.prefs.set("expense_sheet", {"spreadsheet": SID, "tab": "T", "include_credits": False, "column_map": None})
    await ctx.tools.call("gmail_scan_expenses", {"account": "amex.me", "start": "2026-10-01", "end": "2026-10-31"}, call_for(ctx))
    task = asyncio.create_task(ctx.tools.call("sheets_add_evidence", {"account": "amex.me", "start": "2026-10-01", "end": "2026-10-31"}, call_for(ctx)))
    await approve_next(ctx, True)
    v = await task
    assert v["ok"]
    body = fake.sheets[SID]["T"]
    assert any("(PENDING)" in str(c) for row in body[1:] for c in row)
