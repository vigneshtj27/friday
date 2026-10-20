"""The authenticated portal preflight flow: check (safe) and the deliberate one-use grant."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from friday.preflight import Preflight
from friday.web.app import create_app
from tests.conftest import TEST_PASSWORD
from tests.test_preflight import GCP, geo, make_ready


@pytest.fixture
def papp(ctx):
    # a profile that fails preflight only on proxy/egress (patchright default passes the driver check)
    make_ready(ctx, egress=GCP)  # sets a proxy; GCP egress is a datacenter, so egress fails
    ctx.profiles.update_config("mybank.me", ctx.profiles.get("mybank.me").config.model_copy(update={"proxy": None}))
    ctx.preflight = Preflight(ctx, fetch_geo=geo(GCP))
    with TestClient(create_app(ctx), base_url="http://testserver") as c:
        yield ctx, c


def login(c):
    assert c.post("/login", data={"password": TEST_PASSWORD}, follow_redirects=False).status_code == 303


def check(c, pid="mybank.me"):
    return c.post(f"/api/profiles/{pid}/preflight")


def test_check_is_authenticated_and_safe(papp):
    ctx, c = papp
    assert c.post("/api/profiles/mybank.me/preflight").status_code == 401  # no login
    login(c)
    before = len(ctx.audit.list(action_prefix="preflight.override_granted"))
    r = check(c)
    assert r.status_code == 200
    j = r.json()
    assert j["effective_driver"] == "patchright"
    assert {i["name"] for i in j["report"]["items"] if not i["ok"]} >= {"proxy", "egress"}
    assert "driver" not in {i["name"] for i in j["report"]["items"] if not i["ok"]}
    assert j["confirm_token"] and j["override"] is None
    # a check never grants, never opens a run
    assert len(ctx.audit.list(action_prefix="preflight.override_granted")) == before
    assert ctx.preflight.override_status("mybank.me") is None


def test_unknown_profile_is_404(papp):
    ctx, c = papp
    login(c)
    assert c.post("/api/profiles/ghost.me/preflight").status_code == 404
    assert c.post("/api/profiles/ghost.me/preflight/grant", json={"confirm_token": "x", "confirm_profile_id": "ghost.me"}).status_code == 404


def test_grant_requires_matching_typed_id_and_live_token(papp):
    ctx, c = papp
    login(c)
    tok = check(c).json()["confirm_token"]
    # wrong typed id -> 400, no grant
    assert c.post("/api/profiles/mybank.me/preflight/grant", json={"confirm_token": tok, "confirm_profile_id": "other.me"}).status_code == 400
    # bad token -> 409 stale
    assert c.post("/api/profiles/mybank.me/preflight/grant", json={"confirm_token": "nope", "confirm_profile_id": "mybank.me"}).status_code == 409
    assert ctx.preflight.override_status("mybank.me") is None
    # correct confirmation -> one grant
    r = c.post("/api/profiles/mybank.me/preflight/grant", json={"confirm_token": tok, "confirm_profile_id": "mybank.me"})
    assert r.status_code == 200 and r.json()["status"] == "granted"
    st = ctx.preflight.override_status("mybank.me")
    assert st and st["actor"] == "owner"
    audited = ctx.audit.list(action_prefix="preflight.override_granted")
    assert audited and audited[0].details.get("grant") == st["id"]


def test_token_is_single_use_no_double_grant(papp):
    ctx, c = papp
    login(c)
    tok = check(c).json()["confirm_token"]
    body = {"confirm_token": tok, "confirm_profile_id": "mybank.me"}
    assert c.post("/api/profiles/mybank.me/preflight/grant", json=body).status_code == 200
    # replaying the same confirmation does not create or extend a second grant
    assert c.post("/api/profiles/mybank.me/preflight/grant", json=body).status_code == 409
    grants = ctx.audit.list(action_prefix="preflight.override_granted")
    assert len(grants) == 1


def test_stale_when_report_changes_after_check(papp):
    ctx, c = papp
    login(c)
    tok = check(c).json()["confirm_token"]
    # the owner adds a proxy in another tab, but it still exits at a datacenter IP: the report still
    # fails, but on a different set of checks (proxy now passes, egress still fails) -> stale.
    from friday.profiles.models import ProxyConfig

    ctx.profiles.update_config("mybank.me", ctx.profiles.get("mybank.me").config.model_copy(
        update={"proxy": ProxyConfig(server="http://100.64.0.2:3128")}))
    r = c.post("/api/profiles/mybank.me/preflight/grant", json={"confirm_token": tok, "confirm_profile_id": "mybank.me"})
    assert r.status_code == 409  # stale: changed report
    body = r.json()["detail"]
    assert body["status"] == "changed" and "proxy" not in {i["name"] for i in body["report"]["items"] if not i["ok"]}
    assert ctx.preflight.override_status("mybank.me") is None


def test_all_clear_creates_no_grant(papp):
    ctx, c = papp
    login(c)
    tok = check(c).json()["confirm_token"]
    # everything passes now (ready profile, residential egress)
    make_ready(ctx)  # patchright + proxy + en-IN
    ctx.preflight = Preflight(ctx, fetch_geo=geo({"ip": "49.36.1.2", "org": "AS55836 Jio", "country": "IN"}))
    r = c.post("/api/profiles/mybank.me/preflight/grant", json={"confirm_token": tok, "confirm_profile_id": "mybank.me"})
    assert r.status_code == 200 and r.json()["status"] == "all_clear"
    assert ctx.preflight.override_status("mybank.me") is None


def test_grant_rejected_while_a_signin_is_queued(papp):
    ctx, c = papp
    login(c)
    tok = check(c).json()["confirm_token"]
    # a queued sign-in run exists for this profile
    ctx.runs.create("login", "mybank.me", {})
    r = c.post("/api/profiles/mybank.me/preflight/grant", json={"confirm_token": tok, "confirm_profile_id": "mybank.me"})
    assert r.status_code == 409 and "queued or running" in r.json()["detail"]
    assert ctx.preflight.override_status("mybank.me") is None


def test_cross_origin_grant_refused(papp):
    ctx, c = papp
    login(c)
    tok = check(c).json()["confirm_token"]
    r = c.post("/api/profiles/mybank.me/preflight/grant",
               json={"confirm_token": tok, "confirm_profile_id": "mybank.me"},
               headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    assert ctx.preflight.override_status("mybank.me") is None


def test_check_and_grant_are_post_only(papp):
    ctx, c = papp
    login(c)
    assert c.get("/api/profiles/mybank.me/preflight").status_code == 405
    assert c.get("/api/profiles/mybank.me/preflight/grant").status_code == 405


def test_cancel_clears_an_armed_grant(papp):
    ctx, c = papp
    login(c)
    tok = check(c).json()["confirm_token"]
    c.post("/api/profiles/mybank.me/preflight/grant", json={"confirm_token": tok, "confirm_profile_id": "mybank.me"})
    assert ctx.preflight.override_status("mybank.me") is not None
    assert c.post("/api/profiles/mybank.me/preflight/cancel").json()["cancelled"] is True
    assert ctx.preflight.override_status("mybank.me") is None


def test_repin_endpoint_clears_the_pin(papp):
    ctx, c = papp
    login(c)
    ctx.db.execute("UPDATE profiles SET fingerprint_json = ? WHERE id = ?", ('{"driver": "playwright"}', "mybank.me"))
    assert c.post("/api/profiles/mybank.me/repin").json()["repinned"] is True
    assert ctx.profiles.get("mybank.me").fingerprint is None
    assert ctx.audit.list(action_prefix="profile.repin")


def test_state_exposes_override_metadata_not_token(papp):
    ctx, c = papp
    login(c)
    tok = check(c).json()["confirm_token"]
    c.post("/api/profiles/mybank.me/preflight/grant", json={"confirm_token": tok, "confirm_profile_id": "mybank.me"})
    prof = c.get("/api/state").json()["profiles"][0]
    assert prof["override"]["actor"] == "owner" and prof["effective_driver"] == "patchright"
    # the confirmation token is never surfaced in state
    assert "confirm_token" not in str(c.get("/api/state").json())
