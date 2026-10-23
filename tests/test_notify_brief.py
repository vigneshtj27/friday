"""Phase 5: expired reminders, the daily brief, the watch stop-on-block, and the delivery check."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from friday.brief import build_brief
from friday.config import Settings
from friday.notify import LogNotifier, MultiNotifier, Notifier, build_notifier
from friday.vault.masking import Redactor

from .planner_fakes import PlanLLM, attach, last_friday, plan, settle, step

# ---- 5.2 expired one-shot reminders show in the list --------------------------------------------


def test_a_missed_oneshot_reminder_shows_as_expired(ctx):
    now = datetime.now(UTC)
    s = ctx.scheduler.create("reminder", "call the bank", {"at": (now + timedelta(minutes=30)).isoformat()}, now=now)
    ctx.scheduler.pause(s["id"])
    # resume after its time has passed -> it won't fire, but must still be visible as expired
    ctx.scheduler.resume(s["id"], now=now + timedelta(hours=1))
    listed = ctx.scheduler.list()
    match = [x for x in listed if x["id"] == s["id"]]
    assert match and match[0]["expired"] is True and match[0]["active"] is False


# ---- 5.3 the daily brief ------------------------------------------------------------------------


def test_brief_is_silent_when_nothing_changed(ctx):
    assert build_brief(ctx) is None


def test_brief_collects_open_tasks_and_reminders(ctx):
    ctx.tasks.open("t1", "fetch my expenses")
    ctx.tasks.running("t1")
    now = datetime.now(UTC)
    ctx.scheduler.create("reminder", "pay rent", {"at": (now + timedelta(hours=2)).isoformat()}, now=now)
    text = build_brief(ctx, now=now)
    assert text and "Open tasks" in text and "fetch my expenses" in text
    assert "Reminders due soon" in text and "pay rent" in text


async def test_set_daily_brief_creates_a_recurring_schedule(ctx):
    scripted = PlanLLM([plan(step("set_daily_brief", {"on": True, "time": "08:00",
                      "owner_quote": "send me a daily brief at 8am"}))], reports=["Daily brief is on at 08:00."])
    attach(ctx, scripted)
    await ctx.chat.owner_says("send me a daily brief at 8am")
    await settle(ctx)
    briefs = [s for s in ctx.scheduler.list(include_done=True) if s["kind"] == "brief"]
    assert len(briefs) == 1


async def test_a_brief_schedule_fires_the_brief_not_the_planner(ctx):
    ctx.tasks.open("t9", "an open task")
    ctx.tasks.running("t9")
    now = datetime.now(UTC)
    ctx.scheduler.create("brief", "Your daily brief.", {"every": "days", "n": 1, "time": "08:00"}, now=now)
    # force it due and tick
    ctx.db.execute("UPDATE schedules SET next_run_at = ? WHERE kind = 'brief'", (now.isoformat(),))
    await ctx.scheduler.tick(now=now + timedelta(minutes=1))
    assert "daily brief" in last_friday(ctx).lower()


# ---- 5.4 a watch that is blocked stops instead of retrying ---------------------------------------


async def test_a_blocked_watch_is_paused_and_told(ctx):
    now = datetime.now(UTC)
    s = ctx.scheduler.create("job", "watch SBIN price", {"every": "minutes", "n": 15}, notify="on_change", now=now)
    # a run that hit a 403 / CAPTCHA
    await ctx.scheduler._finalize(s["id"], "ex1", now.isoformat(), on_change=True, outcome="blocked",
                                 reply="The site returned HTTP 403 Forbidden (bot detection).", pending=None)  # fmt: skip
    after = ctx.scheduler.get(s["id"])
    assert after["paused"] is True
    assert "paused" in last_friday(ctx).lower()


async def test_an_ordinary_watch_error_does_not_pause(ctx):
    now = datetime.now(UTC)
    s = ctx.scheduler.create("job", "watch something", {"every": "minutes", "n": 15}, notify="on_change", now=now)
    await ctx.scheduler._finalize(s["id"], "ex2", now.isoformat(), on_change=True, outcome="error",
                                 reply="The page took too long to load.", pending=None)  # fmt: skip
    assert ctx.scheduler.get(s["id"])["paused"] is False


# ---- 5.1 delivery check & never silently enabling a channel --------------------------------------


class _FailingChannel(Notifier):
    name = "telegram"

    async def verify(self) -> bool:
        raise RuntimeError("401 unauthorized")


class _OkChannel(Notifier):
    name = "whatsapp"

    async def verify(self) -> bool:
        return True


async def test_delivery_check_reports_each_channel():
    red = Redactor()
    multi = MultiNotifier([LogNotifier(), _FailingChannel(), _OkChannel()], red)
    assert set(multi.channels()) == {"telegram", "whatsapp"}
    result = await multi.verify()
    assert result == {"telegram": False, "whatsapp": True}


def test_ntfy_is_never_enabled_unless_asked():
    red = Redactor()
    # the owner configured telegram only; ntfy creds present but not requested -> not enabled
    settings = Settings(_env_file=None, notify="telegram", telegram_bot_token="t", telegram_chat_id="1",
                        ntfy_topic="secret-topic")  # fmt: skip
    multi = build_notifier(settings, red)
    assert "ntfy" not in multi.channels()
