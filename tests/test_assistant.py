from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from friday.assistant import ChatAssistant
from friday.capabilities.task import OwnerTask
from friday.capabilities.transactions import Transaction
from friday.chat import looks_like_code, mask_codes
from friday.profiles.models import ProfileConfig
from friday.runs import RunStatus

from .planner_fakes import PlanLLM, chk, last_friday, plan, settle, step


@pytest.fixture
def setup(ctx, monkeypatch):
    """A profile with credentials; job submission is recorded and the run finishes at once."""
    ctx.profiles.create("mybank", "me", ProfileConfig())
    ctx.vault.put("mybank/me/username", "test-user")
    ctx.vault.put("mybank/me/password", "test-pass-123")
    submitted = []

    def submit(kind, profile_id, params=None, *, force=False):
        submitted.append((kind, profile_id, params))
        run_id = ctx.runs.create(kind, profile_id, params or {})
        ctx.runs.start(run_id)
        ctx.runs.finish(run_id, RunStatus.SUCCEEDED, f"{kind} done.", {})
        return run_id

    monkeypatch.setattr(ctx.jobs, "submit", submit)

    def attach(plans, reports=None):
        llm = PlanLLM(plans, reports=reports)
        ctx.chat.assistant = ChatAssistant(ctx, lambda: llm)
        return llm

    return ctx, submitted, attach


async def test_free_text_starts_a_run_and_replies(setup):
    ctx, submitted, attach = setup
    await ctx.notifier.start(ctx.broker.handle_reply)
    attach([plan(step("browser_run", {"account": "mybank", "kind": "fetch_statement", "start": "2026-09-01", "end": "2026-09-30"},
                      checks=[chk("status", "eq", "succeeded", says="the run succeeded")], progress="Fetching the statement"),
                 goal="Fetch your My Bank statement for September")],
           reports=["Fetched your My Bank statement for September (2026-09-01 to 2026-09-30)."])  # fmt: skip
    await ctx.chat.owner_says("get my bank statement for september")
    await settle(ctx)
    assert submitted == [("fetch_statement", "mybank.me", {"start": "2026-09-01", "end": "2026-09-30"})]
    kinds = [r["kind"] for r in ctx.chat.store.list()]
    assert kinds[0] is None and "plan" in kinds and "run" in kinds  # owner, plan card, run card
    assert "September" in last_friday(ctx) and "the run succeeded" in last_friday(ctx)
    assert ctx.chat.assistant.busy is False


async def test_codes_never_reach_the_model(setup):
    ctx, _, attach = setup
    await ctx.notifier.start(ctx.broker.handle_reply)
    llm = attach([])
    await ctx.chat.owner_says("482913")
    await settle(ctx)
    assert llm.plan_inputs == []
    assert "discarded" in ctx.chat.store.list()[-1]["text"]


async def test_model_failure_is_reported_politely(setup):
    ctx, _, _ = setup

    def broken():
        raise RuntimeError("no credentials")

    ctx.chat.assistant = ChatAssistant(ctx, broken)
    await ctx.chat.owner_says("check my balance")
    await settle(ctx)
    assert "couldn't reach the model" in ctx.chat.store.list()[-1]["text"]


async def test_tool_errors_go_back_to_the_model(setup):
    ctx, submitted, attach = setup
    llm = attach([plan(step("browser_run", {"account": "nosuchbank", "kind": "login"})),
                  plan(reply="I don't know an account called nosuchbank; your accounts are mybank.me.")])  # fmt: skip
    await ctx.chat.owner_says("sign in to nosuchbank")
    await settle(ctx)
    assert submitted == []
    assert "no single account" in llm.plan_inputs[1] and "<untrusted_failure>" in llm.plan_inputs[1]
    assert "nosuchbank" in last_friday(ctx)


async def test_transactions_tool_filters_and_totals(setup):
    ctx, _, _ = setup
    t = lambda d, desc, amt, direction: Transaction(date.fromisoformat(d), desc, Decimal(amt), "INR", direction, None, "mybank.me", "f1")  # noqa: E731
    ctx.transactions.add([
        t("2026-09-03", "AMAZON PAY INDIA", "1299.00", "debit"),
        t("2026-09-10", "Amazon refund", "199.00", "credit"),
        t("2026-09-12", "Swiggy", "450.00", "debit"),
    ])
    a = ChatAssistant(ctx, lambda: None)
    out = await a._run_tool("query_transactions", {"contains": "amazon", "start": "2026-09-01", "end": "2026-09-30"})
    assert out["matched"] == 2
    assert out["totals"]["INR"] == {"debit": "1299.00", "credit": "199.00"}


def test_task_goal_validation():
    assert OwnerTask.validate({"goal": "  find   my electricity bill due date "}) == {"goal": "find my electricity bill due date"}
    with pytest.raises(ValueError):
        OwnerTask.validate({"goal": ""})


def test_masking_keeps_dates_and_amounts_readable():
    assert looks_like_code("284 113") and looks_like_code("G-482913")
    assert not looks_like_code("fetch 2026-09-01 to 2026-09-30")
    assert mask_codes("fetch 2026-09-01 to 2026-09-30, card 4417, ₹12,480.00") == "fetch 2026-09-01 to 2026-09-30, card 4417, ₹12,480.00"
    assert mask_codes("the code was 284113 i think") == "the code was •••••• i think"


async def test_run_finish_does_not_duplicate_its_card(setup):
    ctx, _, _ = setup
    run_id = ctx.runs.create("task", "mybank.me", {"goal": "check balance"})
    meta = {"kind": "run", "ref": run_id, "profile_id": "mybank.me"}
    await ctx.chat.send("Working on it.", None, meta=meta)
    await ctx.chat.send("mybank.me: Balance is ₹40,210.55.", None, meta=meta)
    rows = ctx.chat.store.list()
    assert [r["kind"] for r in rows] == ["run", None]


# ---- "fetch and populate my Oct card expenses" -------------------------------------------------


@pytest.fixture
def sheets(setup):
    from .fake_google import SID, FakeGoogle, install

    ctx, submitted, attach = setup
    fake = FakeGoogle({SID: {"Oct 2026": [["Date", "Description", "Category", "Amount", "Notes"]]}}, titles={SID: "Expenses 2026"})
    install(ctx, fake)
    return ctx, submitted, attach, SID


async def test_saving_the_expense_sheet_needs_the_owners_words_and_checks_columns(sheets):
    ctx, _, attach, sid = sheets
    attach([plan(step("sheets_set_expense_sheet", {"spreadsheet": sid, "tab": "Oct 2026",
                                                   "owner_quote": "use the Oct 2026 tab of Expenses 2026"}))],
           reports=["Saved."])  # fmt: skip
    await ctx.chat.owner_says("use the Oct 2026 tab of Expenses 2026 for expenses")
    await settle(ctx)
    assert ctx.prefs.get("expense_sheet")["tab"] == "Oct 2026"
    assert "Expenses 2026" in ctx.memory.context("expense sheet")


async def test_recent_card_statement_becomes_activity_fetch(sheets):
    ctx, submitted, attach, sid = sheets
    today, first = date.today().isoformat(), date.today().replace(day=1).isoformat()
    llm = attach([plan(step("browser_run", {"account": "mybank.me", "kind": "fetch_statement", "start": first, "end": today,
                                            "account_type": "card"}, checks=[chk("status", "eq", "succeeded")]))],
                 reports=["Read this month's card activity."])  # fmt: skip
    await ctx.chat.owner_says("fetch my card expenses this month")
    await settle(ctx)
    kind, account, params = submitted[0]
    assert kind == "fetch_activity" and params["account_type"] == "card"
    assert "Switched to fetch_activity" in llm.report_inputs[0]


async def test_sheets_not_connected_points_to_connect_google(setup):
    ctx, _, attach = setup
    ctx.settings.google_client_id = "cid"
    ctx.google.client_id = "cid"
    llm = attach([plan(step("sheets_list")), plan(step("google_connect", {"purpose": "workspace"}))],
                 reports=["Tap the link to connect Google."])  # fmt: skip
    await ctx.chat.owner_says("which sheets do I have?")
    await settle(ctx)
    assert "google_connect" in llm.plan_inputs[1] or "connect Google" in llm.plan_inputs[1]
    assert any("/oauth/google/start?purpose=workspace" in r["text"] for r in ctx.chat.store.list())
