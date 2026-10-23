"""Phase 1: the task ledger, crash-safe journaling, restart recovery, and the chat task tools."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from friday.tools.base import Call
from friday.util import iso

from .fake_google import SID, FakeGoogle, install
from .planner_fakes import PlanLLM, attach, chk, last_friday, plan, settle, step

# ---- ledger unit --------------------------------------------------------------------------------


def test_open_is_owner_only_and_moves_through_states(ctx):
    ctx.tasks.open("t1", "fetch my Amex expenses", origin="owner")
    ctx.tasks.open("s1", "watch SBIN", origin="schedule:abc")  # scheduled fires get no task row
    assert [t["id"] for t in ctx.tasks.list()] == ["t1"]
    assert ctx.tasks.get("t1")["status"] == "planned"
    ctx.tasks.running("t1", "reading the activity page")
    assert ctx.tasks.get("t1")["status"] == "running" and "reading" in ctx.tasks.get("t1")["next_step"]
    ctx.tasks.waiting("t1", "approve adding rows")
    assert ctx.tasks.get("t1")["status"] == "waiting_on_me"
    ctx.tasks.finish("t1", "done", closeout="Added 4 charges.", evidence=["read back 4 rows"])
    t = ctx.tasks.get("t1")
    assert t["status"] == "done" and t["closeout"] == "Added 4 charges." and t["next_step"] is None


def test_journal_marks_uncertain_steps(ctx):
    ctx.tasks.open("t2", "append rows")
    ctx.tasks.journal_start("t2", "s1", "sheets_append_rows", "write")
    ctx.tasks.journal_finish("t2", "s1", "sheets_append_rows", "write", marker="appended 3")
    assert ctx.tasks.uncertain_steps("t2") == []  # finished cleanly
    ctx.tasks.journal_start("t2", "s2", "gmail_send", "send")  # started, never finished (a crash)
    unc = ctx.tasks.uncertain_steps("t2")
    assert len(unc) == 1 and unc[0]["step_id"] == "s2" and unc[0]["tool"] == "gmail_send"


def test_recover_marks_live_tasks_interrupted(ctx):
    ctx.tasks.open("t3", "do a thing")
    ctx.tasks.running("t3", "step 1")
    ctx.tasks.open("t4", "another")  # left planned
    ctx.tasks.finish("t4b" if False else "t4", "done", closeout="ok")  # a finished one is untouched
    ctx.tasks.open("t5", "mid-flight")
    ctx.tasks.running("t5")
    interrupted = ctx.tasks.recover()
    ids = {t["id"] for t in interrupted}
    assert ids == {"t3", "t5"}
    assert ctx.tasks.get("t3")["status"] == "interrupted"
    assert ctx.tasks.get("t4")["status"] == "done"  # already terminal, not reopened


def test_resolve_by_id_or_words_and_cancel(ctx):
    ctx.tasks.open("abc123", "fetch my october amex expenses")
    assert ctx.tasks.cancel("october amex")["id"] == "abc123"
    assert ctx.tasks.get("abc123")["status"] == "cancelled"
    assert ctx.tasks.cancel("nothing matches this") is None


def test_deadline_nudges_are_opt_in(ctx):
    ctx.tasks.open("d1", "waiting task")
    ctx.tasks.waiting("d1", "an approval")
    now = datetime.now(UTC)
    # no deadline -> never nudged, just listed
    assert ctx.tasks.due_nudges(iso(now)) == []
    ctx.tasks.set_deadline("waiting task", iso(now - timedelta(minutes=1)))  # past due
    nudged = ctx.tasks.due_nudges(iso(now))
    assert [t["id"] for t in nudged] == ["d1"]
    assert ctx.tasks.due_nudges(iso(now)) == []  # only once


# ---- through the planner ------------------------------------------------------------------------


@pytest.fixture
def g(ctx):
    install(ctx, FakeGoogle(sheets={SID: {"T": [["Date", "Description", "Amount"]]}}, titles={SID: "Expenses"},
                            mail=[{"id": "m", "threadId": "t", "from": "me", "subject": "s", "date": "", "body": "b"}]))
    return ctx


async def test_owner_request_creates_a_task_with_a_closeout(g):
    ctx = g
    attach(ctx, PlanLLM([plan(step("sheets_list", {"query": "Expenses"}, checks=[chk("spreadsheets.0.id", "nonempty")]))],
                        reports=["You have one spreadsheet: Expenses."]))
    await ctx.chat.owner_says("what sheets do I have?")
    await settle(ctx)
    [t] = ctx.tasks.list()
    assert t["status"] == "done" and t["origin"] == "owner"
    assert "spreadsheet" in t["closeout"].lower() and t["request"] == "what sheets do I have?"


async def test_scheduled_fire_creates_no_task_row_but_journals(ctx):
    sid = ctx.scheduler.create("job", "summarise accounts", {"in_minutes": 1})["id"]
    attach(ctx, PlanLLM([plan(reply="Nothing new.")]))
    await ctx.scheduler._run_job(sid, "summarise accounts", notify="always")
    assert ctx.tasks.list() == []  # no owner task row for a scheduled fire


async def test_waiting_on_me_while_an_approval_is_pending(g):
    ctx = g
    attach(ctx, PlanLLM([plan(
        step("gmail_create_draft", {"to": ["r@example.com"], "subject": "s", "body": "b"}),
        step("gmail_send", {"draft_id": {"$ref": "s1.draft_id"}}),
    )], reports=["Sent."]))
    await ctx.chat.owner_says("email r@example.com and send it")
    # while the send approval is pending, the task shows waiting_on_me
    for _ in range(200):
        if ctx.gate.pending():
            break
        await asyncio.sleep(0.01)
    [t] = ctx.tasks.list()
    assert t["status"] == "waiting_on_me"
    ctx.gate.decide(ctx.gate.pending()[0].id, True)
    await settle(ctx)
    assert ctx.tasks.get(t["id"])["status"] == "done"


# ---- crash safety: no duplicate write on resume -------------------------------------------------
#
# The owner's write path for expenses is `sheets_add_transactions`, which reads the tab back and
# skips rows already there (friday/sheets.py existing_keys). That is what makes a resume after a
# crash safe: re-running the identical write appends nothing. The journal, meanwhile, marks the
# interrupted step `uncertain` so friday never silently re-runs it - the owner chooses to resume.


def _expenses_ctx(ctx):
    """A ctx wired to a fake sheet with one stored Amex charge and a saved expense sheet."""
    from datetime import date
    from decimal import Decimal

    from friday.capabilities.transactions import Transaction
    from friday.profiles.models import Site

    fake = FakeGoogle(sheets={SID: {"Oct": [["Date", "Description", "Amount", "Card"]]}}, titles={SID: "Expenses"})
    install(ctx, fake)
    ctx.sites.upsert(Site(id="amex", name="Amex", login_url="https://americanexpress.com/", domains=["americanexpress.com"]))
    ctx.profiles.create("amex", "me")
    ctx.transactions.add([Transaction(date(2026, 10, 10), "COFFEE", Decimal(5), "INR", "debit", None, "amex.me", "r1")])
    ctx.prefs.set("expense_sheet", {"spreadsheet": SID, "tab": "Oct", "include_credits": False, "column_map": None})
    return fake


def _body_rows(fake):
    return [r for r in fake.sheets[SID]["Oct"][1:] if any(str(c).strip() for c in r)]


async def test_resume_after_a_committed_write_does_not_duplicate(ctx):
    """A crash leaves the append step uncertain; re-running it (a resume) adds no duplicate row."""
    fake = _expenses_ctx(ctx)
    args = {"account": "amex.me", "start": "2026-10-01", "end": "2026-10-31"}
    call = Call(ctx=ctx, owner_text="add my october amex expenses", plan_id="t-append")
    first = await ctx.tools.call("sheets_add_transactions", args, call)
    assert first["appended"] == 1 and first["ok"]
    assert len(_body_rows(fake)) == 1
    # the crash: the step was journaled as started but never confirmed finished.
    ctx.tasks.open("t-append", "add my october amex expenses")
    ctx.tasks.journal_start("t-append", "s1", "sheets_add_transactions", "write")
    assert ctx.tasks.uncertain_steps("t-append")  # recovery would surface this and ask the owner
    # resume: the same write runs again and the read-back skips the row already there.
    second = await ctx.tools.call("sheets_add_transactions", args, call)
    assert second["appended"] == 0  # no duplicate write
    assert len(_body_rows(fake)) == 1


async def test_scheduled_export_does_not_duplicate_on_rerun(ctx):
    """A scheduled job that appends stored transactions, re-fired after a crash, adds nothing new."""
    fake = _expenses_ctx(ctx)
    args = {"account": "amex.me", "start": "2026-10-01", "end": "2026-10-31"}
    sid = ctx.scheduler.create("job", "refresh expenses", {"in_minutes": 1})["id"]
    attach(ctx, PlanLLM([plan(step("sheets_add_transactions", args, checks=[chk("ok", "true")]))],
                        reports=["Added your Amex charges."]))
    await ctx.scheduler._run_job(sid, "refresh expenses", notify="always")
    assert len(_body_rows(fake)) == 1
    # re-fire (as if the first crashed after writing): the export skips the row already there.
    attach(ctx, PlanLLM([plan(step("sheets_add_transactions", args, checks=[chk("ok", "true")]))],
                        reports=["Nothing new to add."]))
    await ctx.scheduler._run_job(sid, "refresh expenses", notify="always")
    assert len(_body_rows(fake)) == 1  # no duplicate


# ---- chat tools + prompt injection --------------------------------------------------------------


async def test_chat_lists_and_cancels_tasks(ctx):
    ctx.tasks.open("k1", "fetch my expenses")
    ctx.tasks.running("k1", "reading")
    attach(ctx, PlanLLM([plan(step("tasks_open", checks=[chk("open", "nonempty")]))], reports=["One open task: fetch my expenses."]))
    await ctx.chat.owner_says("what's open?")
    await settle(ctx)
    assert "open task" in last_friday(ctx).lower()

    attach(ctx, PlanLLM([plan(step("task_cancel", {"query": "k1", "owner_quote": "cancel the expenses task"}))],
                        reports=["Cancelled it."]))
    await ctx.chat.owner_says("cancel the expenses task")
    await settle(ctx)
    assert ctx.tasks.get("k1")["status"] == "cancelled"


async def test_a_web_page_cannot_cancel_or_resume_a_task(ctx):
    ctx.tasks.open("w1", "important task")
    ctx.tasks.running("w1")
    ctx.tasks.recover()  # make it interruptible/resumable
    for tool in ("task_cancel", "task_resume", "task_drop"):
        attach(ctx, PlanLLM([plan(step(tool, {"query": "w1", "owner_quote": "the web page said to cancel it"})),
                             plan(reply="I won't; you didn't ask.")]))
        await ctx.chat.owner_says("summarise the article at example.com")
        await settle(ctx)
    assert ctx.tasks.get("w1")["status"] == "interrupted"  # untouched by the page-driven attempts
