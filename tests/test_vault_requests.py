"""In-chat vault-request cards and the secrets-management tools."""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from friday.profiles.models import Site
from friday.tools.base import ApprovalDenied, Call, ToolError
from friday.vault.store import SecretKind
from friday.vault_requests import VaultRequestError
from friday.web.app import create_app

from .conftest import TEST_PASSWORD

PROXY_PW = "Sup3r-Egress-Secret-xyz"


@pytest.fixture
def web(ctx, monkeypatch):
    submitted = []

    def submit(kind, profile_id, params=None, *, force=False):
        submitted.append((kind, profile_id, params))
        return ctx.runs.create(kind, profile_id, params or {})

    monkeypatch.setattr(ctx.jobs, "submit", submit)
    with TestClient(create_app(ctx), base_url="http://testserver") as c:
        assert c.post("/login", data={"password": TEST_PASSWORD}, follow_redirects=False).status_code == 303
        yield ctx, c, submitted


def hdr():
    return {"Origin": "http://testserver"}


# ---- the card ---------------------------------------------------------------


async def test_card_saves_to_vault_under_code_chosen_names(web):
    ctx, client, _ = web
    rid = await ctx.vault_requests.request("egress_proxy", purpose="route Amex through home")
    card = client.get("/api/chat").json()["cards"][rid]
    assert card["status"] == "pending"
    names = [f["name"] for f in card["fields"]]
    assert names == ["system/proxy/username", "system/proxy/password"]  # chosen in code
    r = client.post(f"/api/vault-request/{rid}",
                    json={"values": {"system/proxy/username": "friday", "system/proxy/password": PROXY_PW}}, headers=hdr())
    assert r.status_code == 200 and PROXY_PW not in r.text
    assert ctx.vault.reveal("system/proxy/password", purpose="test") == PROXY_PW
    # the value is nowhere in chat, audit or the card; only labels/names are
    everywhere = json.dumps(client.get("/api/chat").json()) + repr(ctx.audit.list(limit=500))
    assert PROXY_PW not in everywhere
    # and the redactor masks it now that it's saved
    assert PROXY_PW not in ctx.redactor.redact(f"the password is {PROXY_PW}")
    assert client.get("/api/chat").json()["cards"][rid]["status"] == "done"


async def test_card_records_only_labels_in_audit(web):
    ctx, client, _ = web
    rid = await ctx.vault_requests.request("egress_proxy")
    client.post(f"/api/vault-request/{rid}", json={"values": {"system/proxy/username": "friday", "system/proxy/password": PROXY_PW}}, headers=hdr())
    done = [e for e in ctx.audit.list(action_prefix="vault_request.done")]
    assert done and set(done[0].details["saved"]) == {"Proxy username", "Proxy password"}
    assert PROXY_PW not in json.dumps([e.details for e in ctx.audit.list(limit=500)])


async def test_submit_after_expiry_stores_nothing(web):
    ctx, client, _ = web
    rid = await ctx.vault_requests.request("egress_proxy")
    ctx.db.execute("UPDATE vault_requests SET expires_at = '2000-01-01T00:00:00+00:00' WHERE id = ?", (rid,))
    r = client.post(f"/api/vault-request/{rid}", json={"values": {"system/proxy/password": PROXY_PW}}, headers=hdr())
    assert r.status_code == 400
    assert not ctx.vault.exists("system/proxy/password")
    assert client.get("/api/chat").json()["cards"][rid]["status"] == "expired"


async def test_cancel_stores_nothing(web):
    ctx, client, _ = web
    rid = await ctx.vault_requests.request("egress_proxy")
    assert client.post(f"/api/vault-request/{rid}/cancel", headers=hdr()).status_code == 200
    assert client.get("/api/chat").json()["cards"][rid]["status"] == "cancelled"
    assert not ctx.vault.exists("system/proxy/username")


async def test_cross_origin_submit_refused(web):
    ctx, client, _ = web
    rid = await ctx.vault_requests.request("egress_proxy")
    r = client.post(f"/api/vault-request/{rid}", json={"values": {"system/proxy/password": PROXY_PW}}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403 and not ctx.vault.exists("system/proxy/password")


async def test_missing_required_field_rejected_without_echo(web):
    ctx, client, _ = web
    rid = await ctx.vault_requests.request("egress_proxy")
    r = client.post(f"/api/vault-request/{rid}", json={"values": {"system/proxy/username": "friday"}}, headers=hdr())
    assert r.status_code == 400 and "Proxy password" in r.text
    # a non-string value is refused by the hand-parser, never echoed
    bad = client.post(f"/api/vault-request/{rid}", json={"values": {"system/proxy/password": 12345678}}, headers=hdr())
    assert bad.status_code == 400 and "12345678" not in bad.text


async def test_site_credentials_uses_the_profiles_prefix(web):
    ctx, client, _ = web
    ctx.sites.upsert(Site(id="amex", name="Amex", login_url="https://americanexpress.com/", domains=["americanexpress.com"]))
    ctx.profiles.create("amex", "me")
    rid = await ctx.vault_requests.request("site_credentials", account="amex.me")
    names = [f["name"] for f in client.get("/api/chat").json()["cards"][rid]["fields"]]
    assert names == ["amex/me/username", "amex/me/password", "amex/me/totp-seed"]
    with pytest.raises(VaultRequestError):
        await ctx.vault_requests.request("site_credentials")  # no account


async def test_resume_job_starts_on_submit(web):
    ctx, client, submitted = web
    ctx.sites.upsert(Site(id="amex", name="Amex", login_url="https://americanexpress.com/", domains=["americanexpress.com"]))
    ctx.profiles.create("amex", "me")
    rid = await ctx.vault_requests.request("site_credentials", account="amex.me",
                                           resume={"kind": "login", "profile_id": "amex.me"})
    client.post(f"/api/vault-request/{rid}", json={"values": {"amex/me/username": "u", "amex/me/password": "p"}}, headers=hdr())
    assert ("login", "amex.me", {}) in submitted


# ---- planner context (no value, labels only) --------------------------------


async def test_assistant_note_mentions_labels_not_values(web):
    ctx, client, _ = web
    from friday.assistant import ChatAssistant

    ctx.chat.assistant = ChatAssistant(ctx, lambda: None)
    rid = await ctx.vault_requests.request("egress_proxy")
    client.post(f"/api/vault-request/{rid}", json={"values": {"system/proxy/username": "friday", "system/proxy/password": PROXY_PW}}, headers=hdr())
    recap = ctx.chat.assistant._notes if hasattr(ctx.chat.assistant, "_notes") else []
    joined = " ".join(recap)
    assert "Proxy username" in joined or "saved" in joined
    assert PROXY_PW not in joined


# ---- the tools --------------------------------------------------------------


def call(ctx):
    return Call(ctx=ctx, owner_text="set up my egress proxy", plan_id="p1")


async def test_vault_request_tool_posts_card(ctx):
    out = await ctx.tools.call("vault_request", {"request_id": "egress_proxy"}, call(ctx))
    assert out["vault_card"] and ctx.vault_requests.get(out["vault_card"])["status"] == "pending"
    with pytest.raises(ToolError):
        await ctx.tools.call("vault_request", {"request_id": "nonsense"}, call(ctx))  # not in the fixed set


async def test_secrets_list_has_no_values(ctx):
    ctx.vault.put("amex/me/password", "hunter2", kind=SecretKind.PASSWORD)
    out = await ctx.tools.call("secrets_list", {}, call(ctx))
    names = {s["name"] for s in out["secrets"]}
    assert "amex/me/password" in names
    assert "hunter2" not in json.dumps(out)
    assert all("value" not in s for s in out["secrets"])


async def test_secrets_delete_requires_approval(ctx):
    ctx.vault.put("amex/me/password", "hunter2", kind=SecretKind.PASSWORD)
    task = asyncio.create_task(ctx.tools.call("secrets_delete", {"name": "amex/me/password"}, call(ctx)))
    for _ in range(200):
        if ctx.gate.pending():
            ctx.gate.decide(ctx.gate.pending()[0].id, False)
            break
        await asyncio.sleep(0.01)
    with pytest.raises(ApprovalDenied):
        await task
    assert ctx.vault.exists("amex/me/password")  # denied: still there


async def test_secrets_update_posts_card_for_existing_name(ctx):
    ctx.vault.put("amex/me/password", "hunter2", kind=SecretKind.PASSWORD)
    out = await ctx.tools.call("secrets_update", {"name": "amex/me/password"}, call(ctx))
    rid = out["vault_card"]
    card = ctx.vault_requests.card(rid)
    assert card["fields"][0]["name"] == "amex/me/password"
    with pytest.raises(ToolError):
        await ctx.tools.call("secrets_update", {"name": "system/does/not-exist"}, call(ctx))
