from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from friday.browsercheck import evaluate, parse_candidate, report_lines
from friday.drivers import LaunchRequest, ProxySettings
from friday.preflight import Item, Preflight
from friday.runs import RunStatus
from tests.test_preflight import GCP, HOME, geo, make_ready

GOOD_PROBE = {"webdriver": False, "language": "en-IN", "languages": ["en-IN", "en"], "timezone": "Asia/Kolkata",
              "plugins": 5, "webgl": {"renderer": "ANGLE (Google, Vulkan 1.3.0 (SwiftShader Device))"},
              "screen": [1920, 1080], "window": [1920, 1080, 1920, 960]}  # fmt: skip
HOME_EG = {**HOME, "asn": 55836}


def obs(**kw):
    o = {"ipv4": HOME["ip"], "ipv6": None, "accept_language": "en-IN,en;q=0.9",
         "webrtc": {"available": True, "parsed": [{"address": "3f1c.local", "type": "host", "protocol": "udp"}]},
         "probe": dict(GOOD_PROBE)}  # fmt: skip
    o.update(kw)
    return o


def failed(items):
    return [i.name for i in items if not i.ok]


def test_webrtc_flag_only_with_a_proxy():
    req = LaunchRequest(Path("/x"), Path("/y"), "en-IN", "Asia/Kolkata", 1280, 800)
    assert not any("webrtc" in a for a in req.chrome_args())
    req.proxy = ProxySettings("http://100.64.0.2:3128")
    assert "--force-webrtc-ip-handling-policy=disable_non_proxied_udp" in req.chrome_args()


def test_parse_candidate():
    c = parse_candidate("candidate:842163049 1 udp 1677729535 34.14.129.151 46154 typ srflx raddr 0.0.0.0 rport 0")
    assert c == {"address": "34.14.129.151", "protocol": "udp", "type": "srflx"}
    assert parse_candidate("garbage") is None


def test_clean_profile_passes(ctx):
    p = make_ready(ctx)
    items = evaluate(obs(), p, HOME_EG, browser_version="141.0.7390.37")
    assert failed(items) == [], report_lines({"profile_id": p.id, "ok": False, "checked_at": "x" * 16,
                                              "items": [i.__dict__ for i in items]})  # fmt: skip
    assert "software rendering" in next(i for i in items if i.name == "webgl").detail


@pytest.mark.parametrize("change,bad", [
    ({"ipv4": GCP["ip"]}, "ipv4"),
    ({"ipv4": None}, "ipv4"),
    ({"ipv6": "2600:1900:4000::1"}, "ipv6"),
    ({"webrtc": {"available": True, "parsed": [{"address": "34.14.129.151", "type": "srflx", "protocol": "udp"}]}}, "webrtc"),
    ({"accept_language": "en-GB,en;q=0.9"}, "accept-language"),
    ({"probe": {**GOOD_PROBE, "webdriver": True}}, "webdriver"),
    ({"probe": {**GOOD_PROBE, "language": "en-GB", "languages": ["en-GB"]}}, "languages"),
    ({"probe": {**GOOD_PROBE, "timezone": "Europe/London"}}, "timezone"),
    ({"probe": {**GOOD_PROBE, "window": [0, 0, 0, 0]}}, "window"),
])  # fmt: skip
def test_each_leak_or_mismatch_fails_its_item(ctx, change, bad):
    p = make_ready(ctx)
    assert failed(evaluate(obs(**change), p, HOME_EG)) == [bad]


def test_ip_country_vs_region_and_chrome_major_jump(ctx):
    p = make_ready(ctx)
    assert failed(evaluate(obs(), p, {**HOME_EG, "country": "US"})) == ["timezone"]
    ctx.db.execute("UPDATE profiles SET fingerprint_json = ? WHERE id = ?", (json.dumps({"browserVersion": "140.0.1"}), p.id))
    p = ctx.profiles.get(p.id)
    assert failed(evaluate(obs(), p, HOME_EG, browser_version="141.0.2")) == ["chrome-version"]


def test_live_state_without_proxy(ctx):
    p = ctx.profiles.create("mybank", "me")
    items = evaluate(obs(ipv4=GCP["ip"], probe={**GOOD_PROBE, "webdriver": True}), p, GCP)
    assert {"ipv4", "webdriver"} <= set(failed(items))


def _cache(ctx, p, items, scope="full"):
    ctx.browsercheck._save(p, items, {}, HOME_EG, scope=scope)


async def test_preflight_browser_item_uses_the_cached_check(ctx):
    p = make_ready(ctx)
    rep = await ctx.preflight.check(p.id)
    assert rep.ok and "leak check runs in the browser" in next(i for i in rep.items if i.name == "browser").detail
    _cache(ctx, p, [Item("ipv6", False, "IPv6 2600::1 reachable outside the proxy", ["sysctl ..."])])
    rep = await ctx.preflight.check(p.id)
    assert [i.name for i in rep.failed] == ["browser"] and "browser-check mybank.me" in rep.failed[0].fix[-1]
    # a new proxy invalidates the old result
    ctx.profiles.update_config(p.id, p.config.model_copy(update={"proxy": p.config.proxy.model_copy(update={"server": "http://100.64.0.9:3128"})}))
    assert ctx.browsercheck.cached(ctx.profiles.get(p.id)) is None


def test_leak_refresh_keeps_the_full_items(ctx):
    p = make_ready(ctx)
    _cache(ctx, p, [Item("webdriver", False, "true"), Item("ipv4", True, "ok")])
    _cache(ctx, p, [Item("ipv4", True, "ok"), Item("ipv6", True, "none"), Item("webrtc", True, "none")], scope="leaks")
    r = ctx.prefs.get("browsercheck:mybank.me")
    assert sorted(i["name"] for i in r["items"]) == ["ipv4", "ipv6", "webdriver", "webrtc"] and r["ok"] is False


async def test_in_run_leak_check_stops_before_the_site(ctx, monkeypatch):
    p = make_ready(ctx)

    class Page:
        async def goto(self, *a, **k):
            raise AssertionError("the site must not be opened")

    class Session:
        browser_version = "141"
        context = None

        async def page(self):
            return Page()

    class Op:
        profile = p
        site = ctx.sites.get("mybank")
        session = Session()
        warnings: list = []

    @asynccontextmanager
    async def fake_open(*a, **k):
        yield Op()

    async def leaky(profile, page):
        return [Item("ipv4", True, "ok"), Item("webrtc", False, "ICE candidates expose 34.14.129.151", ["update friday"])]

    monkeypatch.setattr(ctx.profiles, "open", fake_open)
    monkeypatch.setattr(ctx.browsercheck, "leaks_in_session", leaky)
    run_id = ctx.jobs.submit("login", p.id)
    await ctx.jobs.wait_idle()
    run = ctx.runs.get(run_id)
    assert run["status"] == RunStatus.NEEDS_ATTENTION.value and run["result"]["browser_check"] == ["webrtc"]
    assert ctx.jobs.backoff_state("mybank") is None
    assert any("leaks around the proxy" in m["text"] for m in ctx.chat.store.list())


def test_cli_set_region(ctx, monkeypatch):
    from typer.testing import CliRunner

    import friday.cli as cli_mod

    p = make_ready(ctx)
    ctx.profiles.update_config(p.id, p.config.model_copy(update={"locale": "en-GB", "timezone": "Europe/London"}))
    monkeypatch.setattr(cli_mod, "_ctx", lambda: ctx)
    r = CliRunner().invoke(cli_mod.app, ["profile", "set-region", p.id, "in"])
    assert r.exit_code == 0, r.output
    cfg = ctx.profiles.get(p.id).config
    assert (cfg.locale, cfg.timezone) == ("en-IN", "Asia/Kolkata") and "consistent" in r.output
    ctx.preflight = Preflight(ctx, fetch_geo=geo({**HOME, "country": "GB"}))
    r = CliRunner().invoke(cli_mod.app, ["profile", "set-region", p.id, "IN"])
    assert r.exit_code == 1 and "MISMATCH" in r.output
    assert CliRunner().invoke(cli_mod.app, ["profile", "set-region", p.id, "US"]).exit_code == 2  # needs --timezone


@pytest.fixture
def echo_server():
    import http.server
    import threading

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps({"ip": self.client_address[0]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/?format=json"
    srv.shutdown()


@pytest.mark.chrome
async def test_browser_check_in_real_chromium(ctx, echo_server):
    """Through the real launch path, against a local echo page (no public internet)."""
    from friday.profiles.models import Site

    ctx.settings.display_backend = "auto"
    ctx.settings.ip_echo_url, ctx.settings.ip6_echo_url, ctx.settings.webrtc_stun_url = echo_server, None, None
    ctx.sites.upsert(Site(id="mock", name="Mock", login_url=echo_server, domains=["127.0.0.1"]))
    p = ctx.profiles.create("mock", "me")
    ctx.profiles.update_config(p.id, p.config.model_copy(update={"locale": "en-IN", "timezone": "Asia/Kolkata"}))
    ctx.preflight = Preflight(ctx, fetch_geo=geo({"ip": "127.0.0.1", "org": "local", "country": "IN"}))
    rep = await ctx.browsercheck.run(p.id)
    o = rep["observed"]
    assert o["ipv4"] == "127.0.0.1" and o["accept_language"].startswith("en-IN")
    assert o["probe"]["timezone"] in ("Asia/Kolkata", "Asia/Calcutta") and o["probe"]["window"][0] > 0
    assert o["webrtc"]["available"] is True
    names = {i["name"]: i["ok"] for i in rep["items"]}
    assert names["ipv4"] is False  # no proxy configured: reported, as it would be live
    assert names["webrtc"] and names["timezone"] and names["accept-language"] and names["window"]
    assert ctx.prefs.get("browsercheck:mock.me")["scope"] == "full"
    assert (ctx.profiles.user_data_dir(p).parent / "browser-check.json").exists()
