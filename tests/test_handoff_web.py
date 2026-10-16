from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from friday.handoff.manager import HandoffState
from friday.profiles.display import VncTarget
from friday.web.app import create_app

from .conftest import TEST_PASSWORD

# ---- handoff manager -------------------------------------------------------


async def test_handoff_complete_unblocks_waiter(ctx):
    h = ctx.handoffs.create("mybank.me", "seed", None)
    waiter = asyncio.create_task(ctx.handoffs.wait(h.id))
    await asyncio.sleep(0)
    assert ctx.handoffs.complete(h.id)
    assert await waiter == HandoffState.DONE
    assert not ctx.handoffs.complete(h.id)  # idempotent / closed


async def test_handoff_expires(ctx):
    h = ctx.handoffs.create("mybank.me", "seed", None, ttl_seconds=0)
    assert await ctx.handoffs.wait(h.id) == HandoffState.EXPIRED
    assert ctx.audit.list(action_prefix="handoff.expired")


def test_handoff_ids_are_unguessable(ctx):
    ids = {ctx.handoffs.create("mybank.me", "x", None).id for _ in range(50)}
    assert len(ids) == 50 and all(len(i) >= 24 for i in ids)


# ---- web auth --------------------------------------------------------------


@pytest.fixture
def client(ctx):
    with TestClient(create_app(ctx), base_url="http://testserver") as c:
        yield c


def login(client):
    r = client.post("/login", data={"password": TEST_PASSWORD}, follow_redirects=False)
    assert r.status_code == 303 and "friday_session" in r.cookies
    return r


def test_api_requires_login(client):
    assert client.get("/api/profiles").status_code == 401
    assert client.get("/api/secrets").status_code == 401
    assert client.get("/", follow_redirects=False).status_code == 307


def test_bad_password_rejected(client, ctx):
    r = client.post("/login", data={"password": "wrong"}, follow_redirects=False)
    assert r.headers["location"].startswith("/login?error")
    assert "friday_session" not in r.cookies
    assert ctx.audit.list(action_prefix="portal.login_failed")


def test_login_cookie_flags(client):
    r = login(client)
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie


def test_cross_origin_post_refused(client):
    login(client)
    r = client.put(
        "/api/secrets/mybank/me/password", json={"value": "x" * 8}, headers={"Origin": "https://evil.example"}
    )
    assert r.status_code == 403
    r = client.put("/api/secrets/mybank/me/password", json={"value": "x" * 8}, headers={"Origin": "http://testserver"})
    assert r.status_code == 204


def test_secret_api_never_returns_values(client):
    login(client)
    client.put("/api/secrets/mybank/me/password", json={"value": "Api-Secret-77", "kind": "password"})
    body = client.get("/api/secrets").text
    assert "mybank/me/password" in body and "Api-Secret-77" not in body
    assert "Api-Secret-77" not in client.get("/api/audit").text


def test_security_headers(client):
    r = client.get("/login")
    assert r.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]


def test_handoff_page_requires_login_and_done_works(client, ctx):
    h = ctx.handoffs.create("mybank.me", "Log in manually", None)
    r = client.get(f"/handoff/{h.id}", follow_redirects=False)
    assert r.status_code == 307 and "/login" in r.headers["location"]
    assert client.post(f"/handoff/{h.id}/done").status_code == 401
    login(client)
    assert "Done" in client.get(f"/handoff/{h.id}").text
    assert client.post(f"/handoff/{h.id}/done", follow_redirects=False).status_code == 303
    assert h.state == HandoffState.DONE
    assert "Handoff closed" in client.get(f"/handoff/{h.id}").text


def test_revoke_endpoint_wipes_profile(client, ctx):
    login(client)
    p = ctx.profiles.create("mybank", "me")
    ctx.vault.put("mybank/me/password", "wipe-me-123")
    assert client.delete(f"/api/profiles/{p.id}").status_code == 200
    assert ctx.vault.list() == [] and ctx.profiles.list() == []


# ---- websocket VNC bridge --------------------------------------------------


@pytest.fixture
def echo_server():
    """A TCP echo server standing in for x11vnc, on its own loop/thread."""
    import socket
    import threading

    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen()
    port = srv.getsockname()[1]

    def serve():
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            with conn:
                while data := conn.recv(4096):
                    conn.sendall(b"echo:" + data)

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    yield port
    srv.close()


def test_vnc_websocket_requires_auth_and_relays(client, ctx, echo_server):
    h = ctx.handoffs.create("mybank.me", "seed", VncTarget("127.0.0.1", echo_server, "pw"))
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/handoff/{h.id}/vnc") as ws:
            ws.receive_bytes()
    login(client)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/handoff/{h.id}/vnc", headers={"Origin": "https://evil.example"}) as ws:
            ws.receive_bytes()
    with client.websocket_connect(
        f"/handoff/{h.id}/vnc", headers={"Origin": "http://testserver"}, subprotocols=["binary"]
    ) as ws:
        ws.send_bytes(b"RFB")
        assert ws.receive_bytes() == b"echo:RFB"
    ctx.handoffs.complete(h.id)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/handoff/{h.id}/vnc", headers={"Origin": "http://testserver"}) as ws:
            ws.receive_bytes()
