from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from friday.preflight import OVERRIDE_TTL, Preflight, is_datacenter, otp_channel, parse_asn
from friday.profiles.doctor import check_all, examine
from friday.profiles.models import ProxyConfig, Site
from friday.runs import RunStatus
from friday.util import iso
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


async def test_explicit_playwright_fails_every_network_item(ctx):
    """An explicit playwright profile, no proxy, the server's own GCP IP: driver + proxy + egress fail."""
    p = ctx.profiles.create("mybank", "me")
    ctx.profiles.update_config(p.id, p.config.model_copy(update={"driver": "playwright"}))
    ctx.preflight = Preflight(ctx, fetch_geo=geo(GCP))
    rep = await ctx.preflight.check(p.id)
    assert not rep.ok
    assert {i.name for i in rep.failed} == {"driver", "proxy", "egress"}
    msg = rep.chat_message()
    assert "profile set-driver mybank.me patchright" in msg and "deploy/egress.sh mybank.me" in msg
    assert "Google LLC" in msg and "Not counted as a bounce" in msg


async def test_default_driver_profile_still_fails_on_proxy_and_egress(ctx):
    """New default: a null-driver profile inherits patchright, so the driver check passes, but a
    plain cloud profile with no proxy is still blocked on proxy and egress."""
    p = ctx.profiles.create("mybank", "me")
    assert ctx.profiles.effective_driver(p.config) == "patchright"
    ctx.preflight = Preflight(ctx, fetch_geo=geo(GCP))
    rep = await ctx.preflight.check(p.id)
    assert not rep.ok
    assert "driver" not in {i.name for i in rep.failed}
    assert {"proxy", "egress"} <= {i.name for i in rep.failed}
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
    ctx.preflight.grant_override("mybank.me", failed=["driver", "proxy"])
    assert ctx.preflight.override_status("mybank.me")["failed"] == ["driver", "proxy"]
    assert ctx.preflight.take_override("mybank.me", run_id="r1")
    assert not ctx.preflight.take_override("mybank.me")
    # the consume was audited with the run id and the failed checks, not as "all clear"
    used = [e for e in ctx.audit.list(action_prefix="preflight.override_used")]
    assert used and used[0].details.get("run") == "r1" and used[0].details.get("failed") == ["driver", "proxy"]


def test_override_status_read_does_not_consume_or_renew(ctx):
    ctx.profiles.create("mybank", "me")
    ctx.preflight.grant_override("mybank.me")
    for _ in range(3):
        assert ctx.preflight.override_status("mybank.me") is not None  # repeated reads don't consume
    assert ctx.preflight.take_override("mybank.me")  # still consumable exactly once
    assert ctx.preflight.override_status("mybank.me") is None


def test_grant_has_server_controlled_ttl_and_id(ctx):


    ctx.profiles.create("mybank", "me")
    before = datetime.now(UTC)
    ctx.preflight.grant_override("mybank.me", actor="owner")
    st = ctx.preflight.override_status("mybank.me")
    assert st["id"] and len(st["id"]) >= 8
    delta = datetime.fromisoformat(st["until"]) - before
    assert OVERRIDE_TTL - timedelta(seconds=5) <= delta <= OVERRIDE_TTL + timedelta(seconds=5)


def test_clear_override(ctx):
    ctx.profiles.create("mybank", "me")
    assert not ctx.preflight.clear_override("mybank.me")
    ctx.preflight.grant_override("mybank.me")
    assert ctx.preflight.clear_override("mybank.me")
    assert ctx.preflight.override_status("mybank.me") is None
    assert not ctx.preflight.take_override("mybank.me")


def test_legacy_bare_iso_grant_is_honoured(ctx):
    """A grant stored by an older build (just the expiry string) still works once."""
    ctx.profiles.create("mybank", "me")
    ctx.prefs.set("preflight_override:mybank.me", iso(datetime.now(UTC) + timedelta(minutes=10)))
    assert ctx.preflight.override_status("mybank.me") is not None
    assert ctx.preflight.take_override("mybank.me")
    assert not ctx.preflight.take_override("mybank.me")


def test_expired_and_malformed_grants_fail_closed(ctx):
    ctx.profiles.create("mybank", "me")
    ctx.prefs.set("preflight_override:mybank.me", iso(datetime.now(UTC) - timedelta(minutes=1)))
    assert ctx.preflight.override_status("mybank.me") is None
    assert not ctx.preflight.take_override("mybank.me")  # expired: consumed-and-cleared, no attempt
    ctx.prefs.set("preflight_override:mybank.me", {"garbage": True})
    assert ctx.preflight.override_status("mybank.me") is None
    assert not ctx.preflight.take_override("mybank.me")


def test_grant_survives_a_reopen_and_is_not_renewed(ctx, tmp_path):
    from friday.context import build_context
    from friday.vault.masking import Redactor
    from tests.conftest import TEST_KEY

    s = ctx.settings.model_copy(update={"data_dir": tmp_path / "db"})
    c1 = build_context(s, master_key=TEST_KEY, redactor=Redactor(), chat_llm_factory=False)
    c1.sites.upsert(ctx.sites.get("mybank"))
    c1.profiles.create("mybank", "me")
    c1.preflight.grant_override("mybank.me")
    until = c1.preflight.override_status("mybank.me")["until"]
    c1.db.close()
    c2 = build_context(s, master_key=TEST_KEY, redactor=Redactor(), chat_llm_factory=False)
    assert c2.preflight.override_status("mybank.me")["until"] == until  # same expiry, not renewed
    assert c2.preflight.take_override("mybank.me")
    assert not c2.preflight.take_override("mybank.me")
    c2.db.close()


def test_concurrent_consumers_across_connections_yield_one_winner(tmp_path):
    import threading

    from friday.context import build_context
    from friday.profiles.models import Site
    from friday.vault.masking import Redactor
    from tests.conftest import TEST_KEY, TEST_PASSWORD
    from friday.config import Settings
    from friday.web.auth import hash_password

    s = Settings(_env_file=None, data_dir=tmp_path / "data", volume_check="require", volume_encrypted_assert=True,
                 display_backend="native", admin_password_hash=hash_password(TEST_PASSWORD), public_base_url="http://testserver")  # fmt: skip
    seed = build_context(s, master_key=TEST_KEY, redactor=Redactor(), chat_llm_factory=False)
    seed.sites.upsert(Site(id="mybank", name="My Bank", login_url="https://login.mybank.example/", domains=["mybank.example"]))
    seed.profiles.create("mybank", "me")
    seed.preflight.grant_override("mybank.me")
    seed.db.close()
    # N independent contexts (separate SQLite connections) race to consume the one grant.
    ctxs = [build_context(s, master_key=TEST_KEY, redactor=Redactor(), chat_llm_factory=False) for _ in range(8)]
    results: list[bool] = []
    lock = threading.Lock()
    barrier = threading.Barrier(len(ctxs))

    def worker(c):
        barrier.wait()
        ok = c.preflight.take_override("mybank.me")
        with lock:
            results.append(ok)

    threads = [threading.Thread(target=worker, args=(c,)) for c in ctxs]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    for c in ctxs:
        c.db.close()
    assert sum(results) == 1, results


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
    p = ctx.profiles.create("mybank", "me")  # null driver -> patchright by default
    pin(ctx, p.id, {"driver": "patchright", "pinned_for": ctx.profiles.identity_config(p.config)})
    # moving to an explicitly different driver changes the identity config, so the pin is cleared
    ctx.profiles.update_config(p.id, p.config.model_copy(update={"driver": "playwright"}))
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


def test_cli_preflight_json(ctx, monkeypatch):
    from typer.testing import CliRunner

    import friday.cli as cli_mod

    make_ready(ctx, egress=GCP)
    monkeypatch.setattr(cli_mod, "_ctx", lambda: ctx)
    r = CliRunner().invoke(cli_mod.app, ["preflight", "mybank.me", "--json"])
    assert r.exit_code == 1
    out = json.loads(r.output.strip().splitlines()[-1])
    assert out["egress"]["asn"] == 396982 and [i["name"] for i in out["items"] if not i["ok"]] == ["egress"]
    r = CliRunner().invoke(cli_mod.app, ["preflight", "mybank.me", "--override"])
    assert r.exit_code == 0 and "Override granted" in r.output
