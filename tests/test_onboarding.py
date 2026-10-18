"""Adding an account from the chat: site discovery, the secure setup card, resume."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from friday.assistant import ChatAssistant
from friday.onboarding import SetupError, validate_proposal
from friday.web.app import create_app

from .conftest import TEST_PASSWORD
from .test_assistant import ScriptedLLM, settle

AMEX = {
    "found": True, "name": "American Express India", "site_id": "amex",
    "login_url": "https://www.americanexpress.com/en-in/account/login", "domains": ["americanexpress.com"],
    "auth_domains": [], "otp_senders": ["americanexpress@welcome.americanexpress.com"], "reason": "official site",
}  # fmt: skip
SECRET_PW = "Amex-Pa55word-xyz"


# ---- proposal checks ---------------------------------------------------------------


def test_valid_proposal_is_normalized():
    p = validate_proposal(AMEX, existing_ids=set())
    assert p.site_id == "amex" and p.domains == ["americanexpress.com"]
    assert p.site().login_url.startswith("https://www.americanexpress.com/")


def test_existing_id_gets_a_suffix():
    assert validate_proposal(AMEX, existing_ids={"amex"}).site_id.startswith("amex-")


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"found": False, "reason": "unclear which provider"}, "unclear"),
        ({"login_url": "http://www.americanexpress.com/login"}, "https"),
        ({"login_url": "https://203.0.113.9/login"}, "IP"),
        ({"login_url": "https://americanexpress-login.example.net/x", "domains": ["americanexpress.com"]}, "not on the site's own domains"),
    ],
)
def test_bad_proposals_rejected(change, reason):
    with pytest.raises(SetupError, match=reason):
        validate_proposal({**AMEX, **change}, existing_ids=set())


def test_login_host_domain_is_added_when_missing():
    p = validate_proposal({**AMEX, "domains": []}, existing_ids=set())
    assert p.domains == ["americanexpress.com"]


# ---- the setup card end to end through the API --------------------------------------------


@pytest.fixture
def web(ctx, monkeypatch):
    submitted = []

    def submit(kind, profile_id, params=None, *, force=False):
        submitted.append((kind, profile_id, params))
        return ctx.runs.create(kind, profile_id, params or {})

    monkeypatch.setattr(ctx.jobs, "submit", submit)
    with TestClient(create_app(ctx), base_url="http://testserver") as c:
        r = c.post("/login", data={"password": TEST_PASSWORD}, follow_redirects=False)
        assert r.status_code == 303
        yield ctx, c, submitted


async def _new_amex_card(ctx, resume=None):
    p = validate_proposal(AMEX, existing_ids=set())
    return await ctx.onboarding.request(site=p.site(), site_is_new=True, account="me", fields=None,
                                        purpose="to fetch your October expenses", resume=resume)  # fmt: skip


async def test_setup_card_saves_to_vault_and_resumes_the_job(web):
    ctx, client, submitted = web
    sid = await _new_amex_card(ctx, {"kind": "fetch_statement", "params": {"start": "2026-10-01", "end": "2026-10-18"}})
    assert ctx.sites.get("amex") is None, "nothing is saved before the owner confirms"
    poll = client.get("/api/chat").json()
    card = poll["cards"][sid]
    assert card["status"] == "pending" and card["new_site"] and card["site"]["login_url"].startswith("https://")
    assert [f["name"] for f in card["fields"]] == ["username", "password", "totp_seed"]

    r = client.post(f"/api/setup/{sid}", json={"values": {"username": "vignesh.r", "password": SECRET_PW}},
                    headers={"Origin": "http://testserver"})  # fmt: skip
    assert r.status_code == 200, r.text
    assert SECRET_PW not in r.text
    assert ctx.sites.get("amex").domains == ["americanexpress.com"]
    assert ctx.profiles.get("amex.me")
    assert ctx.vault.reveal("amex/me/password", purpose="test") == SECRET_PW
    assert submitted == [("fetch_statement", "amex.me", {"start": "2026-10-01", "end": "2026-10-18", "currency": None})]
    assert client.get("/api/chat").json()["cards"][sid]["status"] == "done"
    # The secret appears nowhere outside the vault.
    everywhere = json.dumps(client.get("/api/chat").json()) + repr(ctx.audit.list(limit=500)) + json.dumps(
        [dict(r) for r in ctx.db.query("SELECT * FROM setup_requests")]
    )
    assert SECRET_PW not in everywhere and "vignesh.r" not in everywhere
    # A submitted card can't be reused.
    again = client.post(f"/api/setup/{sid}", json={"values": {"username": "x", "password": "y"}}, headers={"Origin": "http://testserver"})
    assert again.status_code == 400


async def test_missing_password_is_rejected_without_echoing_values(web):
    ctx, client, submitted = web
    sid = await _new_amex_card(ctx)
    r = client.post(f"/api/setup/{sid}", json={"values": {"username": "vignesh.r"}}, headers={"Origin": "http://testserver"})
    assert r.status_code == 400 and "Password is required" in r.text and "vignesh.r" not in r.text
    bad = client.post(f"/api/setup/{sid}", json={"values": {"password": 12345678}}, headers={"Origin": "http://testserver"})
    assert bad.status_code == 400 and "12345678" not in bad.text
    assert submitted == [] and not ctx.vault.list()


async def test_cross_origin_submit_refused(web):
    ctx, client, _ = web
    sid = await _new_amex_card(ctx)
    r = client.post(f"/api/setup/{sid}", json={"values": {"username": "a", "password": "b"}}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403 and not ctx.vault.list()


async def test_cancel_and_bad_totp(web):
    ctx, client, _ = web
    sid = await _new_amex_card(ctx)
    r = client.post(f"/api/setup/{sid}", json={"values": {"username": "u", "password": "p", "totp_seed": "not base32!"}},
                    headers={"Origin": "http://testserver"})  # fmt: skip
    assert r.status_code == 400 and "authenticator" in r.text and not ctx.vault.list()
    assert client.post(f"/api/setup/{sid}/cancel", headers={"Origin": "http://testserver"}).status_code == 200
    assert client.get("/api/chat").json()["cards"][sid]["status"] == "cancelled"


async def test_known_site_only_asks_for_missing_credentials(web):
    ctx, client, submitted = web
    from friday.profiles.models import ProfileConfig

    ctx.profiles.create("mybank", "me", ProfileConfig())
    ctx.vault.put("mybank/me/username", "known-user")
    sid = await ctx.onboarding.request(site=ctx.sites.get("mybank"), site_is_new=False, account="me", fields=["password"], purpose="", resume=None)
    card = client.get("/api/chat").json()["cards"][sid]
    assert [f["name"] for f in card["fields"]] == ["password"]
    r = client.post(f"/api/setup/{sid}", json={"values": {"password": "new-pass-123"}}, headers={"Origin": "http://testserver"})
    assert r.status_code == 200
    assert submitted == [("login", "mybank.me", {})]  # no job given: sign in to check them


# ---- the assistant drives it from a plain request ---------------------------------------------


async def test_assistant_finds_site_and_posts_card_instead_of_sending_owner_away(ctx, monkeypatch):
    import friday.onboarding as ob

    async def no_network(p, **kw):
        return ["page answered HTTP 200"]

    monkeypatch.setattr(ob, "check_reachable", no_network)

    class Finder:
        async def find(self, query, *, region_hint):
            assert "amex" in query
            return dict(AMEX)

    llm = ScriptedLLM([
        ("find_site", {"provider": "amex"}),
        ("request_setup", {"proposal_id": "p1", "purpose": "to get your October expenses",
                           "then": {"kind": "fetch_statement", "start": "2026-10-01", "end": "2026-10-18"}}),
        "I found American Express India. Fill in the card above and I'll fetch October's statement.",
    ])  # fmt: skip
    ctx.chat.assistant = ChatAssistant(ctx, lambda: llm)
    ctx.chat.assistant.finder = Finder()
    await ctx.chat.owner_says("get my amex expenses for this month")
    await settle(ctx)
    msgs = ctx.chat.store.list()
    setup = [m for m in msgs if m["kind"] == "setup"]
    assert len(setup) == 1 and setup[0]["profile_id"] == "amex.me"
    row = ctx.onboarding.get(setup[0]["ref"])
    assert row["resume"] == {"kind": "fetch_statement", "params": {"start": "2026-10-01", "end": "2026-10-18", "currency": None}}
    tool_result = json.dumps(llm.calls[1][-1]["content"])
    assert "untrusted until the owner confirms" in tool_result
    assert "Sites tab" not in msgs[-1]["text"]


async def test_start_run_without_credentials_points_to_setup(ctx, monkeypatch):
    from friday.profiles.models import ProfileConfig

    ctx.profiles.create("mybank", "me", ProfileConfig())
    llm = ScriptedLLM([("start_run", {"account": "mybank.me", "kind": "login"}), "ok"])
    ctx.chat.assistant = ChatAssistant(ctx, lambda: llm)
    await ctx.chat.owner_says("sign in to mybank")
    await settle(ctx)
    err = json.dumps(llm.calls[1][-1]["content"])
    assert "request_setup" in err and "no saved username or password" in err


def test_site_checks_on_save(ctx):
    from friday.profiles.models import Site, site_problems

    bad = Site(id="amexbad", name="amex", login_url="https://www.americanexpress.com/en-in/account/login",
               domains=["americanexpress", "vigneshtj27"])  # fmt: skip
    problems = " ".join(site_problems(bad))
    assert "'americanexpress' is not a domain name" in problems and "not on the site's domains" in problems
    with pytest.raises(ValueError):
        ctx.sites.upsert(bad)
    assert not site_problems(Site(id="m", name="m", login_url="http://localhost:8000/x", domains=["localhost"]))


async def test_find_site_repairs_a_broken_site(ctx, monkeypatch):
    import friday.onboarding as ob
    from friday.profiles.models import Site

    # A broken record from before the checks existed.
    ctx.db.execute(
        "INSERT INTO sites (id, name, login_url, domains_json, otp_domains_json, otp_senders_json, created_at) VALUES (?, ?, ?, ?, '[]', '[]', 'x')",
        ("amex", "amex", "https://www.americanexpress.com/en-in/account/login", json.dumps(["americanexpress", "vigneshtj27"])),
    )

    async def no_network(p, **kw):
        return []

    monkeypatch.setattr(ob, "check_reachable", no_network)

    class Finder:
        async def find(self, query, *, region_hint):
            return dict(AMEX)

    llm = ScriptedLLM([("find_site", {"provider": "amex"}), "ok"])
    ctx.chat.assistant = ChatAssistant(ctx, lambda: llm)
    ctx.chat.assistant.finder = Finder()
    await ctx.chat.owner_says("fix amex")
    await settle(ctx)
    result = json.loads(llm.calls[1][-1]["content"][0]["content"])
    assert result["site_id"] == "amex" and "repairs" in result and result["domains"] == ["americanexpress.com"]
    assert isinstance(ctx.chat.assistant._proposals["p1"].site(), Site)
