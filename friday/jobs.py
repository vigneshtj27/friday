"""Job queue: one worker per profile (an account is never driven twice at once),
one login per site at a time, a minimum gap between logins to the same site,
exponential backoff after failures, and a global cap on open browsers."""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from datetime import timedelta
from typing import Any

from friday.agent.policy import ActionPolicy, PolicyContext
from friday.agent.recorder import PageRecorder
from friday.agent.tools import ExecutorDeps, Services, ToolExecutor
from friday.capabilities.activity import FetchActivity
from friday.capabilities.statement import FetchStatement
from friday.capabilities.task import OwnerTask
from friday.login.classifier import LoginState
from friday.login.engine import LoginEngine, LoginServices
from friday.runs import RunStatus
from friday.util import iso, utcnow
from friday.sheets import validate_then
from friday.vault.injector import InjectionScope

log = logging.getLogger(__name__)

KINDS = {"login", "fetch_statement", "fetch_activity", "task", "diagnose_probes", "diagnose_manual"}
FETCH_KINDS = ("fetch_statement", "fetch_activity")
FAILURE_STATES = {
    LoginState.SILENT_BOUNCE, LoginState.ACCESS_DENIED_OR_BLOCKED, LoginState.CREDENTIALS_REJECTED,
    LoginState.CAPTCHA, LoginState.PAGE_FAILED,
}  # fmt: skip


class Backoff(RuntimeError):
    pass


class JobRunner:
    def __init__(self, ctx: Any, llm_factory: Any):
        self.ctx = ctx
        self.llm_factory = llm_factory
        self._queues: dict[str, asyncio.Queue[str]] = {}
        self._workers: dict[str, asyncio.Task[None]] = {}
        self._current: dict[str, asyncio.Task[None]] = {}  # run_id -> task
        self._site_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._browsers = asyncio.Semaphore(ctx.settings.max_browsers)

    # ---- backoff ----------------------------------------------------------

    def backoff_state(self, site_id: str) -> dict[str, Any] | None:
        r = self.ctx.db.query_one("SELECT * FROM site_backoff WHERE site_id = ?", (site_id,))
        return dict(r) if r else None

    def _record_attempt(self, site_id: str, failed: bool) -> None:
        s = self.ctx.settings
        prev = self.backoff_state(site_id) or {"failures": 0}
        failures = prev["failures"] + 1 if failed else 0
        wait = min(s.site_backoff_base_seconds * 2 ** (failures - 1), s.site_backoff_max_seconds) if failed else s.site_min_login_interval_seconds
        self.ctx.db.execute(
            "INSERT INTO site_backoff (site_id, failures, last_attempt_at, next_allowed_at) VALUES (?, ?, ?, ?)"
            " ON CONFLICT(site_id) DO UPDATE SET failures=excluded.failures, last_attempt_at=excluded.last_attempt_at,"
            " next_allowed_at=excluded.next_allowed_at",
            (site_id, failures, iso(), iso(utcnow() + timedelta(seconds=wait))),
        )

    def clear_backoff(self, site_id: str) -> None:
        self.ctx.db.execute("DELETE FROM site_backoff WHERE site_id = ?", (site_id,))
        self.ctx.audit.record("site.backoff_cleared", actor="admin", target=site_id)

    def _check_backoff(self, site_id: str) -> None:
        b = self.backoff_state(site_id)
        if b and b.get("next_allowed_at") and b["next_allowed_at"] > iso() and b["failures"] > 0:
            raise Backoff(f"{site_id} is in backoff after {b['failures']} failure(s) until {b['next_allowed_at']}")

    # ---- queue ------------------------------------------------------------

    def submit(self, kind: str, profile_id: str, params: dict[str, Any] | None = None, *, force: bool = False) -> str:
        if kind not in KINDS:
            raise ValueError(f"unknown job kind {kind!r}")
        params = dict(params or {})
        profile = self.ctx.profiles.get(profile_id)
        then = params.pop("then", None)
        if kind == "fetch_statement":
            params = FetchStatement.validate(params)
        if kind == "task":
            params = OwnerTask.validate(params)
        if kind == "fetch_activity":
            params = FetchActivity.validate(params)
        if then is not None:
            if kind not in FETCH_KINDS:
                raise ValueError("only statement and activity fetches can be followed by a sheet update")
            params["then"] = validate_then(then)
        if kind in ("diagnose_probes", "diagnose_manual") and not params.get("report_id"):
            raise ValueError("report_id is required")
        if kind in ("login", *FETCH_KINDS, "task") and not force:
            self._check_backoff(profile.site_id)
        run_id = self.ctx.runs.create(kind, profile_id, params)
        self.ctx.audit.record("job.submit", actor="admin", target=profile_id, run=run_id, kind=kind)
        q = self._queues.setdefault(profile_id, asyncio.Queue())
        q.put_nowait(run_id)
        w = self._workers.get(profile_id)
        if w is None or w.done():
            self._workers[profile_id] = asyncio.create_task(self._worker(profile_id))
        return run_id

    async def _worker(self, profile_id: str) -> None:
        q = self._queues[profile_id]
        while not q.empty():
            run_id = await q.get()
            task = asyncio.create_task(self._execute(run_id))
            self._current[run_id] = task
            try:
                await task
            except asyncio.CancelledError:
                self.ctx.runs.finish(run_id, RunStatus.ABORTED, "Cancelled by the owner.")
            except Exception as e:
                log.exception("run %s crashed", run_id)
                self.ctx.runs.finish(run_id, RunStatus.FAILED, f"Internal error: {type(e).__name__}: {e}"[:500])
            finally:
                self._current.pop(run_id, None)

    def cancel(self, run_id: str) -> bool:
        t = self._current.get(run_id)
        if t and not t.done():
            t.cancel()
            self.ctx.broker.cancel_for_run(run_id)
            return True
        r = self.ctx.runs.get(run_id)
        if r and r["status"] == RunStatus.QUEUED.value:
            self.ctx.runs.finish(run_id, RunStatus.ABORTED, "Cancelled before it started.")
            return True
        return False

    def active(self) -> list[str]:
        return [r for r, t in self._current.items() if not t.done()]

    async def wait_idle(self) -> None:
        while any(not w.done() for w in self._workers.values()):
            await asyncio.sleep(0.1)

    async def shutdown(self) -> None:
        for t in list(self._current.values()):
            t.cancel()
        for w in self._workers.values():
            w.cancel()
        await asyncio.gather(*self._workers.values(), return_exceptions=True)

    # ---- execution --------------------------------------------------------

    def _executor(self, op: Any, page: Any, run_id: str, llm: Any) -> ToolExecutor:
        ctx = self.ctx
        profile, site = op.profile, op.site
        prefix = profile.secret_prefix

        async def request_otp(channel: str, reason: str) -> str:
            res = await ctx.broker.obtain(profile=profile, site=site, channel=channel, since=utcnow() - timedelta(minutes=2), run_id=run_id)
            if not res.code:
                return f"No code available: {res.detail}. Call request_human or finish."
            ctx.vault.put_ephemeral(f"{prefix}otp", res.code, ttl_seconds=300)
            return f"Code ready ({res.detail}). Type secret:{prefix}otp into the code field. It works once."

        async def request_human(reason: str) -> str:
            h = ctx.handoffs.create(profile.id, reason, op.display.vnc)
            await ctx.notifier.send(
                f"{site.name} ({profile.account}) needs you: {reason}", ctx.handoffs.link(h),
                meta={"kind": "handoff", "ref": h.id, "profile_id": profile.id},
            )  # fmt: skip
            ctx.runs.step(run_id, "handoff", result=f"{reason} -> {ctx.handoffs.link(h)}")
            state = await ctx.handoffs.wait(h.id)
            return "The owner finished; read the page and continue." if state.value == "done" else f"Handoff {state.value}; finish with status blocked."

        deps = ExecutorDeps(
            policy=ActionPolicy(PolicyContext(site.all_domains, ctx.redactor)),
            gate=ctx.gate,
            injector=ctx.injector,
            scope=InjectionScope(profile.id, prefix, site.all_domains),
            files=ctx.files,
            runs=ctx.runs,
            audit=ctx.audit,
            redactor=ctx.redactor,
            downloads_dir=ctx.profiles.downloads_dir(profile),
            run_id=run_id,
            profile_id=profile.id,
            services=Services(request_otp=request_otp, request_human=request_human),
            action_delay_ms=tuple(ctx.settings.action_delay_ms),
            step_screenshots=ctx.settings.step_screenshots,
        )
        return ToolExecutor(op.session.context, page, deps)

    async def _execute(self, run_id: str) -> None:
        ctx = self.ctx
        run = ctx.runs.get(run_id)
        if run is None or run["status"] != RunStatus.QUEUED.value:
            return
        kind, params = run["kind"], run["params"]
        profile = ctx.profiles.get(run["profile_id"])
        async with self._browsers:
            ctx.runs.start(run_id)
            if kind == "diagnose_probes":
                report = await ctx.diagnoser.run_probes(params["report_id"], params.get("names"))
                ctx.runs.finish(run_id, RunStatus.SUCCEEDED, f"Probes done. Next: {report.get('next_experiment')}", {"report_id": params["report_id"]})
                return
            if kind == "diagnose_manual":
                report = await ctx.diagnoser.manual_test(params["report_id"])
                ctx.runs.finish(run_id, RunStatus.SUCCEEDED, "Manual-login experiment recorded.", {"report_id": params["report_id"]})
                return
            report_id = await self._run_browser_job(run_id, kind, params, profile)
        if report_id:
            # Probes reopen the profile, so they run after it is closed.
            self.submit("diagnose_probes", profile.id, {"report_id": report_id})

    async def _preflight_blocks(self, run_id: str, kind: str, params: dict[str, Any], profile: Any) -> bool:
        """True if the run must not touch the site (friday/preflight.py). Never counts as a bounce."""
        from friday.preflight import SIGNIN_KINDS

        ctx = self.ctx
        if kind not in SIGNIN_KINDS or ctx.settings.preflight == "off":
            return False
        rep = await ctx.preflight.check(profile.id, need_google=bool((params.get("then") or {}).get("sheet")))
        ctx.runs.step(run_id, "preflight", result="; ".join(f"{i.name}: {'ok' if i.ok else 'FAIL'}" for i in rep.items))
        if rep.ok:
            return False
        if ctx.settings.preflight == "warn":
            log.warning("preflight failed for %s (warn mode): %s", profile.id, [i.name for i in rep.failed])
            return False
        if ctx.preflight.take_override(profile.id):
            ctx.runs.step(run_id, "preflight", result="owner override: one deliberate attempt despite: " + ", ".join(i.name for i in rep.failed))
            return False
        ctx.runs.finish(run_id, RunStatus.NEEDS_ATTENTION, f"Preflight blocked the sign-in: {', '.join(i.name for i in rep.failed)}.",
                        {"preflight": rep.public()})  # fmt: skip
        ctx.audit.record("preflight.blocked", target=profile.id, run=run_id, failed=[i.name for i in rep.failed])
        await ctx.notifier.send(rep.chat_message(), None, meta={"kind": "run", "ref": run_id, "profile_id": profile.id})
        return True

    async def _leaks_block(self, run_id: str, kind: str, op: Any, page: Any) -> bool:
        """Before the site is opened: check, in this browser, that IPv4/IPv6/WebRTC don't bypass the proxy."""
        from friday.preflight import SIGNIN_KINDS, is_local_site

        ctx = self.ctx
        if kind not in SIGNIN_KINDS or ctx.settings.preflight == "off" or is_local_site(op.site):
            return False
        if ctx.browsercheck.cached(op.profile) is not None:
            return False  # preflight already judged the cached result
        items = await ctx.browsercheck.leaks_in_session(op.profile, page)
        ctx.runs.step(run_id, "browser-check", result="; ".join(f"{i.name}: {'ok' if i.ok else 'FAIL ' + i.detail}" for i in items))
        bad = [i for i in items if not i.ok]
        if not bad or ctx.settings.preflight == "warn":
            return False
        names = ", ".join(i.name for i in bad)
        msg = [f"I didn't open {op.site.name} for {op.profile.id}: the browser leaks around the proxy ({names}). Not counted as a bounce."]
        for i in bad:
            msg += [f"- {i.name}: {i.detail}", *(f"  fix: {f}" for f in i.fix)]
        ctx.runs.finish(run_id, RunStatus.NEEDS_ATTENTION, f"Browser leak check failed: {names}.", {"browser_check": [i.name for i in bad]})
        ctx.audit.record("preflight.blocked", target=op.profile.id, run=run_id, failed=[f"browser:{i.name}" for i in bad])
        await ctx.notifier.send("\n".join(msg), None, meta={"kind": "run", "ref": run_id, "profile_id": op.profile.id})
        return True

    async def _run_browser_job(self, run_id: str, kind: str, params: dict[str, Any], profile: Any) -> str | None:
        ctx = self.ctx
        if await self._preflight_blocks(run_id, kind, params, profile):
            return None
        llm = self.llm_factory()
        report_id = None
        async with ctx.profiles.open(profile.id, purpose=kind) as op:
            for w in op.warnings:
                ctx.runs.step(run_id, "warning", result=w)
            page = await op.session.page()
            if await self._leaks_block(run_id, kind, op, page):
                return None
            recorder = PageRecorder(ctx.redactor)
            recorder.attach(op.session.context)
            ex = self._executor(op, page, run_id, llm)
            engine = LoginEngine(
                LoginServices(
                    llm=llm, runs=ctx.runs, vault=ctx.vault, broker=ctx.broker, notifier=ctx.notifier,
                    handoffs=ctx.handoffs, sitenotes=ctx.sitenotes, audit=ctx.audit, diagnoser=ctx.diagnoser,
                )  # fmt: skip
            )
            async with self._site_locks[profile.site_id]:
                b = self.backoff_state(profile.site_id)
                if b and b["failures"] == 0 and b.get("next_allowed_at") and b["next_allowed_at"] > iso():
                    # rate limit: keep a minimum gap between sign-ins to the same site
                    from datetime import datetime

                    gap = (datetime.fromisoformat(b["next_allowed_at"]) - utcnow()).total_seconds()
                    await asyncio.sleep(max(0.0, min(gap, ctx.settings.site_min_login_interval_seconds)))
                login = await engine.login(op, ex, recorder, run_id)
                if not login.reused_session:
                    self._record_attempt(profile.site_id, failed=login.state in FAILURE_STATES)
            if login.agent:
                self._save_notes(op.site.id, run_id, login.agent.notes, login.agent.contradicted_notes)
            if not login.ok:
                ctx.profiles.set_status(profile.id, "needs_attention")
                result: dict[str, Any] = {"login_state": login.state.value, "history": login.history[-12:]}
                if login.diagnosis is not None:
                    report_id = ctx.diagnoser.create(profile.id, run_id, login.state.value, login.diagnosis)
                    result["diagnosis_report"] = report_id
                ctx.runs.finish(run_id, RunStatus.NEEDS_ATTENTION, f"Sign-in did not complete: {login.summary}", result, login.agent.cost_usd if login.agent else 0)
                await ctx.notifier.send(
                    f"{profile.id}: sign-in needs attention ({login.state}).", ctx.portal_link(f"runs/{run_id}"),
                    meta={"kind": "run", "ref": run_id, "profile_id": profile.id},
                )  # fmt: skip
                await ex.stop()
                return report_id
            ctx.profiles.set_status(profile.id, "logged_in")
            if kind == "login":
                ctx.runs.finish(run_id, RunStatus.SUCCEEDED, login.summary, {"reused_session": login.reused_session})
            elif kind in (*FETCH_KINDS, "task"):
                cap = {"fetch_statement": FetchStatement, "fetch_activity": FetchActivity, "task": OwnerTask}[kind](ctx, llm)
                res = await cap.run(op, ex, run_id, params)
                if res.status == "succeeded" and (params.get("then") or {}).get("sheet"):
                    await self._then_sheet(res, profile.id, params)
                self._save_notes(op.site.id, run_id, res.notes, res.contradicted_notes)
                ctx.runs.finish(run_id, RunStatus(res.status), res.summary, res.result)
                await ctx.notifier.send(
                    f"{profile.id}: {res.summary}"[:500], ctx.portal_link(f"runs/{run_id}"),
                    meta={"kind": "run", "ref": run_id, "profile_id": profile.id},
                )  # fmt: skip
            await ex.stop()
        return report_id

    async def _then_sheet(self, res: Any, profile_id: str, params: dict[str, Any]) -> None:
        """After a successful fetch: append the range's transactions to the owner's tab and verify by reading it back."""
        from datetime import date as _date

        from friday.sheets import export_transactions

        t = params["then"]["sheet"]
        start, end = _date.fromisoformat(params["start"]), _date.fromisoformat(params["end"])
        try:
            v = await export_transactions(
                self.ctx, spreadsheet=t["spreadsheet"], tab=t["tab"], account=profile_id, start=start, end=end,
                include_credits=t.get("include_credits", False), column_map=t.get("column_map"),
            )  # fmt: skip
        except Exception as e:
            res.status = "needs_attention"
            res.result["sheet_error"] = f"{type(e).__name__}: {e}"[:500]
            res.summary += f" The sheet was not updated: {e}"[:400]
            return
        res.result["sheet"] = v
        res.summary += (
            f" Sheet tab '{v['tab']}': appended {v['appended']} row(s)"
            + (f", skipped {v['already_in_sheet']} already there" if v["already_in_sheet"] else "")
            + (f", left out {v['credits_left_out']} credit(s)" if v["credits_left_out"] else "")
            + f". Read back: {v['rows_after']} data rows (was {v['rows_before']}); {v['rows_in_range']} rows dated "
            f"{start}..{end} totalling {v['total_in_range']}."
        )
        if not v["ok"]:
            res.status = "needs_attention"
            res.summary += f" Only {v['found_after']} of {v['appended']} appended rows were found on read-back: check the tab."

    def _save_notes(self, site_id: str, run_id: str, notes: list[dict[str, str]], contradicted: list[str]) -> None:
        for n in notes[:10]:
            if isinstance(n, dict) and n.get("text"):
                self.ctx.sitenotes.add(site_id, str(n.get("kind", "quirk")), str(n["text"]), run_id=run_id)
        if contradicted:
            self.ctx.sitenotes.mark_stale(site_id, contradicted)

