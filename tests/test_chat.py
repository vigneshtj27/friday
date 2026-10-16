from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from friday.chat import mask_codes
from friday.profiles.models import ProfileConfig
from friday.web.app import create_app

from .test_handoff_web import login


def test_mask_codes():
    assert mask_codes("it's 284 113 thanks") == "it's •••••• thanks"
    assert mask_codes("fetch my march statement") == "fetch my march statement"


async def test_code_request_lands_in_chat_and_reply_resolves_it(ctx):
    await ctx.notifier.start(ctx.broker.handle_reply)
    profile = ctx.profiles.create("mybank", "me", ProfileConfig())
    task = asyncio.create_task(ctx.broker.obtain(profile=profile, site=ctx.sites.get("mybank"), channel="sms", since=None))
    await asyncio.sleep(0.05)
    card = ctx.chat.store.list()[-1]
    assert card["sender"] == "friday" and card["kind"] == "otp" and card["profile_id"] == profile.id
    assert card["ref"] == ctx.broker.pending()[0].id

    await ctx.chat.owner_says("The code is 284113")
    assert (await task).code == "284113"
    rows = ctx.chat.store.list()
    assert rows[-2]["sender"] == "owner" and "284113" not in rows[-2]["text"]
    assert "went to the sign-in" in rows[-1]["text"]
    assert "284113" not in json.dumps([dict(r) for r in ctx.db.query("SELECT * FROM chat_messages")])


async def test_stray_code_is_not_stored_and_status_reports_pending(ctx):
    await ctx.notifier.start(ctx.broker.handle_reply)
    await ctx.chat.owner_says("771904")
    rows = ctx.chat.store.list()
    assert "771904" not in rows[0]["text"] and "discarded" in rows[1]["text"]
    await ctx.chat.owner_says("status", lambda: "Nothing is waiting on you.")
    assert ctx.chat.store.list()[-1]["text"] == "Nothing is waiting on you."


@pytest.fixture
def client(ctx):
    with TestClient(create_app(ctx), base_url="http://testserver") as c:
        yield c


def test_chat_requires_login(client):
    assert client.get("/api/chat").status_code == 401
    r = client.get("/chat", follow_redirects=False)
    assert r.status_code in (302, 307) and r.headers["location"].startswith("/login")


def test_chat_page_and_conversation(client):
    login(client)
    assert client.get("/", follow_redirects=False).headers["location"] == "/chat"
    assert "friday" in client.get("/chat").text
    assert client.get("/api/chat").json()["messages"] == []

    assert client.post("/api/chat", json={"text": "status"}).status_code == 200
    msgs = client.get("/api/chat").json()["messages"]
    assert [m["sender"] for m in msgs] == ["owner", "friday"]
    assert "Nothing is waiting on you" in msgs[1]["text"]

    after = msgs[-1]["id"]
    assert client.get(f"/api/chat?after={after}").json()["messages"] == []
    assert client.post("/api/chat", json={"text": "x"}, headers={"Origin": "https://evil.example"}).status_code == 403


def test_cards_carry_current_state(client, ctx):
    login(client)
    ctx.db.execute(
        "INSERT INTO approvals (id, run_id, profile_id, action, details_json, status, created_at, expires_at)"
        " VALUES ('ap1', NULL, 'mybank.me', 'Pay bill of 1,200', ?, 'approved', '2026-01-01T00:00:00+00:00', '2026-01-01T00:10:00+00:00')",
        (json.dumps({"url": "https://mybank.example/pay"}),),
    )
    ctx.chat.store.add("friday", "Approval needed", kind="approval", ref="ap1", profile_id="mybank.me")
    data = client.get("/api/chat").json()
    assert data["cards"]["ap1"]["status"] == "approved"
    assert data["cards"]["ap1"]["action"] == "Pay bill of 1,200"


def test_long_poll_returns_on_new_message(client, ctx):
    login(client)
    first = client.get("/api/chat").json()
    ctx.chat.store.add("friday", "hello")
    data = client.get(f"/api/chat?after={first['last_id']}&wait=5&sig={first['sig']}").json()
    assert [m["text"] for m in data["messages"]] == ["hello"]


def test_clear(client, ctx):
    login(client)
    ctx.chat.store.add("friday", "hello")
    assert client.delete("/api/chat").status_code == 204
    assert client.get("/api/chat").json()["messages"] == []
