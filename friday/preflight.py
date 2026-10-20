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

import hmac
import json
import re
import secrets
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


def is_local_site(site: Any) -> bool:
    """The mock bank and other test sites on this machine: no network identity to check."""
    return bool(site) and all(d in LOCAL for d in site.all_domains)


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
        local = is_local_site(site)
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
            items.append(self._browser_item(profile))
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

    def _browser_item(self, profile: Any) -> Item:
        """The last browser self-check (IPv4/IPv6/WebRTC leaks and what the page sees) for this proxy."""
        bc = getattr(self.ctx, "browsercheck", None)
        r = bc.cached(profile) if bc else None
        if r is None:
            return Item("browser", True, "no browser self-check for this proxy in the last 24 h: the leak check runs in the "
                        f"browser before the sign-in (full check: {cli(f'browser-check {profile.id}')})")  # fmt: skip
        bad = [i for i in r["items"] if not i["ok"]]
        if not bad:
            return Item("browser", True, f"self-check ({r['scope']}, {r['checked_at'][:16]}Z): no leaks, page sees a consistent browser")
        return Item("browser", False, "; ".join(f"{i['name']}: {i['detail']}" for i in bad),
                    sorted({f for i in bad for f in i.get("fix") or []}) + [cli(f"browser-check {profile.id}")])  # fmt: skip

    # ---- one deliberate attempt -------------------------------------------------------
    #
    # A preflight override is a single-use, 30-minute, profile-scoped exception the owner grants
    # on purpose (CLI --override, or the portal confirmation). It does NOT skip any check: the
    # report is still computed and still fails; it only lets exactly one eligible sign-in run go
    # ahead despite that report. The grant is stored in prefs as JSON; the consume is atomic so two
    # racing runs can never both use one grant. A legacy grant (a bare ISO string from an older
    # build) is still honoured.

    @staticmethod
    def _override_key(profile_id: str) -> str:
        return f"preflight_override:{profile_id}"

    @staticmethod
    def _parse_grant(raw: Any) -> dict[str, Any] | None:
        """Normalise stored grant JSON (new dict form or legacy bare ISO string) to a dict."""
        if raw is None:
            return None
        if isinstance(raw, str):  # legacy: just the expiry
            return {"id": None, "until": raw, "actor": "owner", "failed": []}
        if isinstance(raw, dict) and raw.get("until"):
            return raw
        return None

    def override_status(self, profile_id: str) -> dict[str, Any] | None:
        """Read the current grant without consuming it. None when there is none or it expired."""
        g = self._parse_grant(self.ctx.prefs.get(self._override_key(profile_id)))
        if g is None:
            return None
        try:
            expired = datetime.fromisoformat(g["until"]) < datetime.now(UTC)
        except (ValueError, TypeError):
            return None
        if expired:
            return None
        return {"id": g.get("id"), "until": g["until"], "actor": g.get("actor", "owner"), "failed": g.get("failed") or []}

    def grant_override(self, profile_id: str, *, actor: str = "cli", failed: list[str] | None = None) -> str:
        """Issue a fresh single-use grant. Server sets the id, expiry (now + OVERRIDE_TTL) and time;
        the caller supplies only who asked and which checks were failing when shown."""
        until = datetime.now(UTC) + OVERRIDE_TTL
        grant = {"id": secrets.token_hex(8), "until": iso(until), "actor": actor,
                 "failed": list(failed or []), "granted_at": iso()}  # fmt: skip
        self.ctx.prefs.set(self._override_key(profile_id), grant)
        self.ctx.audit.record("preflight.override_granted", actor=actor, target=profile_id,
                              grant=grant["id"], until=grant["until"], failed=grant["failed"])  # fmt: skip
        return grant["until"]

    def take_override(self, profile_id: str, *, run_id: str | None = None) -> bool:
        """Consume one grant for a run, atomically. Returns True only if a live grant existed and
        this caller is the one that removed it; the delete + audit commit together, so concurrent
        callers across connections yield exactly one winner. Fails closed on malformed/expired."""
        key = self._override_key(profile_id)
        db = self.ctx.db
        with db.tx() as c:
            row = c.execute("SELECT value_json FROM prefs WHERE key = ?", (key,)).fetchone()
            if row is None:
                return False
            # Delete first and require we removed the row: a racing consumer's DELETE then finds
            # nothing. One-use is enforced here, not by any in-memory flag.
            if c.execute("DELETE FROM prefs WHERE key = ?", (key,)).rowcount != 1:
                return False
            try:
                g = self._parse_grant(json.loads(row[0]))
                live = g is not None and datetime.fromisoformat(g["until"]) >= datetime.now(UTC)
            except (ValueError, TypeError):
                g, live = None, False
            if not live:
                return False  # expired/malformed grant is now cleared; fail closed
            self.ctx.audit.record_c(c, "preflight.override_used", actor="owner", target=profile_id,
                                    grant=(g or {}).get("id"), run=run_id, failed=(g or {}).get("failed") or [])  # fmt: skip
            return True

    # ---- portal flow: a fresh check, then a deliberate owner grant ----------------------

    @staticmethod
    def _confirm_key(profile_id: str) -> str:
        return f"preflight_confirm:{profile_id}"

    def _signature(self, profile_id: str, rep: Report) -> str:
        """What the owner is confirming: the profile's driver/proxy and the exact set of failed
        checks. If any of this changes before they confirm, the old confirmation is stale."""
        p = self.ctx.profiles.get(profile_id)
        return "|".join([
            self.ctx.profiles.effective_driver(p.config),
            p.config.proxy.server if p.config.proxy else "none",
            ",".join(sorted(i.name for i in rep.failed)),
        ])  # fmt: skip

    def _sanitized_egress(self, profile_id: str) -> dict[str, Any] | None:
        eg = self.ctx.prefs.get(f"egress:{profile_id}") or {}
        if not eg:
            return None
        # server-only safe fields; the proxy server string carries no credentials by model design
        return {k: eg.get(k) for k in ("ip", "org", "asn", "country", "datacenter", "checked_at", "proxy") if k in eg}

    async def portal_check(self, profile_id: str, *, fresh: bool = True) -> dict[str, Any]:
        """Run a fresh preflight (no site opened, no login) for the Accounts view, and mint a
        single-use confirmation token bound to the current report. Reading this never grants."""
        p = self.ctx.profiles.get(profile_id)  # raises ProfileNotFound -> 404 at the endpoint
        need_google = False
        rep = await self.check(profile_id, need_google=need_google, fresh=fresh)
        token = secrets.token_hex(16)
        sig = self._signature(profile_id, rep)
        self.ctx.prefs.set(self._confirm_key(profile_id),
                           {"token": token, "sig": sig, "issued_at": iso()})  # fmt: skip
        return {
            "profile_id": profile_id,
            "effective_driver": self.ctx.profiles.effective_driver(p.config),
            "report": rep.public(),
            "egress": self._sanitized_egress(profile_id),
            "override": self.override_status(profile_id),
            "pending_signin": self.ctx.jobs.has_pending_signin(profile_id) if self.ctx.jobs else None,
            "confirm_token": token,  # never logged; returned only to this authenticated response
        }

    async def portal_grant(self, profile_id: str, *, confirm_token: str, confirm_profile_id: str) -> dict[str, Any]:
        """Validate the owner's confirmation and, only if the checks still fail and nothing material
        changed, issue one grant. Returns a dict describing the outcome; the endpoint maps the
        'status' to an HTTP code. Never grants merely because the page was opened."""
        p = self.ctx.profiles.get(profile_id)  # ProfileNotFound -> 404
        if confirm_profile_id != profile_id:
            return {"status": "mismatch"}  # 400: typed id / dialog came from another profile
        stored = self.ctx.prefs.get(self._confirm_key(profile_id))
        if not stored or not confirm_token or not hmac.compare_digest(str(stored.get("token", "")), confirm_token):
            return {"status": "stale", "reason": "confirmation expired or not recognised; re-check and try again"}
        # Refuse while a sign-in is queued/running: that job could consume the grant unexpectedly.
        pending = self.ctx.jobs.has_pending_signin(profile_id) if self.ctx.jobs else None
        if pending:
            return {"status": "busy", "run_id": pending}
        rep = await self.check(profile_id, fresh=True)
        if rep.ok:
            self.ctx.prefs.set(self._confirm_key(profile_id), None)
            return {"status": "all_clear"}  # nothing to override; no grant created
        if self._signature(profile_id, rep) != stored.get("sig"):
            # the report or the profile's driver/proxy changed since it was shown: force a re-decision
            return {"status": "changed", "report": rep.public(),
                    "effective_driver": self.ctx.profiles.effective_driver(p.config)}  # fmt: skip
        self.ctx.prefs.set(self._confirm_key(profile_id), None)  # one-use confirmation
        until = self.grant_override(profile_id, actor="owner", failed=[i.name for i in rep.failed])
        return {"status": "granted", "override": self.override_status(profile_id), "until": until}

    def clear_override(self, profile_id: str, *, actor: str = "owner") -> bool:
        """Cancel an un-consumed grant (owner changed their mind)."""
        key = self._override_key(profile_id)
        if self.ctx.prefs.get(key) is None:
            return False
        self.ctx.prefs.set(key, None)
        self.ctx.audit.record("preflight.override_cancelled", actor=actor, target=profile_id)
        return True
