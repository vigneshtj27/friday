"""Chat tools for the daily brief and the notification delivery check (friday/brief.py, notify/).

The brief is opt-in: the owner turns it on and it fires once a day, silent if nothing changed.
Turning it on/off and running a delivery check are owner actions on friday's own config, so they need
the owner's own words (owner_quote); they touch no site and send nothing outside the owner's own
already-configured channels.
"""

from __future__ import annotations

from typing import Any

from friday.brief import build_brief
from friday.tools.base import Call, Tool, ToolError

BRIEF_TEXT = "Your daily brief."


class DailyBriefNow(Tool):
    name = "daily_brief"
    description = "Show the daily brief now: pending approvals, open tasks, reminders due soon, changed watches, failed runs."
    schema = {"type": "object", "properties": {}}

    async def run(self, args: dict[str, Any], call: Call) -> Any:
        text = build_brief(call.ctx)
        return {"brief": text or "Nothing to report right now.", "empty": text is None}


class SetDailyBrief(Tool):
    name = "set_daily_brief"
    description = ("Turn the daily brief on at a time each day (e.g. \"08:00\"), or off. On is a recurring schedule that "
                  "fires once a day and stays silent if there's nothing to report. Use for \"send me a daily brief at 8am\".")
    schema = {"type": "object", "properties": {
        "on": {"type": "boolean"}, "time": {"type": "string", "description": "HH:MM in your timezone, when on"},
    }, "required": ["on"]}  # fmt: skip
    effect = "write"
    needs_owner_quote = True

    async def run(self, args: dict[str, Any], call: Call) -> Any:
        ctx = call.ctx
        existing = [s for s in ctx.scheduler.list(include_done=True) if s["kind"] == "brief"]
        if not args["on"]:
            for s in existing:
                ctx.scheduler.cancel(s["id"])
            return {"daily_brief": "off", "verified": ["daily brief turned off"]}
        hhmm = str(args.get("time") or "08:00")
        try:
            hh, mm = (int(x) for x in hhmm.split(":"))
            spec_time = f"{hh:02d}:{mm:02d}"
        except (ValueError, TypeError) as e:
            raise ToolError("time must look like 08:00") from e
        for s in existing:  # replace any prior brief schedule
            ctx.scheduler.cancel(s["id"])
        sched = ctx.scheduler.create("brief", BRIEF_TEXT, {"every": "days", "n": 1, "time": spec_time})
        return {"daily_brief": "on", "at": spec_time, "schedule": sched["id"],
                "verified": [f"daily brief on at {spec_time}"]}  # fmt: skip


class NotifyTest(Tool):
    name = "notify_test"
    description = ("Send a test to your notification channels and report which delivered. Only channels you've already "
                  "configured and approved are tried; this never enables a new one.")
    schema = {"type": "object", "properties": {}}
    effect = "write"
    needs_owner_quote = True

    async def run(self, args: dict[str, Any], call: Call) -> Any:
        notifier = call.ctx.notifier
        channels = notifier.channels() if hasattr(notifier, "channels") else []
        if not channels:
            return {"channels": [], "result": {}, "note": "No external channel is configured; approvals stay in this chat."}
        result = await notifier.verify()
        return {"channels": channels, "result": result,
                "verified": [f"{n}: {'delivered' if ok else 'failed'}" for n, ok in result.items()]}  # fmt: skip


TOOLS: list[Tool] = [DailyBriefNow(), SetDailyBrief(), NotifyTest()]
