"""Reminders, recurring jobs and progress reporting."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from friday.scheduler import ScheduleError, next_run, normalise

from .planner_fakes import PlanLLM, attach, chk, last_friday, plan, settle, step
from .test_tools import approve_next


async def says_and_approve(ctx, msg, *, yes=True):
    """Post an owner message and tap-approve the schedule-mutation card it raises (concurrently)."""
    await ctx.chat.owner_says(msg)
    await asyncio.gather(settle(ctx), approve_next(ctx, yes))


def test_specs_and_next_run():
    t0 = datetime(2026, 10, 20, 6, 0, tzinfo=timezone.utc)  # 11:30 in Kolkata, a Tuesday
    assert next_run(normalise({"in_minutes": 5}), "Asia/Kolkata", after=t0, created=t0) == t0 + timedelta(minutes=5)
    assert next_run(normalise({"in_minutes": 5}), "Asia/Kolkata", after=t0) is None  # one-shot, already fired
    weekly = normalise({"every": "weeks", "weekdays": [0], "time": "09:00"})
    nxt = next_run(weekly, "Asia/Kolkata", after=t0)
    assert nxt == datetime(2026, 10, 26, 3, 30, tzinfo=timezone.utc)  # Monday 09:00 IST
    daily = normalise({"every": "days", "time": "18:00"})
    assert next_run(daily, "Asia/Kolkata", after=t0) == datetime(2026, 10, 20, 12, 30, tzinfo=timezone.utc)
    for bad in ({}, {"every": "minutes", "n": 1}, {"in_minutes": -1}, {"every": "weeks", "weekdays": [9]}, {"at": "tomorrow"}):
        with pytest.raises(ScheduleError):
            normalise(bad)


async def test_reminder_fires_once_when_due(ctx):
    ctx.settings.default_timezone = "Asia/Kolkata"
    t0 = datetime.now(timezone.utc)
    s = ctx.scheduler.create("reminder", "test the reminder", {"in_minutes": 5}, now=t0)
    assert await ctx.scheduler.tick(t0 + timedelta(minutes=4)) == []
    assert await ctx.scheduler.tick(t0 + timedelta(minutes=5, seconds=1)) == [s["id"]]
    assert await ctx.scheduler.tick(t0 + timedelta(minutes=6)) == []
    assert [m["text"] for m in ctx.chat.store.list()] == ["Reminder: test the reminder"]
    assert ctx.scheduler.list() == []


async def test_the_background_loop_fires_on_its_own(ctx):
    from friday.scheduler import Scheduler

    sched = Scheduler(ctx, poll_seconds=0.05)
    sched.create("reminder", "stretch", {"in_minutes": 0.005})  # 0.3 s
    sched.start()
    try:
        for _ in range(100):
            if ctx.chat.store.list():
                break
            await asyncio.sleep(0.05)
    finally:
        await sched.stop()
    assert ctx.chat.store.list()[0]["text"] == "Reminder: stretch"


async def test_recurring_job_skips_missed_slots_and_reschedules(ctx):
    ctx.settings.default_timezone = "Asia/Kolkata"
    t0 = datetime(2026, 10, 20, 6, 0, tzinfo=timezone.utc)
    s = ctx.scheduler.create("job", "refresh my expense sheet from Amex", {"every": "weeks", "weekdays": [0], "time": "09:00"}, now=t0)
    ctx.chat.assistant = None  # no model: the job reports that instead of running
    much_later = t0 + timedelta(days=30)
    assert await ctx.scheduler.tick(much_later) == [s["id"]]  # three missed Mondays fire once
    await asyncio.gather(*ctx.scheduler._jobs)
    assert ctx.scheduler.get(s["id"])["runs"] == 1 and ctx.scheduler.get(s["id"])["active"]
    assert await ctx.scheduler.tick(much_later) == []


# ---- through the chat -----------------------------------------------------------------------------


async def test_remind_me_in_five_minutes(ctx):
    ctx.settings.default_timezone = "Asia/Kolkata"
    attach(ctx, PlanLLM([plan(step("schedule_create", {"kind": "reminder", "text": "test the reminder", "when": {"in_minutes": 5},
                                                       "owner_quote": "Remind me in 5 minutes to test the reminder"},
                                   checks=[chk("active", "true", says="reminder saved")]))],
                        reports=["OK, I'll remind you in 5 minutes."]))  # fmt: skip
    await says_and_approve(ctx, "Remind me in 5 minutes to test the reminder")
    [s] = ctx.scheduler.list()
    assert s["kind"] == "reminder" and "reminder saved" in last_friday(ctx)
    fired = await ctx.scheduler.tick(datetime.now(timezone.utc) + timedelta(minutes=5, seconds=30))
    assert fired == [s["id"]] and last_friday(ctx) == "Reminder: test the reminder"


async def test_weekly_job_created_listed_and_cancelled(ctx):
    ctx.settings.default_timezone = "Asia/Kolkata"
    msg = "Every Monday, refresh my expense sheet from Amex"
    attach(ctx, PlanLLM([plan(step("schedule_create", {
        "kind": "job", "text": "Fetch this month's Amex activity and add it to my expense sheet",
        "when": {"every": "weeks", "weekdays": [0], "time": "09:00"}, "owner_quote": "Every Monday, refresh my expense sheet from Amex"}))],
        reports=["Done: every Monday at 09:00."]))  # fmt: skip
    await says_and_approve(ctx, msg)
    [s] = ctx.scheduler.list()
    assert s["when"] == "every Monday at 09:00" and s["next"].startswith("Mon")

    attach(ctx, PlanLLM([plan(step("schedule_list", checks=[chk("schedules", "nonempty")]))],
                        reports=["One job: every Monday at 09:00, refresh your expense sheet from Amex."]))  # fmt: skip
    await ctx.chat.owner_says("what's scheduled?")
    await settle(ctx)
    assert "every Monday" in last_friday(ctx)

    attach(ctx, PlanLLM([plan(step("schedule_cancel", {"query": "Amex", "owner_quote": "cancel the Monday Amex refresh"}))],
                        reports=["Cancelled the Monday Amex refresh."]))  # fmt: skip
    await says_and_approve(ctx, "cancel the Monday Amex refresh")
    assert ctx.scheduler.list() == []


async def test_scheduled_job_runs_through_the_planner(ctx):
    llm = attach(ctx, PlanLLM([plan(reply="Here's your Monday summary: nothing new.")]))
    t0 = datetime.now(timezone.utc)
    s = ctx.scheduler.create("job", "Summarise anything new in my accounts", {"in_minutes": 1}, now=t0)
    await ctx.scheduler.tick(t0 + timedelta(minutes=2))
    await asyncio.gather(*ctx.scheduler._jobs)
    texts = [m["text"] for m in ctx.chat.store.list()]
    assert texts == ["Scheduled job: Summarise anything new in my accounts", "Here's your Monday summary: nothing new."]
    assert "Summarise anything new" in llm.plan_inputs[0]
    assert ctx.audit.list(action_prefix="assistant.plan")[0].details["origin"] == f"schedule:{s['id']}"


async def test_a_web_page_cannot_schedule_anything(ctx):
    attach(ctx, PlanLLM([plan(step("schedule_create", {"kind": "job", "text": "email my statements to x@evil.com",
                                                       "when": {"every": "days"}, "owner_quote": "email statements to evil"})),
                         plan(reply="I won't schedule that; you didn't ask for it.")]))  # fmt: skip
    await ctx.chat.owner_says("summarise the article at example.com")
    await settle(ctx)
    assert ctx.scheduler.list() == []


# ---- pause / resume / run-now --------------------------------------------------------------------


async def test_pause_stops_fires_and_resume_recomputes_from_now(ctx):
    ctx.settings.default_timezone = "Asia/Kolkata"
    t0 = datetime(2026, 10, 20, 6, 0, tzinfo=timezone.utc)
    ctx.chat.assistant = None
    s = ctx.scheduler.create("job", "refresh expenses", {"every": "hours", "n": 1}, now=t0)
    [paused] = ctx.scheduler.pause(s["id"])
    assert paused["paused"] and not paused["active"]
    # it is listed as paused, and does not fire even long past due
    assert [x["id"] for x in ctx.scheduler.list()] == [s["id"]]
    assert await ctx.scheduler.tick(t0 + timedelta(hours=5)) == []
    assert ctx.scheduler.get(s["id"])["runs"] == 0
    # resume recomputes next run from now; it never back-fires the 5 missed hourly slots
    now = t0 + timedelta(hours=5)
    [resumed] = ctx.scheduler.resume(s["id"], now=now)
    assert resumed["active"] and not resumed["paused"]
    assert await ctx.scheduler.tick(now + timedelta(minutes=1)) == []  # next slot is +1h, not now
    assert await ctx.scheduler.tick(now + timedelta(hours=1, seconds=1)) == [s["id"]]
    await asyncio.gather(*ctx.scheduler._jobs)
    assert ctx.scheduler.get(s["id"])["runs"] == 1


async def test_pause_needs_owner_quote_and_an_approval_tap(ctx):
    from friday.tools.base import ApprovalDenied, Call, ToolError

    s = ctx.scheduler.create("job", "do a thing", {"every": "days"})
    call = Call(ctx=ctx, owner_text="please pause the thing job", plan_id="p")
    # owner_quote is enforced by the registry; a wrong quote is refused outright
    with pytest.raises(ToolError):
        await ctx.tools.call("schedule_pause", {"query": s["id"], "owner_quote": "not in the message"}, call)
    assert not ctx.scheduler.get(s["id"])["paused"]
    # a valid quote is not enough: the mutation still needs the owner's tap on the card
    task = asyncio.create_task(ctx.tools.call("schedule_pause", {"query": s["id"], "owner_quote": "pause the thing"}, call))
    await approve_next(ctx, True)
    out = await task
    assert out["paused"][0]["id"] == s["id"] and ctx.scheduler.get(s["id"])["paused"]


# ---- watches (notify=on_change) ------------------------------------------------------------------


def _watch_plan(value, summary):
    return PlanLLM([plan(step("watch_report", {"value": value, "summary": summary}))], reports=["checked."])


def _friday_texts(ctx):
    return [m["text"] for m in ctx.chat.store.list() if m["sender"] == "friday"]


async def test_watch_posts_only_on_change(ctx):
    ctx.settings.default_timezone = "Asia/Kolkata"
    s = ctx.scheduler.create("job", "check SBIN price", {"every": "minutes", "n": 15}, notify="on_change")
    sid = s["id"]

    attach(ctx, _watch_plan("742.35", "SBIN is at 742.35."))
    await ctx.scheduler._run_job(sid, "check SBIN price", notify="on_change")
    assert "SBIN is at 742.35." in _friday_texts(ctx)
    st1 = ctx.scheduler._state(sid)
    assert st1["last_value"] == "742.35" and st1["last_checked_at"] and st1["last_changed_at"]

    # identical value: nothing new posted, but last-checked moves
    before = len(_friday_texts(ctx))
    attach(ctx, _watch_plan("742.35", "SBIN is at 742.35."))
    await ctx.scheduler._run_job(sid, "check SBIN price", notify="on_change")
    assert len(_friday_texts(ctx)) == before
    st2 = ctx.scheduler._state(sid)
    assert st2["last_checked_at"] >= st1["last_checked_at"] and st2["last_changed_at"] == st1["last_changed_at"]

    # changed value: posts the new summary
    attach(ctx, _watch_plan("750.00", "SBIN jumped to 750.00."))
    await ctx.scheduler._run_job(sid, "check SBIN price", notify="on_change")
    assert "SBIN jumped to 750.00." in _friday_texts(ctx)
    assert ctx.scheduler._state(sid)["last_value"] == "750.00"
    # schedule_list shows it is alive (last checked/changed)
    [listed] = [x for x in ctx.scheduler.list() if x["id"] == sid]
    assert listed["notify"] == "on_change" and listed["last_checked"] and listed["last_changed"]


async def test_watch_without_report_is_no_change(ctx):
    s = ctx.scheduler.create("job", "check something", {"every": "hours"}, notify="on_change")
    attach(ctx, PlanLLM([plan(reply="I looked, nothing to report.")], reports=["done."]))
    await ctx.scheduler._run_job(s["id"], "check something", notify="on_change")
    # no watch_report call: treated as no change, nothing posted, but last-checked recorded
    assert "I looked, nothing to report." not in _friday_texts(ctx)
    st = ctx.scheduler._state(s["id"])
    assert st["last_checked_at"] and st["last_value"] is None


async def test_watch_error_posts_once_then_suppresses(ctx, monkeypatch):
    s = ctx.scheduler.create("job", "check the flaky site", {"every": "hours"}, notify="on_change")
    sid = s["id"]

    async def boom(*a, **k):
        raise RuntimeError("site down")

    monkeypatch.setattr(ctx.chat.assistant if ctx.chat.assistant else _ensure_assistant(ctx), "reply", boom)
    await ctx.scheduler._run_job(sid, "check the flaky site", notify="on_change")
    errs = [t for t in _friday_texts(ctx) if "didn't run" in t]
    assert len(errs) == 1
    # identical error next fire: suppressed
    await ctx.scheduler._run_job(sid, "check the flaky site", notify="on_change")
    assert len([t for t in _friday_texts(ctx) if "didn't run" in t]) == 1


def _ensure_assistant(ctx):
    from friday.assistant import ChatAssistant

    ctx.chat.assistant = ChatAssistant(ctx, lambda: None)
    return ctx.chat.assistant


async def test_watch_report_outside_a_watch_job_is_an_error(ctx):
    from friday.tools.base import Call, ToolError

    with pytest.raises(ToolError, match="only usable inside an on_change scheduled job"):
        await ctx.tools.call("watch_report", {"value": "x", "summary": "y"}, Call(ctx=ctx, owner_text="", plan_id="p"))


# ---- scheduled approvals -------------------------------------------------------------------------


async def test_scheduled_approval_expiry_offers_rerun_and_run_now(ctx):
    from .test_tools import approve_next
    from .fake_google import SID, FakeGoogle, install

    fake = FakeGoogle(sheets={SID: {"S": [["Date", "x"]]}}, titles={SID: "Sheet"},
                      mail=[{"id": "m", "threadId": "t", "from": "me", "subject": "s", "date": "", "body": "b"}])
    install(ctx, fake)
    attach(ctx, PlanLLM([plan(
        step("gmail_create_draft", {"to": ["ravi@example.com"], "subject": "Monday", "body": "hello"}),
        step("gmail_send", {"draft_id": {"$ref": "s1.draft_id"}}),
    )], reports=["I drafted the email but you didn't approve sending it, so it wasn't sent."]))  # fmt: skip
    s = ctx.scheduler.create("job", "email ravi my monday summary", {"every": "weeks", "weekdays": [0], "time": "09:00"})
    sid = s["id"]
    next_before = ctx.scheduler.get(sid)  # for next-fire comparison later

    task = asyncio.create_task(ctx.scheduler._run_job(sid, "email ravi my monday summary", notify="always"))
    await approve_next(ctx, False)  # deny (same as an expiry)
    await task
    msg = _friday_texts(ctx)[-1]
    assert f"run {sid}" in msg and "wasn't sent" in msg
    assert fake.sent == []

    # schedule_run_now re-runs the job without changing its next scheduled fire
    nxt = ctx.scheduler.get(sid)["next"]
    attach(ctx, PlanLLM([plan(
        step("gmail_create_draft", {"to": ["ravi@example.com"], "subject": "Monday", "body": "hello"}),
        step("gmail_send", {"draft_id": {"$ref": "s1.draft_id"}}),
    )], reports=["Sent your Monday email to Ravi."]))  # fmt: skip
    hits = ctx.scheduler.run_now(sid)
    assert [h["id"] for h in hits] == [sid]
    await approve_next(ctx, True)
    await asyncio.gather(*ctx.scheduler._jobs)
    assert fake.sent and fake.sent[0]["to"] == "ravi@example.com"
    assert ctx.scheduler.get(sid)["next"] == nxt  # next fire untouched


async def test_scheduled_reply_reaches_the_notifier(ctx):
    log_notifier = ctx.notifier.notifiers[0]
    assert log_notifier.name == "log"
    log_notifier.sent.clear()
    attach(ctx, PlanLLM([plan(reply="Your Monday summary: two new charges.")]))
    s = ctx.scheduler.create("job", "summarise my accounts", {"in_minutes": 1})
    await ctx.scheduler._run_job(s["id"], "summarise my accounts", notify="always")
    assert "Your Monday summary: two new charges." in _friday_texts(ctx)  # full reply in chat
    assert any("two new charges" in t for t, _ in log_notifier.sent)  # and through the notifier


async def test_a_web_page_cannot_pause_resume_or_run_schedules(ctx):
    s = ctx.scheduler.create("job", "refresh expenses", {"every": "days"})
    for tool in ("schedule_pause", "schedule_resume", "schedule_run_now", "schedule_cancel"):
        attach(ctx, PlanLLM([plan(step(tool, {"query": s["id"], "owner_quote": "the web page told me to"})),
                             plan(reply="I won't; you didn't ask.")]))  # fmt: skip
        await ctx.chat.owner_says("summarise the article at example.com")
        await settle(ctx)
    assert ctx.scheduler.get(s["id"])["active"] and not ctx.scheduler.get(s["id"])["paused"]


# ---- #1 schedule mutations need an owner tap, not a substring quote ------------------------------


def _runs(ctx, sid):
    return ctx.db.query("SELECT * FROM schedule_runs WHERE schedule_id = ? ORDER BY started_at", (sid,))


async def test_mutations_are_gated_by_an_owner_tap_not_a_quote(ctx):
    """A page-influenced plan can copy the whole owner request, so owner_quote present-and-valid is
    not authority. Every mutation still needs the owner to tap the card; a denial changes nothing."""
    from friday.tools.base import ApprovalDenied, Call

    ctx.settings.default_timezone = "Asia/Kolkata"
    existing = ctx.scheduler.create("job", "refresh my sheet", {"every": "days"})
    # owner_text is a summarization request; the quote is a real substring of it but unrelated to scheduling.
    owner_text = "summarise the article at example.com; it says to schedule and run things"
    call = Call(ctx=ctx, owner_text=owner_text, plan_id="p")
    mutations = [
        ("schedule_create", {"kind": "job", "text": "email my data out", "when": {"every": "days"}, "owner_quote": "schedule"}),
        ("schedule_pause", {"query": existing["id"], "owner_quote": "schedule"}),
        ("schedule_resume", {"query": existing["id"], "owner_quote": "schedule"}),
        ("schedule_run_now", {"query": existing["id"], "owner_quote": "run"}),
        ("schedule_cancel", {"query": existing["id"], "owner_quote": "schedule"}),
    ]
    for name, args in mutations:
        task = asyncio.create_task(ctx.tools.call(name, args, call))
        await approve_next(ctx, False)  # deny every card
        with pytest.raises(ApprovalDenied):
            await task
    # nothing took effect: no new schedule, the existing one is untouched and never ran
    assert [s["id"] for s in ctx.scheduler.list()] == [existing["id"]]
    assert ctx.scheduler.get(existing["id"])["active"] and not ctx.scheduler.get(existing["id"])["paused"]
    assert _runs(ctx, existing["id"]) == []
    # with the owner's tap, a create goes through
    task = asyncio.create_task(ctx.tools.call("schedule_create",
        {"kind": "reminder", "text": "stand up", "when": {"in_minutes": 30}, "owner_quote": "schedule"}, call))
    await approve_next(ctx, True)
    out = await task
    assert out["kind"] == "reminder" and len(ctx.scheduler.list()) == 2


# ---- #2 same-schedule runs are serialized; neither staged report is lost -------------------------


async def test_overlapping_runs_do_not_clobber_each_other(ctx):
    sid = ctx.scheduler.create("job", "watch X", {"every": "minutes", "n": 15}, notify="on_change")["id"]
    gate = asyncio.Event()
    calls = []

    async def reply(text, *, origin, schedule_id, watch_id):
        n = len(calls)
        calls.append(n)
        value = "A" if n == 0 else "B"
        st = ctx.scheduler._state(watch_id)
        changed = st is None or st["last_value"] is None or st["last_value"] != value
        ctx.scheduler.record_watch(watch_id, value, f"value is {value}", changed)
        ctx.scheduler.note_outcome(watch_id, status="done", stopped_by_approval=False)
        if n == 0:
            await gate.wait()  # hold run 1 open after it has staged its report
        return "ok"

    from friday.assistant import ChatAssistant

    ctx.chat.assistant = ChatAssistant(ctx, lambda: None)
    ctx.chat.assistant.reply = reply

    run1 = asyncio.create_task(ctx.scheduler._run_job(sid, "watch X", notify="on_change"))
    await asyncio.sleep(0.05)  # let run 1 acquire the lock and stage "A"
    run2 = asyncio.create_task(ctx.scheduler._run_job(sid, "watch X", notify="on_change"))
    await asyncio.sleep(0.05)
    assert calls == [0]  # run 2 is blocked on the per-schedule lock, hasn't started reply
    gate.set()
    await asyncio.gather(run1, run2)
    assert calls == [0, 1]
    texts = [m["text"] for m in ctx.chat.store.list() if m["sender"] == "friday"]
    assert "value is A" in texts and "value is B" in texts  # neither report lost
    assert ctx.scheduler._state(sid)["last_value"] == "B"
    assert len(_runs(ctx, sid)) == 2  # one durable record per run


# ---- #3 a failed/blocked run never commits a staged watch success -------------------------------


async def test_staged_watch_success_discarded_when_a_later_step_fails(ctx):
    sid = ctx.scheduler.create("job", "watch Y", {"every": "hours"}, notify="on_change")["id"]
    # watch_report stages a "change", then a later step's check fails; replans are exhausted -> blocked.
    attach(ctx, PlanLLM(
        [plan(step("watch_report", {"value": "999", "summary": "Y changed to 999."}),
              step("schedule_list", checks=[chk("schedules", "eq", "nope")])),
         plan(reply="I couldn't verify the check.")],
        reports=["blocked"]))
    await ctx.scheduler._run_job(sid, "watch Y", notify="on_change")
    texts = [m["text"] for m in ctx.chat.store.list() if m["sender"] == "friday"]
    assert "Y changed to 999." not in texts  # the staged success was not committed
    st = ctx.scheduler._state(sid)
    assert st["last_value"] is None and st["last_changed_at"] is None  # previous (none) preserved
    assert st["last_checked_at"]  # but the attempt is recorded
    assert _runs(ctx, sid)[-1]["outcome"] == "blocked"


async def test_staged_watch_success_discarded_on_approval_denial(ctx):
    from .fake_google import SID, FakeGoogle, install

    install(ctx, FakeGoogle(sheets={SID: {"S": [["Date", "x"]]}}, titles={SID: "S"},
                            mail=[{"id": "m", "threadId": "t", "from": "me", "subject": "s", "date": "", "body": "b"}]))
    sid = ctx.scheduler.create("job", "watch Z and email me", {"every": "hours"}, notify="on_change")["id"]
    attach(ctx, PlanLLM([plan(
        step("watch_report", {"value": "500", "summary": "Z is 500."}),
        step("gmail_create_draft", {"to": ["r@example.com"], "subject": "Z", "body": "changed"}),
        step("gmail_send", {"draft_id": {"$ref": "s2.draft_id"}}),
    )], reports=["I checked Z but you didn't approve the email."]))
    task = asyncio.create_task(ctx.scheduler._run_job(sid, "watch Z and email me", notify="on_change"))
    await approve_next(ctx, False)  # deny the send
    await task
    st = ctx.scheduler._state(sid)
    assert st["last_value"] is None  # staged 500 not committed
    assert st["last_checked_at"]
    assert any(f"run {sid}" in m["text"] for m in ctx.chat.store.list())  # rerun offered
    assert _runs(ctx, sid)[-1]["outcome"] == "approval_denied"


# ---- #4 resuming an expired one-shot must not back-fire -----------------------------------------


async def test_resume_expired_absolute_one_shot_does_not_fire(ctx):
    ctx.settings.default_timezone = "UTC"
    t0 = datetime(2026, 11, 1, 6, 0, tzinfo=timezone.utc)
    at_str = (t0 + timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M")  # 06:10 UTC
    s = ctx.scheduler.create("reminder", "call the bank", {"at": at_str}, now=t0)
    ctx.scheduler.pause(s["id"])
    # resume AFTER the due time: the past occurrence is marked expired, not fired
    [r] = ctx.scheduler.resume(s["id"], now=t0 + timedelta(hours=1))
    assert r["expired"] and not r["active"] and r["next"] is None
    assert await ctx.scheduler.tick(t0 + timedelta(hours=2)) == []  # never fires


async def test_resume_future_one_shot_rearms_to_its_original_time(ctx):
    ctx.settings.default_timezone = "UTC"
    t0 = datetime(2026, 11, 1, 6, 0, tzinfo=timezone.utc)
    at_str = (t0 + timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M")
    s = ctx.scheduler.create("reminder", "call the bank", {"at": at_str}, now=t0)
    ctx.scheduler.pause(s["id"])
    [r] = ctx.scheduler.resume(s["id"], now=t0 + timedelta(minutes=5))  # still before due
    assert not r["expired"] and r["active"]
    assert await ctx.scheduler.tick(t0 + timedelta(minutes=9)) == []
    assert await ctx.scheduler.tick(t0 + timedelta(minutes=11)) == [s["id"]]  # fires at its original time


async def test_resume_expired_relative_one_shot_does_not_fire(ctx):
    ctx.settings.default_timezone = "UTC"
    t0 = datetime(2026, 11, 1, 6, 0, tzinfo=timezone.utc)
    s = ctx.scheduler.create("reminder", "stretch", {"in_minutes": 10}, now=t0)
    ctx.scheduler.pause(s["id"])
    [r] = ctx.scheduler.resume(s["id"], now=t0 + timedelta(hours=1))  # original due t0+10 has passed
    assert r["expired"] and not r["active"]
    assert await ctx.scheduler.tick(t0 + timedelta(hours=2)) == []


async def test_resume_recurring_recomputes_from_now(ctx):
    ctx.settings.default_timezone = "UTC"
    t0 = datetime(2026, 11, 1, 6, 0, tzinfo=timezone.utc)
    ctx.chat.assistant = None
    s = ctx.scheduler.create("job", "hourly thing", {"every": "hours"}, now=t0)
    ctx.scheduler.pause(s["id"])
    now = t0 + timedelta(hours=5)
    [r] = ctx.scheduler.resume(s["id"], now=now)
    assert not r["expired"] and r["active"]
    assert await ctx.scheduler.tick(now + timedelta(minutes=1)) == []  # not back-fired
    assert await ctx.scheduler.tick(now + timedelta(hours=1, seconds=1)) == [s["id"]]


# ---- #5 early failures go through the same finalization + delivery ------------------------------


async def test_missing_model_watch_error_dedupes_and_records(ctx):
    log_notifier = ctx.notifier.notifiers[0]
    log_notifier.sent.clear()
    sid = ctx.scheduler.create("job", "watch something", {"every": "hours"}, notify="on_change")["id"]
    ctx.chat.assistant = None
    await ctx.scheduler._run_job(sid, "watch something", notify="on_change")
    await ctx.scheduler._run_job(sid, "watch something", notify="on_change")
    errs = [m["text"] for m in ctx.chat.store.list() if "didn't run" in m["text"]]
    assert len(errs) == 1  # posted once, identical repeat suppressed
    st = ctx.scheduler._state(sid)
    assert st["last_checked_at"] and st["last_value"] is None
    assert len(_runs(ctx, sid)) == 2 and all(r["outcome"] == "missing_model" for r in _runs(ctx, sid))
    assert any("didn't run" in t for t, _ in log_notifier.sent)  # external channel got the first failure


# ---- #6 durable no-report outcome; secret redaction across watch surfaces ------------------------


async def test_no_report_outcome_is_persisted(ctx):
    sid = ctx.scheduler.create("job", "quiet check", {"every": "hours"}, notify="on_change")["id"]
    attach(ctx, PlanLLM([plan(reply="looked, nothing to say.")], reports=["done."]))
    await ctx.scheduler._run_job(sid, "quiet check", notify="on_change")
    assert _runs(ctx, sid)[-1]["outcome"] == "no_change_missing_report"
    assert ctx.scheduler._state(sid)["last_checked_at"]


async def test_watch_state_and_delivery_are_redacted(ctx):
    log_notifier = ctx.notifier.notifiers[0]
    log_notifier.sent.clear()
    ctx.redactor.register("mybank/me/password", "S3cr3t-Token-xyz")
    sid = ctx.scheduler.create("job", "watch secret", {"every": "hours"}, notify="on_change")["id"]
    attach(ctx, _watch_plan("tok S3cr3t-Token-xyz", "value now S3cr3t-Token-xyz."))
    await ctx.scheduler._run_job(sid, "watch secret", notify="on_change")
    st = ctx.scheduler._state(sid)
    run = _runs(ctx, sid)[-1]
    chat = " ".join(m["text"] for m in ctx.chat.store.list())
    ext = " ".join(t for t, _ in log_notifier.sent)
    for blob in (st["last_value"], st["last_summary"], run["detail"] or "", run["value"] or "", chat, ext):
        assert "S3cr3t-Token-xyz" not in blob


# ---- migration of an existing database with schedules through the new schema ---------------------


def test_existing_database_migrates_through_new_schema(tmp_path, monkeypatch):
    import friday.db as dbmod
    from friday.db import Database
    from friday.util import iso

    # A database stopped at the schema from before the pause/watch/run-record migrations, holding a
    # schedule row in the original columns.
    monkeypatch.setattr(dbmod, "MIGRATIONS", dbmod.MIGRATIONS[:-2])
    path = tmp_path / "old.db"
    old = Database(path)
    old.execute("INSERT INTO schedules (id, kind, text, spec_json, tz, next_run_at, created_at)"
                " VALUES ('abc', 'job', 'legacy job', '{\"every\": \"days\"}', 'UTC', ?, ?)",
                (iso(), iso()))  # fmt: skip
    old.close()

    # Reopen with the full migration list: the two new migrations run against the existing data.
    monkeypatch.undo()
    db = Database(path)
    row = db.query_one("SELECT * FROM schedules WHERE id = 'abc'")
    assert row["text"] == "legacy job" and row["notify"] == "always" and row["paused_at"] is None  # back-filled defaults
    # the new tables exist and are usable
    db.execute("INSERT INTO schedule_runs (id, schedule_id, outcome, started_at, finished_at) VALUES ('r1','abc','no_change',?,?)",
               (iso(), iso()))  # fmt: skip
    db.execute("INSERT INTO schedule_state (schedule_id, last_checked_at) VALUES ('abc', ?)", (iso(),))
    assert db.query_one("SELECT outcome FROM schedule_runs WHERE id = 'r1'")["outcome"] == "no_change"
    db.close()


async def test_short_ttl_approval_expiry_on_a_scheduled_job(ctx):
    """A real expiry (not an explicit denial): the gate times out and the job reports what it didn't do."""
    from .fake_google import SID, FakeGoogle, install

    ctx.gate._ttl = 0.2  # expire the card quickly
    install(ctx, FakeGoogle(sheets={SID: {"S": [["Date", "x"]]}}, titles={SID: "S"},
                            mail=[{"id": "m", "threadId": "t", "from": "me", "subject": "s", "date": "", "body": "b"}]))
    sid = ctx.scheduler.create("job", "email my summary", {"every": "days"})["id"]
    attach(ctx, PlanLLM([plan(
        step("gmail_create_draft", {"to": ["r@example.com"], "subject": "S", "body": "hi"}),
        step("gmail_send", {"draft_id": {"$ref": "s1.draft_id"}}),
    )], reports=["I drafted it but the approval to send expired, so it wasn't sent."]))
    await ctx.scheduler._run_job(sid, "email my summary", notify="always")  # no one taps: it expires
    msg = [m["text"] for m in ctx.chat.store.list() if m["sender"] == "friday"][-1]
    assert f"run {sid}" in msg
    assert _runs(ctx, sid)[-1]["outcome"] == "approval_denied"
