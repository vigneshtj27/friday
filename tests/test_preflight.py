from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from friday.preflight import Preflight, is_datacenter, otp_channel, parse_asn
from friday.profiles.doctor import check_all, examine
from friday.profiles.models import ProxyConfig, Site
from friday.runs import RunStatus
from friday.version import build_info
from friday.web.app import create_app
from tests.conftest import TEST_PASSWORD

HOME = {"ip": "49.36.1.2", "org": "AS55836 Reliance Jio Infocomm Limited", "country": "IN", "city": "Chennai"}
GCP = {"ip": "34.14.129.151", "org": "AS396982 Google LLC", "country": "IN", "city": "Mumbai"}


def geo(answer):
    calls = []

    async def fetch(proxy_url):
        calls.append(proxy_url)
        if isinstance(answer, Exception):
            raise answer
        return dict(answer)

    fetch.calls = calls
    return fetch


def make_ready(ctx, pid="mybank.me", *, egress=HOME):
    p = ctx.profiles.get(pid) if any(x.id == pid for x in ctx.profiles.list()) else ctx.profiles.create("mybank", "me")
    cfg = p.config.model_copy(update={"driver": "patchright", "locale": "en-IN", "timezone": "Asia/Kolkata",
                                      "proxy": ProxyConfig(server="http://100.64.0.2:3128")})  # fmt: skip
    ctx.profiles.update_config(p.id, cfg)
    ctx.preflight = Preflight(ctx, fetch_geo=geo(egress))
    return ctx.profiles.get(p.id)


def pin(ctx, pid, fp):
    ctx.db.execute("UPDATE profiles SET fingerprint_json = ? WHERE id = ?", (json.dumps(fp), pid))


def test_asn_parsing_and_datacenter_detection():
    assert parse_asn("AS396982 Google LLC") == 396982 and parse_asn(None) is None
    assert is_datacenter(GCP)
    assert is_datacenter({"org": "AS99999 Some Hosting Ltd"})  # by name
    assert not is_datacenter(HOME)


async def test_fresh_live_config_fails_every_network_item(ctx):
    """The live state: driver null, no proxy, the server's own GCP IP."""
    p = ctx.profiles.create("mybank", "me")
    ctx.preflight = Preflight(ctx, fetch_geo=geo(GCP))
    rep = await ctx.preflight.check(p.id)
    assert not rep.ok
    assert {i.name for i in rep.failed} == {"driver", "proxy", "egress"}
    msg = rep.chat_message()
    assert "profile set-driver mybank.me patchright" in msg and "deploy/egress.sh mybank.me" in msg
    assert "Google LLC" in msg and "Not counted as a bounce" in msg
    assert ctx.prefs.get("preflight:mybank.me")["ok"] is False


async def test_ready_profile_passes_and_proxy_is_used(ctx):
    p = make_ready(ctx)
    rep = await ctx.preflight.check(p.id)
    assert rep.ok, rep.lines()
    assert ctx.preflight._fetch_geo.calls == ["http://100.64.0.2:3128"]
    eg = ctx.prefs.get("egress:mybank.me")
    assert eg["asn"] == 55836 and eg["datacenter"] is False and eg["proxy"] == "http://100.64.0.2:3128"
    await ctx.preflight.check(p.id)  # cached
    assert len(ctx.preflight._fetch_geo.calls) == 1
    await ctx.preflight.check(p.id, fresh=True)
    assert len(ctx.preflight._fetch_geo.calls) == 2


@pytest.mark.parametrize("answer,needle", [(GCP, "datacenter"), ({**HOME, "country": "US"}, "expected an IP in IN"),
                                           (OSError("connection refused"), "couldn't look up")])  # fmt: skip
async def test_bad_egress_through_the_proxy(ctx, answer, needle):
    p = make_ready(ctx, egress=answer)
    rep = await ctx.preflight.check(p.id)
    assert [i.name for i in rep.failed] == ["egress"] and needle in rep.failed[0].detail


async def test_localhost_site_skips_network_checks(ctx):
    ctx.sites.upsert(Site(id="mock", name="Mock", login_url="http://127.0.0.1:9/login", domains=["127.0.0.1"]))
    p = ctx.profiles.create("mock", "me")
    ctx.preflight = Preflight(ctx, fetch_geo=geo(GCP))
    rep = await ctx.preflight.check(p.id)
    assert rep.ok and rep.items[0].name == "network" and ctx.preflight._fetch_geo.calls == []


async def test_google_item_only_when_asked(ctx):
    p = make_ready(ctx)
    ctx.google.can = lambda scope: False
    assert (await ctx.preflight.check(p.id)).ok
    rep = await ctx.preflight.check(p.id, need_google=True)
    assert [i.name for i in rep.failed] == ["google"]


async def test_drifted_pin_fails_identity(ctx):
    p = make_ready(ctx)
    pin(ctx, p.id, {"driver": "playwright", "webdriver": True, "language": "en-GB", "languages": ["en-GB"], "timezone": "Asia/Kolkata"})
    rep = await ctx.preflight.check(p.id)
    assert [i.name for i in rep.failed] == ["identity"]
    assert "profile doctor mybank.me --repin" in rep.failed[0].fix[0]


def test_otp_channel(ctx, monkeypatch):
    site = ctx.sites.get("mybank")
    assert otp_channel(ctx, site)["channel"] == "chat"
    ctx.settings.sms_webhook_token = "tok"
    ch = otp_channel(ctx, site)
    assert ch["channel"] == "sms" and ch["live"] is False
    ctx.audit.record("hook.sms", actor="sms", target="sms")
    assert otp_channel(ctx, site)["live"] is True
    monkeypatch.setattr(type(ctx.gmail), "configured", property(lambda self: True))
    site = site.model_copy(update={"otp_senders": ["AmericanExpress@welcome.americanexpress.com"]})
    assert otp_channel(ctx, site)["channel"] == "email"


def test_override_is_one_use(ctx):
    ctx.profiles.create("mybank", "me")
    assert not ctx.preflight.take_override("mybank.me")
    ctx.preflight.grant_override("mybank.me")
    assert ctx.preflight.take_override("mybank.me")
    assert not ctx.preflight.take_override("mybank.me")


async def test_blocked_run_never_opens_the_browser_and_is_not_a_bounce(ctx, monkeypatch):
    p = ctx.profiles.create("mybank", "me")
    ctx.preflight = Preflight(ctx, fetch_geo=geo(GCP))

    def boom(*a, **k):
        raise AssertionError("the site must not be touched")

    monkeypatch.setattr(ctx.profiles, "open", boom)
    run_id = ctx.jobs.submit("fetch_activity", p.id, {"start": "2026-10-01", "end": "2026-10-20"})
    await ctx.jobs.wait_idle()
    run = ctx.runs.get(run_id)
    assert run["status"] == RunStatus.NEEDS_ATTENTION.value
    assert "Preflight blocked" in run["summary"] and run["result"]["preflight"]["ok"] is False
    assert ctx.jobs.backoff_state("mybank") is None  # not counted as a bounce
    msgs = [m["text"] for m in ctx.chat.store.list()]
    assert any("I didn't try to sign in to mybank.me" in t for t in msgs)
    # a second request is blocked the same way, not by backoff
    ctx.jobs.submit("login", p.id)
    await ctx.jobs.wait_idle()


async def test_override_lets_exactly_one_attempt_through(ctx, monkeypatch):
    p = ctx.profiles.create("mybank", "me")
    ctx.preflight = Preflight(ctx, fetch_geo=geo(GCP))
    opened = []

    def fake_open(*a, **k):
        opened.append(1)
        raise RuntimeError("stop here")

    monkeypatch.setattr(ctx.profiles, "open", fake_open)
    ctx.preflight.grant_override(p.id)
    ctx.jobs.submit("login", p.id)
    await ctx.jobs.wait_idle()
    ctx.jobs.submit("login", p.id)
    await ctx.jobs.wait_idle()
    assert opened == [1]


async def test_warn_and_off_modes_do_not_block(ctx, monkeypatch):
    p = ctx.profiles.create("mybank", "me")
    ctx.preflight = Preflight(ctx, fetch_geo=geo(GCP))
    opened = []
    monkeypatch.setattr(ctx.profiles, "open", lambda *a, **k: opened.append(1) or (_ for _ in ()).throw(RuntimeError("stop")))
    for mode in ("warn", "off"):
        ctx.settings.preflight = mode
        ctx.jobs.submit("login", p.id)
        await ctx.jobs.wait_idle()
    assert opened == [1, 1]


# ---- profile doctor --------------------------------------------------------


def test_doctor_old_pin_is_stale_and_repinned(ctx):
    p = ctx.profiles.create("mybank", "me")
    pin(ctx, p.id, {"driver": "playwright", "webdriver": True, "language": "en-GB", "languages": ["en-GB"]})
    rep = examine(ctx.profiles, ctx.profiles.get(p.id))
    assert rep.stale and any("en-GB" in i for i in rep.issues)
    check_all(ctx, repin=True)
    assert ctx.profiles.get(p.id).fingerprint is None


def test_doctor_mismatch_under_current_config_is_not_repinned(ctx):
    """A fresh pin that still disagrees means the browser ignores the config; re-pinning would loop."""
    p = ctx.profiles.create("mybank", "me")
    want = ctx.profiles.identity_config(p.config)
    pin(ctx, p.id, {"driver": want["driver"], "language": "en-GB", "pinned_for": want})
    rep = examine(ctx.profiles, ctx.profiles.get(p.id))
    assert rep.issues and not rep.stale
    assert any("isn't applying the config" in n for n in rep.notes)
    check_all(ctx, repin=True)
    assert ctx.profiles.get(p.id).fingerprint is not None


def test_config_change_repins(ctx):
    p = ctx.profiles.create("mybank", "me")
    pin(ctx, p.id, {"driver": "playwright", "pinned_for": ctx.profiles.identity_config(p.config)})
    ctx.profiles.update_config(p.id, p.config.model_copy(update={"driver": "patchright"}))
    assert ctx.profiles.get(p.id).fingerprint is None


# ---- version stamp and /api/state ------------------------------------------


def test_build_info_file(tmp_path, monkeypatch):
    import friday.version as v

    f = tmp_path / "BUILD_INFO"
    f.write_text(json.dumps({"sha": "9857f4d0123456789", "ref": "main", "built_at": "2026-10-20T14:00:00Z"}))
    monkeypatch.setattr(v, "BUILD_INFO", f)
    v.build_info.cache_clear()
    b = v.build_info()
    v.build_info.cache_clear()
    assert b["short"] == "9857f4d" and b["built_at"] == "2026-10-20T14:00:00Z" and b["source"] == "build"


def test_healthz_and_state_carry_version_egress_and_otp(ctx):
    make_ready(ctx)
    ctx.prefs.set("egress:mybank.me", {"ip": "49.36.1.2", "asn": 55836, "proxy": "http://100.64.0.2:3128"})
    with TestClient(create_app(ctx), base_url="http://testserver") as c:
        h = c.get("/healthz").json()
        assert h["ok"] and h["version"] == build_info()["short"]
        c.post("/login", data={"password": TEST_PASSWORD}, follow_redirects=False)
        s = c.get("/api/state").json()
    assert s["version"]["short"] == h["version"]
    assert s["egress"]["mybank.me"]["asn"] == 55836
    assert s["otp_channels"]["mybank"]["channel"] == "chat"
