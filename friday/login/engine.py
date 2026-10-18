"""Generic login(site, account): the agent loop drives the page, this state machine
watches every step, and each state has exactly one next action.

Hard rules enforced here (not in the prompt):
  * a valid session is reused, never re-logged-in;
  * a rejected password is retried at most once, then the owner is asked;
  * no state may repeat more than twice;
  * SILENT_BOUNCE and ACCESS_DENIED_OR_BLOCKED stop the run and trigger a diagnosis.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from friday.agent.loop import AgentLoop, AgentOutcome, AgentTask, HookResult, StepEvent
from friday.agent.recorder import PageRecorder
from friday.agent.snapshot import take_snapshot
from friday.agent.tools import ToolExecutor
from friday.login.classifier import Classification, LoginState, Observation, classify, classify_with_model
from friday.util import utcnow

log = logging.getLogger(__name__)

PROGRESS_TOOLS = {"click", "press", "goto", "select", "wait_for", "download_file", "request_human"}
STOP_FOR_DIAGNOSIS = {LoginState.SILENT_BOUNCE, LoginState.ACCESS_DENIED_OR_BLOCKED}


@dataclass
class LoginOutcome:
    ok: bool
    state: LoginState
    summary: str
    reused_session: bool = False
    agent: AgentOutcome | None = None
    history: list[str] = field(default_factory=list)
    diagnosis: dict[str, Any] | None = None  # captured in-session evidence, if any


@dataclass
class LoginServices:
    """What the engine needs from the rest of the app (kept explicit for testing)."""

    llm: Any
    runs: Any
    vault: Any
    broker: Any
    notifier: Any
    handoffs: Any
    sitenotes: Any
    audit: Any
    diagnoser: Any | None = None


class LoginEngine:
    def __init__(self, services: LoginServices, *, max_steps: int = 30, push_wait_seconds: int = 180):
        self.s = services
        self.max_steps = max_steps
        self.push_wait_seconds = push_wait_seconds

    async def observe(self, ex: ToolExecutor, recorder: PageRecorder) -> Observation:
        try:
            snap = await take_snapshot(ex.page)
            ex.snapshot = snap
        except Exception as e:
            return Observation(ex.page.url, "", "", [], load_error=f"snapshot failed: {type(e).__name__}")
        return Observation.from_snapshot(snap, status=recorder.last_status)

    async def _model_check(self, ex: ToolExecutor, obs: Observation, run_id: str) -> Classification:
        shot = None
        try:
            shot = base64.b64encode(await ex.d.injector.screenshot(ex.page, type="jpeg", quality=55)).decode()
        except Exception:
            pass
        c, cost = await classify_with_model(self.s.llm, obs, shot)
        self.s.runs.add_cost(run_id, cost)
        return c

    async def login(self, op: Any, ex: ToolExecutor, recorder: PageRecorder, run_id: str) -> LoginOutcome:
        profile, site = op.profile, op.site
        prefix = profile.secret_prefix
        history: list[str] = []
        start_url = site.login_url or f"https://{site.domains[0]}/"
        from friday.profiles.models import site_problems

        problems = site_problems(site)
        if problems:
            # Otherwise the allowlist blocks the login page and the run sees only Chrome's error page.
            return LoginOutcome(False, LoginState.PAGE_FAILED, "The site's settings are wrong: " + "; ".join(problems) + ". Fix its domains, then try again.", history=[])

        # 1. Reuse a valid session if there is one.
        await ex.start()
        try:
            await ex.page.goto(start_url, wait_until="domcontentloaded", timeout=45000)
            await ex._settle()
        except Exception as e:
            log.info("initial navigation failed: %s", e)
        obs = await self.observe(ex, recorder)
        c = classify(obs)
        history.append(str(c))
        self.s.runs.step(run_id, "state", state=c.state.value, result=c.reason)
        if c.state == LoginState.LOGGED_IN:
            confirm = await self._model_check(ex, obs, run_id)
            if confirm.state == LoginState.LOGGED_IN:
                self.s.audit.record("login.session_reused", target=profile.id, run=run_id)
                return LoginOutcome(True, LoginState.LOGGED_IN, "Existing session is still valid.", True, history=history)
        if c.state in STOP_FOR_DIAGNOSIS or c.state == LoginState.PAGE_FAILED:
            evidence = await self._evidence(ex, recorder, c.state)
            return LoginOutcome(False, c.state, f"Login page itself is not usable: {c}", history=history, diagnosis=evidence)

        missing = [n for n in (f"{prefix}username", f"{prefix}password") if not self.s.vault.exists(n)]
        if missing:
            return LoginOutcome(False, c.state, f"Missing credentials in the vault: {', '.join(missing)}", history=history)
        answer_secrets = [m.name for m in self.s.vault.list(prefix) if any(k in m.name for k in ("answer", "security", "memorable"))]

        # 2. Let the agent sign in, watching every step.
        st = _MachineState(prev=c.state)
        evidence_box: dict[str, Any] = {}

        async def hook(ev: StepEvent) -> HookResult | None:
            if ev.tool == "type" and str(ev.args.get("text", "")).startswith(f"secret:{prefix}password") and not ev.result.is_error:
                st.password_typed = True
            if ev.tool not in PROGRESS_TOOLS:
                return None
            submitted = st.password_typed and ev.tool in ("click", "press") and ex.last_submit is not None
            obs = await self.observe(ex, recorder)
            c = classify(obs, just_submitted_credentials=submitted)
            if c.state == LoginState.LOGGED_IN:
                mc = await self._model_check(ex, obs, run_id)  # always confirm before declaring success
                if mc.state != LoginState.LOGGED_IN:
                    c = mc if mc.state != LoginState.UNKNOWN else Classification(LoginState.UNKNOWN, "model did not confirm sign-in", 0.5)
            elif c.confidence < 0.7:
                mc = await self._model_check(ex, obs, run_id)
                # A post-submit "login form" verdict from the rules is a bounce; keep it.
                if mc.state != LoginState.UNKNOWN and not (submitted and c.state == LoginState.SILENT_BOUNCE):
                    c = mc
            if submitted:
                st.password_typed = False
            history.append(str(c))
            self.s.runs.step(run_id, "state", state=c.state.value, result=c.reason)
            return await self._transition(c, st, op, ex, recorder, run_id, evidence_box, answer_secrets, submitted)

        task = AgentTask(
            goal=(
                f"Sign in to {site.name} for account '{profile.account}'. Use secret:{prefix}username and "
                f"secret:{prefix}password. The system watches the page after each action and will tell you about "
                "one-time codes, approvals and challenges. When the account area is showing, call finish with "
                "status success."
            ),
            run_id=run_id,
            profile_id=profile.id,
            site_name=site.name,
            allowed_domains=site.all_domains,
            secret_names=[m.name for m in self.s.vault.list(prefix) if not m.name.endswith("totp-seed")],
            site_notes=self.s.sitenotes.render(site.id),
            max_steps=self.max_steps,
        )
        loop = AgentLoop(self.s.llm, ex, self.s.runs)
        outcome = await loop.run(task, hook)
        final = LoginState(st.final) if st.final else (LoginState.LOGGED_IN if outcome.status == "success" else st.prev)
        if outcome.status == "success" and final != LoginState.LOGGED_IN:
            # The model claimed success: verify before believing it.
            obs = await self.observe(ex, recorder)
            c = classify(obs)
            if c.state != LoginState.LOGGED_IN:
                c = await self._model_check(ex, obs, run_id)
            final = c.state
            history.append("verify: " + str(c))
        ok = final == LoginState.LOGGED_IN
        if ok and site.login_url is None and st.login_form_url:
            self.s.sitenotes.set_singleton(site.id, "login_url", f"Login form at {st.login_form_url}", run_id=run_id)
        return LoginOutcome(
            ok, final, outcome.summary if not st.stop_reason else st.stop_reason, False, outcome, history,
            evidence_box.get("evidence"),
        )  # fmt: skip

    async def _evidence(self, ex: ToolExecutor, recorder: PageRecorder, state: LoginState) -> dict[str, Any] | None:
        if not self.s.diagnoser:
            return None
        try:
            return await self.s.diagnoser.capture_in_session(ex, recorder, state)
        except Exception as e:
            log.warning("evidence capture failed: %s", e)
            return {"error": f"capture failed: {type(e).__name__}"}

    async def _transition(
        self,
        c: Classification,
        st: _MachineState,
        op: Any,
        ex: ToolExecutor,
        recorder: PageRecorder,
        run_id: str,
        evidence_box: dict[str, Any],
        answer_secrets: list[str],
        submitted: bool,
    ) -> HookResult | None:
        profile, site = op.profile, op.site
        prefix = profile.secret_prefix
        changed = c.state != st.prev
        st.prev = c.state
        st.counts[c.state] += 1

        def stop(final: LoginState, status: str, summary: str) -> HookResult:
            st.final, st.stop_reason = final.value, summary
            return HookResult(stop=AgentOutcome(status, summary))

        if c.state not in (LoginState.LOGIN_FORM, LoginState.UNKNOWN) and st.counts[c.state] > 2:
            return stop(c.state, "needs_attention", f"Stuck: the page stayed in {c.state} more than twice.")

        match c.state:
            case LoginState.LOGGED_IN:
                if st.final == LoginState.LOGGED_IN.value:  # already told; the model kept acting
                    return stop(LoginState.LOGGED_IN, "success", "Signed in.")
                st.final = LoginState.LOGGED_IN.value
                return HookResult(
                    inject="Sign-in confirmed: the account area is showing. Call finish now with status success, "
                    "and add notes about this site's sign-in flow for next time. Do not navigate further."
                )
            case LoginState.LOGIN_FORM:
                if changed:
                    st.login_form_url = st.login_form_url or ex.page.url.split("?")[0]
                    return HookResult(
                        inject=f"Page state: sign-in form. Fill the username field with secret:{prefix}username and the "
                        f"password field with secret:{prefix}password (if shown), then submit."
                    )
                return None
            case LoginState.NEEDS_OTP_EMAIL | LoginState.NEEDS_OTP_SMS | LoginState.NEEDS_TOTP:
                if not changed and st.otp_ready:
                    return None
                channel = {
                    LoginState.NEEDS_OTP_EMAIL: "email",
                    LoginState.NEEDS_OTP_SMS: "sms",
                    LoginState.NEEDS_TOTP: "totp",
                }[c.state]
                if channel == "sms" and "email" in c.channels and self.s.broker.gmail and self.s.broker.gmail.configured:
                    channel = "email"
                return await self._otp(channel, st, op, run_id, stop)
            case LoginState.NEEDS_APPROVAL_IN_APP:
                await self.s.notifier.send(f"Approve the sign-in to {site.name} ({profile.account}) on your phone.")
                deadline = asyncio.get_running_loop().time() + self.push_wait_seconds
                while asyncio.get_running_loop().time() < deadline:
                    await asyncio.sleep(3)
                    obs = await self.observe(ex, recorder)
                    c2 = classify(obs)
                    if c2.state != LoginState.NEEDS_APPROVAL_IN_APP:
                        st.prev = c2.state
                        return HookResult(inject=f"The approval was handled. The page now looks like: {c2.state}. Read the page and continue.")
                return stop(LoginState.NEEDS_APPROVAL_IN_APP, "needs_attention", "No in-app approval before the timeout.")
            case LoginState.SECURITY_QUESTION:
                if answer_secrets:
                    return HookResult(
                        inject="Page state: security question. Stored answers: "
                        + ", ".join(f"secret:{n}" for n in answer_secrets)
                        + ". Type the one whose name matches the question. If none match, call request_human."
                    )
                return await self._handoff(op, run_id, "Security question with no stored answer", st, stop)
            case LoginState.CAPTCHA:
                return await self._handoff(op, run_id, "CAPTCHA shown during sign-in", st, stop)
            case LoginState.CREDENTIALS_REJECTED:
                if not submitted:
                    # The form went out without the stored password (typing failed or was skipped):
                    # not a verdict on the credentials, so it doesn't count toward the lockout limit.
                    return HookResult(
                        inject="That submit did not include the stored password (the type step failed or was "
                        "skipped). Fill both fields with the secret: references and check they were filled "
                        "before submitting. Do not submit an empty form again."
                    )
                st.rejections += 1
                if st.rejections == 1:
                    return HookResult(
                        inject="The site rejected the credentials. Check that the username went into the username "
                        "field and the password into the password field, then submit exactly once more. If it is "
                        "rejected again, stop."
                    )
                await self.s.notifier.send(
                    f"{site.name} ({profile.account}) rejected the stored password twice. I stopped to avoid a lockout. "
                    "Please check the credentials."
                )
                return stop(LoginState.CREDENTIALS_REJECTED, "needs_attention", "Credentials rejected twice; stopped to avoid locking the account.")
            case LoginState.SILENT_BOUNCE | LoginState.ACCESS_DENIED_OR_BLOCKED:
                evidence_box["evidence"] = await self._evidence(ex, recorder, c.state)
                await self.s.notifier.send(f"Sign-in to {site.name} ({profile.account}) hit {c.state}. Diagnosis started.")
                return stop(c.state, "needs_attention", f"{c.state}: {c.reason}. A diagnosis report was started.")
            case LoginState.PAGE_FAILED:
                if st.counts[c.state] == 1:
                    return HookResult(inject=f"The page failed to load. Navigate to {site.login_url or ex.page.url} once more.")
                evidence_box["evidence"] = await self._evidence(ex, recorder, c.state)
                return stop(LoginState.PAGE_FAILED, "failed", "The page failed to load twice.")
            case LoginState.UNKNOWN:
                st.unknowns += 1
                if st.unknowns >= 4:
                    return await self._handoff(op, run_id, "Unrecognised page during sign-in", st, stop)
                return None
        return None

    async def _otp(self, channel: str, st: _MachineState, op: Any, run_id: str, stop: Any) -> HookResult:
        st.otp_rounds += 1
        if st.otp_rounds > 2:
            return stop(LoginState(st.prev), "needs_attention", "Two one-time codes did not get through.")
        since: datetime = st.started_at
        res = await self.s.broker.obtain(profile=op.profile, site=op.site, channel=channel, since=since, run_id=run_id)
        if not res.code:
            return stop(LoginState(st.prev), "needs_attention", f"No one-time code: {res.detail}.")
        name = f"{op.profile.secret_prefix}otp"
        self.s.vault.put_ephemeral(name, res.code, ttl_seconds=300)
        st.otp_ready = True
        return HookResult(
            inject=f"A one-time code is ready ({res.detail}). Type secret:{name} into the code field and submit. "
            "It can be used once. If the page says the code expired, click the site's resend option and call "
            "request_otp."
        )

    async def _handoff(self, op: Any, run_id: str, reason: str, st: _MachineState, stop: Any) -> HookResult:
        h = self.s.handoffs.create(op.profile.id, reason, op.display.vnc)
        await self.s.notifier.send(
            f"{op.site.name} ({op.profile.account}) needs you: {reason}.", self.s.handoffs.link(h),
            meta={"kind": "handoff", "ref": h.id, "profile_id": op.profile.id},
        )  # fmt: skip
        self.s.runs.step(run_id, "handoff", result=f"{reason} -> {self.s.handoffs.link(h)}")
        state = await self.s.handoffs.wait(h.id)
        if state.value != "done":
            return stop(LoginState(st.prev), "blocked", f"Handoff '{reason}' ended as {state.value}.")
        st.counts.clear()
        return HookResult(inject="The owner finished the step in the browser. Read the page and continue.")


@dataclass
class _MachineState:
    prev: LoginState
    counts: Counter = field(default_factory=Counter)
    password_typed: bool = False
    rejections: int = 0
    unknowns: int = 0
    otp_rounds: int = 0
    otp_ready: bool = False
    final: str | None = None
    stop_reason: str | None = None
    login_form_url: str | None = None
    started_at: datetime = field(default_factory=utcnow)
