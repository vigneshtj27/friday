"""Diagnose SILENT_BOUNCE / ACCESS_DENIED_OR_BLOCKED instead of guessing.

1. capture_in_session: final URL, recent navigation statuses, console errors, failed
   requests, Set-Cookie presence, masked screenshot, visible text, fingerprint check
   and egress IP geolocation (via the profile's own network path).
2. Experiments, one variable at a time, each logged:
     headless   - same profile, headless instead of headed
     no_proxy / alt_proxy - without the profile's proxy, or through an alternate egress
     chromium   - Playwright's bundled Chromium instead of Google Chrome
     fresh      - a throwaway profile instead of the persistent one
     manual     - the owner signs in by hand in the same profile (remote view)
   "probe" experiments only load the login page (no credentials, no lockout risk).
   "submit" experiments submit credentials once and are only run when the owner asks.
3. rank_causes: a ranked list of likely causes with evidence; anything not isolated
   by an experiment is marked unconfirmed. Suggests the cheapest next experiment.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
from typing import Any

from friday.agent.snapshot import take_snapshot
from friday.login.classifier import LoginState, Observation, classify
from friday.util import iso

log = logging.getLogger(__name__)

FINGERPRINT_JS = r"""
async () => {
  const r = {};
  r.webdriver = navigator.webdriver;
  r.userAgent = navigator.userAgent;
  r.headlessUA = /Headless/i.test(navigator.userAgent);
  r.platform = navigator.platform;
  r.language = navigator.language;
  r.languages = Array.from(navigator.languages || []);
  r.hardwareConcurrency = navigator.hardwareConcurrency;
  r.deviceMemory = navigator.deviceMemory ?? null;
  r.plugins = navigator.plugins ? navigator.plugins.length : null;
  r.chromeObject = typeof window.chrome === 'object';
  r.timezone = Intl.DateTimeFormat().resolvedOptions().timeZone;
  r.tzOffsetMin = new Date().getTimezoneOffset();
  r.screen = [screen.width, screen.height, screen.availWidth, screen.availHeight, screen.colorDepth];
  r.window = [outerWidth, outerHeight, innerWidth, innerHeight];
  try {
    const gl = document.createElement('canvas').getContext('webgl');
    const ext = gl.getExtension('WEBGL_debug_renderer_info');
    r.webglVendor = gl.getParameter(ext.UNMASKED_VENDOR_WEBGL);
    r.webglRenderer = gl.getParameter(ext.UNMASKED_RENDERER_WEBGL);
  } catch (e) { r.webglError = String(e); }
  try {
    const p = await navigator.permissions.query({name: 'notifications'});
    r.notificationPermission = Notification.permission;
    r.notificationQueryState = p.state;
  } catch (e) {}
  r.automationGlobals = Object.getOwnPropertyNames(window).filter(k => /^(cdc_|\$cdc_|__playwright|__pw|__puppeteer|_selenium|callPhantom|_phantom|__nightmare|domAutomation)/.test(k));
  r.uaBrands = navigator.userAgentData ? navigator.userAgentData.brands.map(b => b.brand) : null;
  return r;
}
"""
HOSTING_ORGS = re.compile(
    r"amazon|aws|google|microsoft|azure|digitalocean|hetzner|ovh|linode|akamai|vultr|oracle|contabo|"
    r"scaleway|alibaba|tencent|choopa|leaseweb|m247|datacamp|hostinger|ionos|cloudflare|fly\.io|render",
    re.IGNORECASE,
)
EXPERIMENTS = {
    "headless": "same profile, headless instead of headed",
    "no_proxy": "same profile without the configured proxy",
    "alt_proxy": "same profile through the alternate egress (FRIDAY_DIAG_ALT_PROXY)",
    "chromium": "Playwright's bundled Chromium instead of Google Chrome",
    "fresh": "a fresh throwaway profile instead of the persistent one",
    "manual": "the owner signs in by hand in the same profile",
}


def fingerprint_findings(fp: dict[str, Any], geo: dict[str, Any] | None) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []

    def add(sev: str, finding: str) -> None:
        out.append({"severity": sev, "finding": finding})

    if fp.get("webdriver"):
        add("high", "navigator.webdriver is true: the page can see the browser is automated")
    if fp.get("headlessUA"):
        add("high", "user agent says HeadlessChrome")
    if fp.get("automationGlobals"):
        add("high", f"automation globals on window: {fp['automationGlobals']}")
    win = fp.get("window") or [1, 1]
    if win and win[0] == 0:
        add("high", "outerWidth is 0 (headless tell)")
    if fp.get("plugins") == 0:
        add("medium", "no browser plugins reported (PDF viewer missing is unusual for desktop Chrome)")
    renderer = str(fp.get("webglRenderer") or "")
    if re.search(r"swiftshader|llvmpipe|software", renderer, re.I):
        add("medium", f"software WebGL renderer ({renderer}); typical of servers/VMs, rare on real desktops")
    if fp.get("webglError"):
        add("medium", "WebGL unavailable")
    if fp.get("notificationPermission") == "denied" and fp.get("notificationQueryState") == "prompt":
        add("medium", "Notification permission inconsistent with permissions API (known headless tell)")
    if not fp.get("languages"):
        add("medium", "navigator.languages is empty")
    if fp.get("uaBrands") is not None and not any("Google Chrome" in b for b in fp["uaBrands"]):
        add("info", f"UA brands {fp['uaBrands']} do not include Google Chrome (Chromium build?)")
    if geo:
        if geo.get("timezone") and fp.get("timezone") and geo["timezone"] != fp["timezone"]:
            from friday.profiles.manager import same_timezone

            if not same_timezone(geo["timezone"], fp["timezone"]):
                add("high", f"browser timezone {fp['timezone']} does not match egress IP timezone {geo['timezone']}")
        if HOSTING_ORGS.search(str(geo.get("org", ""))):
            add("high", f"egress IP belongs to a hosting/datacenter network ({geo.get('org')})")
        lang_country = (fp.get("language") or "").split("-")[-1].upper()
        if geo.get("country") and len(lang_country) == 2 and lang_country != geo["country"].upper():
            add("info", f"browser language region {lang_country} differs from IP country {geo['country']}")
    return out


def rank_causes(report: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
    """Turn evidence + experiments into ranked causes. Pure function (tested directly)."""
    trigger = report.get("trigger_state")
    cap = report.get("capture") or {}
    findings = report.get("fingerprint_findings") or []
    exps = {e["name"]: e for e in report.get("experiments") or [] if e.get("status") == "done"}
    baseline_bad = trigger in (LoginState.SILENT_BOUNCE.value, LoginState.ACCESS_DENIED_OR_BLOCKED.value)
    good = {LoginState.LOGIN_FORM.value, LoginState.LOGGED_IN.value}

    def worked(name: str) -> bool | None:
        e = exps.get(name)
        if not e:
            return None
        return e.get("result_state") in good if e.get("mode") == "probe" else e.get("result_state") == LoginState.LOGGED_IN.value

    causes: dict[str, dict[str, Any]] = {
        k: {"cause": k, "score": 0.0, "evidence": [], "confirmed": False}
        for k in ("automation_fingerprint", "ip_reputation", "new_device_distrust", "session_conflict", "bad_credentials", "site_outage")
    }

    def ev(k: str, pts: float, text: str) -> None:
        causes[k]["score"] += pts
        causes[k]["evidence"].append(text)

    for f in findings:
        if f["severity"] == "high" and re.search(r"webdriver|Headless|automation|outerWidth", f["finding"]):
            ev("automation_fingerprint", 2, f["finding"])
        elif f["severity"] == "medium" and re.search(r"WebGL|plugins|Notification|languages", f["finding"]):
            ev("automation_fingerprint", 0.5, f["finding"])
        if re.search(r"datacenter|timezone .* does not match", f["finding"]):
            ev("ip_reputation", 2, f["finding"])
    statuses = [n.get("status") for n in cap.get("navigations", [])]
    if any(s and s >= 500 for s in statuses):
        ev("site_outage", 2, f"server errors in recent navigations: {statuses}")
    if len(cap.get("failed_requests", [])) >= 5:
        ev("site_outage", 0.5, f"{len(cap['failed_requests'])} failed requests")
    if any(s in (403, 429) for s in statuses):
        ev("ip_reputation", 1, f"HTTP 403/429 seen: {statuses}")
        ev("automation_fingerprint", 1, f"HTTP 403/429 seen: {statuses}")
    if trigger == LoginState.SILENT_BOUNCE.value:
        submit_cookie = [n for n in cap.get("navigations", []) if n.get("set_cookie")]
        if not submit_cookie:
            ev("session_conflict", 0.5, "no Set-Cookie on recent document responses")
        ev("bad_credentials", 0.3, "a bounce can hide a credential problem on some sites (no error shown)")
        ev("automation_fingerprint", 0.5, "silent bounces are a common bot-defence response")
    if report.get("profile_status") == "new":
        ev("new_device_distrust", 1.5, "profile was never seeded by a manual login")
    if report.get("previous_rejections"):
        ev("bad_credentials", 2, "credentials were explicitly rejected before")

    # Experiments isolate one variable each. A manual sign-in only separates "automation signature"
    # from "IP/account/site" when the browser itself doesn't carry the automation flag: under plain
    # Playwright (or a pin with navigator.webdriver = true) the human drives the same flagged browser.
    env = report.get("environment") or {}
    confounded = bool(env) and (env.get("driver") != "patchright" or env.get("pinned_webdriver") is True)
    m = worked("manual")
    if confounded and "manual" in exps:
        exps["manual"]["verdict"] = "INCONCLUSIVE"
        for k in ("automation_fingerprint", "ip_reputation"):
            causes[k]["evidence"].append(
                f"manual sign-in ({exps['manual'].get('result_state')}) is INCONCLUSIVE: the remote-view browser ran under "
                f"{env.get('driver')} with navigator.webdriver {'true' if env.get('pinned_webdriver') else 'unknown'}, "
                "so it can't separate automation signature from IP"
            )
        m = None
    if env.get("egress", {}).get("datacenter"):
        ev("ip_reputation", 1, f"egress {env['egress'].get('ip')} is a datacenter network ({env['egress'].get('org')})")
    if m is True:
        ev("automation_fingerprint", 4, "manual login in the same profile works while the agent's does not")
        causes["automation_fingerprint"]["confirmed"] = True
        for k in ("ip_reputation", "bad_credentials", "site_outage"):
            causes[k]["score"] -= 2
            causes[k]["evidence"].append("manual login from the same IP/profile works")
    elif m is False:
        for k in ("ip_reputation", "bad_credentials", "site_outage"):
            ev(k, 1.5, "manual login in the same profile also fails")
        causes["automation_fingerprint"]["score"] -= 2
        causes["automation_fingerprint"]["evidence"].append("manual login also fails, so automation is not the (only) cause")
    if baseline_bad:
        if worked("headless") is True:
            ev("automation_fingerprint", 0.5, "headless probe behaved differently from headed (unexpected; check)")
        for name in ("no_proxy", "alt_proxy"):
            if worked(name) is True:
                ev("ip_reputation", 3, f"changing egress ({name}) fixed it")
                causes["ip_reputation"]["confirmed"] = exps[name].get("mode") == "submit"
            elif worked(name) is False:
                ev("ip_reputation", -1, f"changing egress ({name}) did not help")
        if worked("fresh") is True:
            ev("session_conflict", 2, "a fresh profile works while the persistent one does not")
            causes["session_conflict"]["confirmed"] = exps["fresh"].get("mode") == "submit"
        elif worked("fresh") is False:
            ev("new_device_distrust", 0.5, "fresh profile also fails")
        if worked("chromium") is not None and worked("chromium") != (exps.get("baseline", {}).get("result_state") in good):
            ev("automation_fingerprint", 1, "bundled Chromium behaves differently from Google Chrome")

    ranked = sorted(causes.values(), key=lambda c: c["score"], reverse=True)
    for c in ranked:
        c["score"] = round(c["score"], 2)
        c["likelihood"] = "high" if c["score"] >= 3 else "medium" if c["score"] >= 1.5 else "low"
        if not c["confirmed"]:
            c["note"] = "unconfirmed: not isolated by an experiment"
    ranked = [c for c in ranked if c["score"] > 0] or ranked[:1]

    if confounded:
        pid = report.get("profile_id") or "<profile>"
        return ranked, (
            f"Fix the prerequisites first: run `deploy/gcp/deploy.sh cli preflight {pid}` (patchright driver, home egress, re-pin). "
            "Under the current driver/pin a manual sign-in through the remote view is INCONCLUSIVE; after a patchright "
            "re-pin it discriminates automation signature vs IP."
        )
    order = ["manual", "fresh", "no_proxy", "alt_proxy", "headless", "chromium"]
    done = set(exps) | {e["name"] for e in report.get("experiments") or [] if e.get("status") == "skipped"}
    nxt = next((n for n in order if n not in done), None)
    hint = {
        "manual": "Sign in by hand in the same profile via the remote view (no agent, no lockout risk). If it works, the cause is the automation signature; if not, IP/account/site.",
        "fresh": "Probe the login page with a fresh profile.",
        "no_proxy": "Probe without the proxy.",
        "alt_proxy": "Probe through the alternate egress (set FRIDAY_DIAG_ALT_PROXY, e.g. your home connection).",
        "headless": "Probe the same profile headless.",
        "chromium": "Probe with bundled Chromium (needs `playwright install chromium`).",
    }
    return ranked, hint.get(nxt or "", "All experiments done. Review the evidence, or try a submit experiment from the portal.")


class Diagnoser:
    def __init__(self, settings: Any, db: Any, audit: Any, files: Any, profiles: Any, handoffs: Any, redactor: Any, notifier: Any = None):
        self.settings = settings
        self.db = db
        self.audit = audit
        self.files = files
        self.profiles = profiles
        self.handoffs = handoffs
        self.redactor = redactor
        self.notifier = notifier

    # ---- persistence ----------------------------------------------------

    def create(self, profile_id: str, run_id: str | None, trigger_state: str, data: dict[str, Any]) -> str:
        rid = secrets.token_hex(6)
        profile = self.profiles.get(profile_id)
        rejections = self.db.query_one(
            "SELECT COUNT(*) FROM run_steps s JOIN runs r ON r.id = s.run_id WHERE r.profile_id = ? AND s.state = ?",
            (profile_id, LoginState.CREDENTIALS_REJECTED.value),
        )[0]
        report = {
            "id": rid,
            "profile_id": profile_id,
            "run_id": run_id,
            "trigger_state": trigger_state,
            "profile_status": profile.status,
            "previous_rejections": rejections,
            "created_at": iso(),
            "environment": self.environment(profile),
            **data,
            "experiments": data.get("experiments", []),
        }
        report["causes"], report["next_experiment"] = rank_causes(report)
        self.db.execute(
            "INSERT INTO diagnosis_reports (id, run_id, profile_id, trigger_state, report_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (rid, run_id, profile_id, trigger_state, json.dumps(report, default=str), report["created_at"]),
        )
        self.audit.record("diagnosis.create", target=profile_id, report=rid, trigger=trigger_state)
        return rid

    def environment(self, profile: Any) -> dict[str, Any]:
        """What the site saw from us: driver, the pinned automation flag, and the egress (from the last preflight)."""
        fp = profile.fingerprint or {}
        row = self.db.query_one("SELECT value_json FROM prefs WHERE key = ?", (f"egress:{profile.id}",))
        eg = json.loads(row["value_json"]) if row else {}
        return {
            "driver": self.profiles.effective_driver(profile.config),
            "pinned_driver": fp.get("driver"),
            "pinned_webdriver": fp.get("webdriver"),
            "proxy": profile.config.proxy.server if profile.config.proxy else None,
            "egress": {k: eg.get(k) for k in ("ip", "org", "asn", "country", "datacenter", "checked_at") if k in eg},
        }

    def get(self, report_id: str) -> dict[str, Any] | None:
        r = self.db.query_one("SELECT report_json FROM diagnosis_reports WHERE id = ?", (report_id,))
        return json.loads(r[0]) if r else None

    def list(self, profile_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        sql = "SELECT report_json FROM diagnosis_reports"
        params: list[Any] = []
        if profile_id:
            sql += " WHERE profile_id = ?"
            params.append(profile_id)
        rows = self.db.query(sql + " ORDER BY created_at DESC LIMIT ?", [*params, limit])
        return [json.loads(r[0]) for r in rows]

    def _save(self, report: dict[str, Any]) -> None:
        report["causes"], report["next_experiment"] = rank_causes(report)
        self.db.execute(
            # Free-text evidence was redacted at capture time; ids and states must stay intact.
            "UPDATE diagnosis_reports SET report_json = ? WHERE id = ?", (json.dumps(report, default=str), report["id"])
        )

    def add_experiment(self, report_id: str, exp: dict[str, Any]) -> dict[str, Any]:
        report = self.get(report_id)
        if report is None:
            raise KeyError(report_id)
        exp = {"at": iso(), **exp}
        report["experiments"] = [e for e in report.get("experiments", []) if not (e["name"] == exp["name"] and e.get("mode") == exp.get("mode"))]
        report["experiments"].append(exp)
        self._save(report)
        self.audit.record("diagnosis.experiment", target=report["profile_id"], report=report_id, **{k: exp.get(k) for k in ("name", "mode", "status", "result_state")})
        return report

    # ---- evidence -------------------------------------------------------

    async def fingerprint(self, context: Any) -> tuple[dict[str, Any], dict[str, Any] | None]:
        page = await context.new_page()  # about:blank in the same context: same browser identity
        try:
            fp = await page.evaluate(FINGERPRINT_JS)
        finally:
            await page.close()
        geo = None
        if self.settings.geoip_url:
            try:
                resp = await context.request.get(self.settings.geoip_url, timeout=15000)
                if resp.ok:
                    raw = await resp.json()
                    geo = {k: raw.get(k) for k in ("country", "region", "city", "org", "timezone")}
                    geo["ip_seen"] = bool(raw.get("ip"))
            except Exception as e:
                geo = {"error": type(e).__name__}
        return fp, geo

    async def capture_in_session(self, ex: Any, recorder: Any, state: LoginState) -> dict[str, Any]:
        page = ex.page
        snap = await take_snapshot(page)
        shot = await ex._masked_screenshot("diagnosis")
        fp, geo = await self.fingerprint(ex.context)
        cookies = await ex.context.cookies()
        return {
            "capture": {
                "final_url": self.redactor.redact(page.url),
                "title": snap.title,
                "visible_text": self.redactor.redact(snap.text[:2000]),
                "screenshot_file_id": shot,
                "cookie_count": len(cookies),
                "blocked_navigations": ex.blocked_navigations[-5:],
                **recorder.snapshot(),
            },
            "fingerprint": self.redactor.redact_obj(fp),
            "geo": geo,
            "fingerprint_findings": fingerprint_findings(fp, geo),
            "state_at_capture": state.value,
        }

    # ---- experiments ----------------------------------------------------

    async def probe(self, report_id: str, name: str) -> dict[str, Any]:
        """Load the login page under one changed variable. No credentials are submitted."""
        report = self.get(report_id)
        if report is None:
            raise KeyError(report_id)
        profile = self.profiles.get(report["profile_id"])
        kwargs: dict[str, Any] = {}
        if name == "headless":
            kwargs["headless"] = True
        elif name == "no_proxy":
            if not profile.config.proxy:
                return self.add_experiment(report_id, {"name": name, "mode": "probe", "status": "skipped", "notes": "profile has no proxy configured"})
            kwargs["proxy_mode"] = "none"
        elif name == "alt_proxy":
            if not self.settings.diag_alt_proxy:
                return self.add_experiment(report_id, {"name": name, "mode": "probe", "status": "skipped", "notes": "FRIDAY_DIAG_ALT_PROXY not set"})
            kwargs["proxy_mode"] = "alt"
        elif name == "chromium":
            kwargs["channel"] = "chromium"
        elif name == "fresh":
            kwargs["fresh"] = True
        elif name != "baseline":
            raise ValueError(f"not a probe experiment: {name}")
        site = self.profiles._sites.get(profile.site_id)
        url = site.login_url or f"https://{site.domains[0]}/"
        try:
            async with self.profiles.open(profile.id, purpose=f"diagnosis:{name}", with_vnc=False, **kwargs) as op:
                page = await op.session.page()
                status = None
                try:
                    resp = await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                    status = resp.status if resp else None
                    try:
                        await page.wait_for_load_state("networkidle", timeout=8000)
                    except Exception:
                        pass
                    obs = Observation.from_snapshot(await take_snapshot(page), status=status)
                except Exception as e:
                    obs = Observation(page.url, "", "", [], load_error=type(e).__name__)
                c = classify(obs)
                webdriver = None
                try:
                    from friday.agent import isolated

                    webdriver = await isolated.evaluate(page.main_frame, "() => navigator.webdriver")
                except Exception:
                    pass
                exp = {
                    "name": name, "mode": "probe", "status": "done", "variable": EXPERIMENTS.get(name, "baseline"),
                    "result_state": c.state.value, "reason": c.reason, "http_status": status, "webdriver": webdriver,
                    "driver": op.session.driver, "browser_version": op.session.browser_version,
                }  # fmt: skip
        except Exception as e:
            msg = str(e).splitlines()[0][:200]
            exp = {"name": name, "mode": "probe", "status": "skipped", "notes": f"could not run: {type(e).__name__}: {msg}"}
        return self.add_experiment(report_id, exp)

    async def run_probes(self, report_id: str, names: list[str] | None = None) -> dict[str, Any]:
        report: dict[str, Any] = {}
        for name in names or ["baseline", "headless", "no_proxy", "alt_proxy", "fresh", "chromium"]:
            report = await self.probe(report_id, name)
        return report

    async def manual_test(self, report_id: str) -> dict[str, Any]:
        """Experiment (e): the owner signs in by hand in the same profile through the remote view."""
        report = self.get(report_id)
        if report is None:
            raise KeyError(report_id)
        profile = self.profiles.get(report["profile_id"])
        site = self.profiles._sites.get(profile.site_id)
        async with self.profiles.open(profile.id, purpose="diagnosis:manual", actor="admin") as op:
            page = await op.session.page()
            try:
                await page.goto(site.login_url or f"https://{site.domains[0]}/", wait_until="domcontentloaded", timeout=45000)
            except Exception:
                pass
            h = self.handoffs.create(profile.id, "Diagnosis: sign in by hand in this browser, then click Done.", op.display.vnc)
            if self.notifier:
                await self.notifier.send(
                    f"Diagnosis for {profile.id}: please sign in by hand.", self.handoffs.link(h),
                    meta={"kind": "handoff", "ref": h.id, "profile_id": profile.id},
                )  # fmt: skip
            state = await self.handoffs.wait(h.id)
            if state.value != "done":
                return self.add_experiment(report_id, {"name": "manual", "mode": "submit", "status": "skipped", "notes": f"handoff {state.value}"})
            obs = Observation.from_snapshot(await take_snapshot(page))
            c = classify(obs)
            return self.add_experiment(
                report_id,
                {"name": "manual", "mode": "submit", "status": "done", "variable": EXPERIMENTS["manual"], "result_state": c.state.value, "reason": c.reason},
            )
