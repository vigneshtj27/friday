"""Browser self-check: what does a site see from this profile's real browser?

`friday browser-check <profile>` opens the profile through the normal launch path (same driver,
proxy, locale, display) and, on neutral IP-echo pages only (never a bank), records:

  navigator.webdriver, language/languages and the Accept-Language header, timezone vs the IP's
  country, the IPv4 the browser itself egresses from, whether an IPv6 address bypasses the proxy,
  WebRTC ICE candidates (a STUN gather can expose the VM's own address), WebGL renderer, plugins,
  window/screen sizes and the Chrome version vs the pin.

The leak subset (IPv4, IPv6, WebRTC) also runs inside a sign-in run, in the same browser, before
the site is opened, when no fresh result exists for the current proxy. Nothing here spoofs
anything: a failure is fixed by changing the setup (proxy, driver, region), not by faking values.
"""

from __future__ import annotations

import ipaddress
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from friday.preflight import Item, expected_country
from friday.util import iso

log = logging.getLogger(__name__)

CHECK_TTL = timedelta(hours=24)
LEAK_ITEMS = ("ipv4", "ipv6", "webrtc")

CANNOT_HELP = [
    "Where code cannot help:",
    "- The IP matters most, and friday can't create a residential one: use a home connection (deploy/egress.sh) "
    "or one fixed residential/ISP proxy per profile. Never a rotating pool.",
    "- Software WebGL (SwiftShader/llvmpipe) on a server is honest and is not spoofed. A browser running on a "
    "home machine (browser-node mode) has a real GPU, fonts and screen.",
    "- No setting cleans an account or IP that is already flagged: wait (hours, maybe a day) before the next real sign-in.",
    "- MFA and CAPTCHAs go to you (code card, remote view); trusted-device status is earned by a clean manual "
    "sign-in with 'remember this device' from the home IP.",
]

PROBE_JS = """() => {
  let webgl = null;
  try {
    const g = document.createElement('canvas').getContext('webgl');
    if (g) {
      const d = g.getExtension('WEBGL_debug_renderer_info');
      webgl = {vendor: d ? g.getParameter(d.UNMASKED_VENDOR_WEBGL) : g.getParameter(g.VENDOR),
               renderer: d ? g.getParameter(d.UNMASKED_RENDERER_WEBGL) : g.getParameter(g.RENDERER)};
    }
  } catch (e) {}
  return {
    webdriver: navigator.webdriver, language: navigator.language, languages: navigator.languages,
    timezone: Intl.DateTimeFormat().resolvedOptions().timeZone, userAgent: navigator.userAgent,
    plugins: navigator.plugins.length, webgl, screen: [screen.width, screen.height],
    window: [outerWidth, outerHeight, innerWidth, innerHeight], rtc: typeof RTCPeerConnection === 'function',
  };
}"""

WEBRTC_JS = """async (stun) => {
  if (typeof RTCPeerConnection !== 'function') return {available: false, candidates: []};
  const pc = new RTCPeerConnection({iceServers: stun ? [{urls: stun}] : []});
  const out = [];
  pc.onicecandidate = (e) => { if (e.candidate && e.candidate.candidate) out.push(e.candidate.candidate); };
  pc.createDataChannel('probe');
  await pc.setLocalDescription(await pc.createOffer());
  await new Promise((done) => {
    const t = setTimeout(done, 5000);
    pc.onicegatheringstatechange = () => { if (pc.iceGatheringState === 'complete') { clearTimeout(t); done(); } };
  });
  pc.close();
  return {available: true, candidates: out};
}"""


def parse_candidate(c: str) -> dict[str, str] | None:
    """`candidate:1 1 udp 2122 192.0.2.1 5000 typ srflx ...` -> {address, type, protocol}."""
    parts = c.split()
    if len(parts) < 8 or "typ" not in parts:
        return None
    return {"address": parts[4], "protocol": parts[2].lower(), "type": parts[parts.index("typ") + 1]}


def _public(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False  # mDNS host candidates (<uuid>.local) hide the address: fine
    return ip.is_global


def _major(version: str | None) -> str | None:
    return str(version).split(".")[0] if version else None


async def _echo(page: Any, url: str) -> tuple[str | None, str | None]:
    """The IP the page's own fetch reaches the echo URL from, plus the Accept-Language sent."""
    seen: dict[str, str] = {}

    def on_request(req: Any) -> None:
        if req.url.startswith(url.split("?")[0]):
            seen["accept_language"] = req.headers.get("accept-language", "")

    page.on("request", on_request)
    try:
        resp = await page.goto(url, wait_until="domcontentloaded", timeout=20_000)
        body = (await page.inner_text("body")).strip()
        if resp is None or not resp.ok:
            return None, seen.get("accept_language")
        try:
            return str(json.loads(body).get("ip")), seen.get("accept_language")
        except (ValueError, AttributeError):
            return body.split()[0] if body else None, seen.get("accept_language")
    except Exception as e:
        log.info("echo %s failed: %s", url, type(e).__name__)
        return None, seen.get("accept_language")
    finally:
        page.remove_listener("request", on_request)


async def observe(page: Any, settings: Any, *, leaks_only: bool = False) -> dict[str, Any]:
    """Collect what the site would see. Only neutral echo pages are visited."""
    obs: dict[str, Any] = {}
    obs["ipv4"], obs["accept_language"] = await _echo(page, settings.ip_echo_url)
    obs["ipv6"], _ = await _echo(page, settings.ip6_echo_url) if settings.ip6_echo_url else (None, None)
    try:
        rtc = await page.evaluate(WEBRTC_JS, settings.webrtc_stun_url)
    except Exception as e:
        rtc = {"available": None, "candidates": [], "error": type(e).__name__}
    obs["webrtc"] = {**rtc, "parsed": [p for p in (parse_candidate(c) for c in rtc.get("candidates", [])) if p]}
    if not leaks_only:
        try:
            obs["probe"] = await page.evaluate(PROBE_JS)
        except Exception as e:
            obs["probe"] = {"error": type(e).__name__}
    return obs


def evaluate(obs: dict[str, Any], profile: Any, egress: dict[str, Any], *, browser_version: str | None = None,
             leaks_only: bool = False) -> list[Item]:  # fmt: skip
    """Turn observations into pass/fail items. Pure (tested directly)."""
    cfg = profile.config
    pid = profile.id
    items: list[Item] = []
    want_ip = egress.get("ip") if not egress.get("error") else None
    v4 = obs.get("ipv4")
    if not cfg.proxy:
        items.append(Item("ipv4", False, f"browser egresses from {v4} with no proxy (the server's own IP)", [f"deploy/egress.sh {pid} <home-tailscale-ip>"]))
    elif v4 is None:
        items.append(Item("ipv4", False, "the browser couldn't reach the IP echo page through the proxy", [f"deploy/gcp/deploy.sh cli preflight {pid} --fresh"]))
    elif want_ip and v4 != want_ip:
        items.append(Item("ipv4", False, f"browser egresses from {v4}, but the proxy exits at {want_ip}: traffic is bypassing the proxy",
                          [f"deploy/gcp/deploy.sh cli profile show {pid}  # check config.proxy.bypass"]))  # fmt: skip
    else:
        items.append(Item("ipv4", True, f"{v4}" + (" (= proxy egress)" if want_ip else " (proxy egress not looked up)")))
    v6 = obs.get("ipv6")
    v6_leak = bool(v6) and v6 != want_ip
    items.append(Item("ipv6", not v6_leak, f"IPv6 {v6} reachable outside the proxy" if v6_leak else "no IPv6 outside the proxy",
                      ["sysctl net.ipv6.conf.all.disable_ipv6=1 on the VM (deploy/gcp/startup.sh does this on boot) and in the container (docker-compose.yml)"]))  # fmt: skip
    rtc = obs.get("webrtc") or {}
    exposed = sorted({c["address"] for c in rtc.get("parsed", []) if _public(c["address"]) and c["address"] != want_ip})
    if rtc.get("available") is False:
        items.append(Item("webrtc", True, "WebRTC unavailable (note: a missing API is itself unusual)"))
    else:
        items.append(Item("webrtc", not exposed, f"ICE candidates expose {', '.join(exposed)}" if exposed else
                          f"no public address outside the proxy ({len(rtc.get('parsed', []))} candidates)",
                          ["the launcher sets --force-webrtc-ip-handling-policy=disable_non_proxied_udp when a proxy is set; "
                           "update friday and re-run browser-check"]))  # fmt: skip
    if leaks_only:
        return items
    p = obs.get("probe") or {}
    if p.get("error"):
        items.append(Item("probe", False, f"page probe failed ({p['error']})"))
        return items
    items.append(Item("webdriver", p.get("webdriver") is not True, f"navigator.webdriver = {str(p.get('webdriver')).lower()}",
                      [f"deploy/gcp/deploy.sh cli profile set-driver {pid} patchright"]))  # fmt: skip
    langs = p.get("languages") or []
    lang_ok = str(p.get("language", "")).lower() == cfg.locale.lower() and (not langs or str(langs[0]).lower() == cfg.locale.lower())
    items.append(Item("languages", lang_ok, f"navigator.language {p.get('language')}, languages {langs}, configured {cfg.locale}",
                      [f"deploy/gcp/deploy.sh cli profile set-region {pid} <country>"]))  # fmt: skip
    al = obs.get("accept_language") or ""
    items.append(Item("accept-language", al.lower().startswith(cfg.locale.lower()), f"Accept-Language: {al or 'not seen'}",
                      [f"deploy/gcp/deploy.sh cli profile set-region {pid} <country>"]))  # fmt: skip
    from friday.profiles.manager import same_timezone

    tz_ok = not cfg.timezone or same_timezone(p.get("timezone"), cfg.timezone)
    country = (egress.get("country") or "").upper()
    want_country = expected_country(profile)
    region_ok = not (country and want_country) or country == want_country
    items.append(Item("timezone", tz_ok and region_ok, f"browser timezone {p.get('timezone')} (configured {cfg.timezone}); "
                      f"IP country {country or '?'}, profile region {want_country or '?'}",
                      [f"deploy/gcp/deploy.sh cli profile set-region {pid} {country or '<country>'}  # or move the proxy"]))  # fmt: skip
    win = p.get("window") or [0, 0, 0, 0]
    items.append(Item("window", bool(win[0] and win[1]), f"outer {win[0]}x{win[1]}, inner {win[2]}x{win[3]}, screen {p.get('screen')}"
                      + ("" if win[0] and win[1] else ": a zero-size window looks headless")))  # fmt: skip
    pinned = getattr(profile, "fingerprint", None) or {}
    pinned_major, now_major = _major(pinned.get("browserVersion")), _major(browser_version)
    items.append(Item("chrome-version", not (pinned_major and now_major and pinned_major != now_major),
                      f"Chrome {browser_version or '?'}" + (f" (pinned {pinned.get('browserVersion')}: major version changed)"
                      if pinned_major and now_major and pinned_major != now_major else ""),
                      [f"bump deliberately: deploy/gcp/deploy.sh cli profile doctor {pid} --repin"]))  # fmt: skip
    gl = (p.get("webgl") or {}).get("renderer") or "none"
    soft = any(s in gl for s in ("SwiftShader", "llvmpipe", "Software"))
    items.append(Item("webgl", True, f"{gl}" + (" (software rendering: honest, not spoofed; see browser-node mode)" if soft else "")))
    items.append(Item("plugins", True, f"{p.get('plugins')} plugins"))
    return items


class BrowserCheck:
    def __init__(self, ctx: Any):
        self.ctx = ctx

    def cached(self, profile: Any) -> dict[str, Any] | None:
        """The last result, if taken for the current proxy within 24 h."""
        r = self.ctx.prefs.get(f"browsercheck:{profile.id}")
        server = profile.config.proxy.server if profile.config.proxy else None
        if not r or r.get("proxy") != server or datetime.fromisoformat(r["checked_at"]) < datetime.now(UTC) - CHECK_TTL:
            return None
        return r

    def _save(self, profile: Any, items: list[Item], obs: dict[str, Any], egress: dict[str, Any], *, scope: str) -> dict[str, Any]:
        from dataclasses import asdict

        prev = self.ctx.prefs.get(f"browsercheck:{profile.id}") or {}
        rep = {
            "profile_id": profile.id, "scope": scope, "checked_at": iso(),
            "proxy": profile.config.proxy.server if profile.config.proxy else None,
            "ok": all(i.ok for i in items), "items": [asdict(i) for i in items],
            "observed": obs, "egress": {k: egress.get(k) for k in ("ip", "org", "asn", "country")},
        }  # fmt: skip
        if scope == "leaks" and prev.get("scope") == "full" and prev.get("proxy") == rep["proxy"]:
            # keep the full items, refresh the leak ones
            keep = [i for i in prev["items"] if i["name"] not in LEAK_ITEMS]
            rep = {**prev, "checked_at": rep["checked_at"], "items": rep["items"] + keep, "observed": {**prev["observed"], **obs}}
            rep["ok"] = all(i["ok"] for i in rep["items"])
        self.ctx.prefs.set(f"browsercheck:{profile.id}", rep)
        try:
            base = self.ctx.profiles.user_data_dir(profile).parent
            (base / "browser-check.json").write_text(json.dumps(rep, indent=2, default=str))
        except OSError:
            pass
        self.ctx.audit.record("browser.check", target=profile.id, scope=scope, ok=rep["ok"],
                              failed=[i["name"] for i in rep["items"] if not i["ok"]])  # fmt: skip
        return rep

    async def run(self, profile_id: str) -> dict[str, Any]:
        """Full check through the real launch path (CLI)."""
        ctx = self.ctx
        profile = ctx.profiles.get(profile_id)
        egress = await ctx.preflight.egress(profile, fresh=True)
        async with ctx.profiles.open(profile_id, purpose="browser-check", actor="cli") as op:
            page = await op.session.page()
            obs = await observe(page, ctx.settings)
            version = op.session.browser_version
            await page.goto("about:blank")
        items = evaluate(obs, ctx.profiles.get(profile_id), egress, browser_version=version)
        return self._save(profile, items, obs, egress, scope="full")

    async def leaks_in_session(self, profile: Any, page: Any) -> list[Item]:
        """Leak subset inside an already-open run, before the site is touched."""
        egress = await self.ctx.preflight.egress(profile)
        obs = await observe(page, self.ctx.settings, leaks_only=True)
        items = evaluate(obs, profile, egress, leaks_only=True)
        self._save(profile, items, obs, egress, scope="leaks")
        return items


def report_lines(rep: dict[str, Any]) -> list[str]:
    out = [f"browser-check {rep['profile_id']}: {'ALL CLEAR' if rep['ok'] else 'PROBLEMS'} ({rep['checked_at'][:16]}Z)"]
    for i in rep["items"]:
        out.append(f"  [{'ok' if i['ok'] else 'FAIL'}] {i['name']}: {i['detail']}")
        if not i["ok"]:
            out += [f"         fix: {f}" for f in i.get("fix") or []]
    return out + [""] + CANNOT_HELP
