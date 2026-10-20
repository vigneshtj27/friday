"""Preflight: refuse to spend a real sign-in attempt when its prerequisites are known to be missing.

Every bounced sign-in is a real login against a real account from whatever IP/browser the site
sees; repeated ones train bot defences and risk the account. So before a run signs in (login,
fetch_activity, fetch_statement, task), friday checks:

  1. driver      - the profile uses patchright (plain Playwright exposes navigator.webdriver)
  2. proxy       - a proxy is configured for the profile
  3. egress      - the IP the site sees, looked up THROUGH that proxy, is not a datacenter/hosting
                   network and is in the expected country
  4. identity    - the pinned fingerprint was taken under the current driver/locale/timezone/proxy
  5. otp         - how one-time codes reach friday (email sender + Gmail, SMS forwarder, or the
                   chat code card as the manual default)
  6. google      - connected, when the request also writes to Google (checked by the planner)

A failed item stops the run before the site is touched, posts one chat message naming the items and
the exact commands that fix them, and does not count as a bounce. `friday preflight <id> --override`
grants one deliberate attempt. Sites served from localhost (the mock bank) skip the network checks.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from friday.login.diagnosis import HOSTING_ORGS
from friday.util import iso

# Cloud / hosting ASNs (ipinfo's org is "AS<n> <name>"); HOSTING_ORGS catches the rest by name.
DATACENTER_ASNS = {
    15169, 396982, 19527, 36040, 139070,  # Google, Google Cloud
    16509, 14618, 8987,  # Amazon / AWS
    8075, 8068,  # Microsoft / Azure
    13335, 209242,  # Cloudflare
    14061, 24940, 16276, 63949, 20473, 31898, 45102, 37963, 132203, 51167, 60781, 9009, 212238, 54113, 46606,
}
GENERIC_HOSTING = re.compile(r"\bhosting\b|data ?cent(er|re)|\bvps\b|\bcolo(cation)?\b|\bserver(s)?\b", re.IGNORECASE)
LOCAL = {"localhost", "127.0.0.1", "::1"}
EGRESS_TTL = timedelta(minutes=15)
OVERRIDE_TTL = timedelta(minutes=30)
SIGNIN_KINDS = ("login", "fetch_activity", "fetch_statement", "task")


@dataclass
class Item:
    name: str
    ok: bool
    detail: str
    fix: list[str] = field(default_factory=list)


@dataclass
class Report:
    profile_id: str
    items: list[Item]
    checked_at: str
    override: bool = False

    @property
    def ok(self) -> bool:
        return all(i.ok for i in self.items)

    @property
    def failed(self) -> list[Item]:
        return [i for i in self.items if not i.ok]

    def public(self) -> dict[str, Any]:
        return {"profile_id": self.profile_id, "ok": self.ok, "checked_at": self.checked_at, "override": self.override,
                "items": [asdict(i) for i in self.items]}  # fmt: skip

    def lines(self) -> list[str]:
        out = [f"preflight {self.profile_id}: {'ALL CLEAR' if self.ok else 'BLOCKED'}"]
        for i in self.items:
            out.append(f"  [{'ok' if i.ok else 'FAIL'}] {i.name}: {i.detail}")
            if not i.ok:
                out += [f"         fix: {f}" for f in i.fix]
        return out

    def chat_message(self) -> str:
        bad = self.failed
        lines = [(f"I didn't try to sign in to {self.profile_id}: {len(bad)} prerequisite{'s' if len(bad) > 1 else ''} "
                  "missing, and every failed attempt counts against the account. Not counted as a bounce.")]  # fmt: skip
        for i in bad:
            lines.append(f"- {i.name}: {i.detail}")
            lines += [f"  fix: {f}" for f in i.fix]
        lines.append(f"Check again with: deploy/gcp/deploy.sh cli preflight {self.profile_id}")
        return "\n".join(lines)


def cli(args: str) -> str:
    """How the owner runs a friday command on the deployed server, from their laptop."""
    return f"deploy/gcp/deploy.sh cli {args}"


def parse_asn(org: str | None) -> int | None:
    m = re.match(r"\s*AS(\d+)", org or "")
    return int(m.group(1)) if m else None


def is_datacenter(geo: dict[str, Any]) -> bool:
    asn = parse_asn(geo.get("org"))
    org = str(geo.get("org") or "")
    return (asn is not None and asn in DATACENTER_ASNS) or bool(HOSTING_ORGS.search(org) or GENERIC_HOSTING.search(org))


def expected_country(profile: Any) -> str | None:
    loc = profile.config.locale or ""
    if "-" in loc and len(loc.split("-")[-1]) == 2:
        return loc.split("-")[-1].upper()
    return {"Asia/Kolkata": "IN", "Asia/Calcutta": "IN"}.get(profile.config.timezone or "")


def otp_channel(ctx: Any, site: Any) -> dict[str, Any]:
    """How codes reach friday for a site: email (sender + Gmail), sms (forwarder), else the chat card."""
    if site.otp_senders and ctx.gmail.configured:
        return {"channel": "email", "live": True, "detail": f"Gmail, from {', '.join(site.otp_senders)}"}
    if ctx.settings.sms_webhook_token:
        seen = ctx.db.query_one("SELECT ts FROM audit_log WHERE action = 'hook.sms' ORDER BY id DESC LIMIT 1")
        return {"channel": "sms", "live": seen is not None,
                "detail": f"SMS forwarder at /hooks/sms ({'last message ' + seen['ts'][:16] if seen else 'no message received yet'})"}  # fmt: skip
    return {"channel": "chat", "live": True, "detail": "you type the code into the chat card (manual)"}


class Preflight:
    def __init__(self, ctx: Any, *, fetch_geo: Any = None):
        self.ctx = ctx
        self._fetch_geo = fetch_geo or self._geo_via

    async def _geo_via(self, proxy_url: str | None) -> dict[str, Any]:
        url = self.ctx.settings.geoip_url or "https://ipinfo.io/json"
        async with httpx.AsyncClient(timeout=20, proxy=proxy_url) as http:
            r = await http.get(url)
            r.raise_for_status()
            raw = r.json()
        return {k: raw.get(k) for k in ("ip", "org", "country", "city", "timezone")}

    def _proxy_url(self, profile: Any) -> str:
        p = self.ctx.profiles._proxy(profile)
        if p is None:
            raise ValueError("no proxy")
        if not p.server.startswith(("http://", "https://")):
            raise ValueError(f"can't check egress through {p.server.split(':')[0]} proxies (use http)")
        if p.username:
            scheme, rest = p.server.split("://", 1)
            from urllib.parse import quote

            return f"{scheme}://{quote(p.username, safe='')}:{quote(p.password or '', safe='')}@{rest}"
        return p.server

    async def egress(self, profile: Any, *, fresh: bool = False) -> dict[str, Any]:
        """The egress the site would see for this profile (cached 15 min per proxy)."""
        key = f"egress:{profile.id}"
        server = profile.config.proxy.server if profile.config.proxy else None
        cached = self.ctx.prefs.get(key)
        if cached and not fresh and cached.get("proxy") == server and \
                datetime.fromisoformat(cached["checked_at"]) > datetime.now(UTC) - EGRESS_TTL:  # fmt: skip
            return cached
        try:
            geo = await self._fetch_geo(self._proxy_url(profile) if server else None)
            out = {**geo, "asn": parse_asn(geo.get("org")), "datacenter": is_datacenter(geo)}
        except Exception as e:
            out = {"error": f"{type(e).__name__}: {e}"[:200]}
        out.update(proxy=server, checked_at=iso())
        self.ctx.prefs.set(key, out)
        return out

    async def check(self, profile_id: str, *, need_google: bool = False, fresh: bool = False) -> Report:
        ctx = self.ctx
        profile = ctx.profiles.get(profile_id)
        site = ctx.sites.get(profile.site_id)
        items: list[Item] = []
        local = bool(site) and all(d in LOCAL for d in site.all_domains)
        driver = ctx.profiles.effective_driver(profile.config)
        if local:
            items.append(Item("network", True, "local test site: driver, proxy and egress checks skipped"))
        else:
            items.append(Item("driver", driver == "patchright", f"{driver}" + ("" if driver == "patchright" else
                              " (exposes navigator.webdriver = true to the site)"),
                              [cli(f"profile set-driver {profile_id} patchright")]))  # fmt: skip
            has_proxy = profile.config.proxy is not None
            items.append(Item("proxy", has_proxy, profile.config.proxy.server if has_proxy else "none: the site sees the server's datacenter IP",
                              [f"deploy/egress.sh {profile_id} <home-tailscale-ip>"]))  # fmt: skip
            if has_proxy:
                eg = await self.egress(profile, fresh=fresh)
                want = expected_country(profile)
                if eg.get("error"):
                    items.append(Item("egress", False, f"couldn't look up the IP through the proxy ({eg['error']})",
                                      ["start the home proxy: friday egress-proxy --listen <home-tailscale-ip>:3128", f"deploy/egress.sh {profile_id} <home-tailscale-ip>"]))  # fmt: skip
                else:
                    bad = eg["datacenter"] or (want is not None and (eg.get("country") or "").upper() != want)
                    detail = f"{eg.get('ip')} {eg.get('org')} ({eg.get('country')})"
                    if eg["datacenter"]:
                        detail += ": a datacenter/hosting network"
                    elif bad:
                        detail += f": expected an IP in {want}"
                    items.append(Item("egress", not bad, detail, [f"route {profile_id} through a residential ISP in {want or 'your country'}: deploy/egress.sh {profile_id} <home-tailscale-ip>"]))
            else:
                eg = await self.egress(profile, fresh=fresh)  # the server's own IP, which the site sees today
                seen = f"{eg.get('ip')} {eg.get('org')} ({eg.get('country')})" if not eg.get("error") else "the server's own IP"
                items.append(Item("egress", False, f"no proxy, so the site sees {seen}",
                                  [f"deploy/egress.sh {profile_id} <home-tailscale-ip>"]))  # fmt: skip
        from friday.profiles.doctor import examine

        doc = examine(ctx.profiles, profile)
        if not doc.pinned:
            items.append(Item("identity", True, "not pinned yet: the first sign-in pins it under the current config"))
        else:
            items.append(Item("identity", not doc.issues, "pin matches the config" if not doc.issues else "; ".join(doc.issues),
                              [cli(f"profile doctor {profile_id} --repin")]))  # fmt: skip
        ch = otp_channel(ctx, site) if site else {"channel": "chat", "detail": "chat card"}
        note = "" if ch["channel"] != "chat" else (" - hands-free options: email sender + Gmail "
                                                   f"({cli(f'site set-otp-senders {profile.site_id} <sender>')}, then 'connect my Gmail' in the chat) "
                                                   "or an SMS forwarder to /hooks/sms")  # fmt: skip
        items.append(Item("otp", True, f"{ch['channel']}: {ch['detail']}{note}"))
        if need_google:
            g = ctx.google.can("sheets")
            items.append(Item("google", g, "connected" if g else "not connected", ["say 'connect Google' in the chat and tap the link"]))
        rep = Report(profile_id, items, iso())
        ctx.prefs.set(f"preflight:{profile_id}", rep.public())
        return rep

    # ---- one deliberate attempt -------------------------------------------------------

    def grant_override(self, profile_id: str, *, actor: str = "cli") -> str:
        until = datetime.now(UTC) + OVERRIDE_TTL
        self.ctx.prefs.set(f"preflight_override:{profile_id}", iso(until))
        self.ctx.audit.record("preflight.override_granted", actor=actor, target=profile_id, until=iso(until))
        return iso(until)

    def take_override(self, profile_id: str) -> bool:
        key = f"preflight_override:{profile_id}"
        until = self.ctx.prefs.get(key)
        if not until:
            return False
        self.ctx.prefs.set(key, None)  # one attempt
        if datetime.fromisoformat(until) < datetime.now(UTC):
            return False
        self.ctx.audit.record("preflight.override_used", actor="owner", target=profile_id)
        return True
