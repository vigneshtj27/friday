"""Reminders and recurring jobs as tools (friday/scheduler.py)."""

from __future__ import annotations

from typing import Any

from friday.scheduler import ScheduleError
from friday.tools.base import Call, Tool, ToolError

WHEN = {
    "type": "object",
    "description": "one of: {in_minutes: n} | {at: 'YYYY-MM-DDTHH:MM' in the owner's timezone} | "
                   "{every: minutes|hours|days|weeks, n, time: 'HH:MM', weekdays: [0..6, Monday=0]}",
    "properties": {"in_minutes": {"type": "number"}, "at": {"type": "string"},
                   "every": {"type": "string", "enum": ["minutes", "hours", "days", "weeks"]}, "n": {"type": "integer"},
                   "time": {"type": "string"}, "weekdays": {"type": "array", "items": {"type": "integer"}}},
}  # fmt: skip


class ScheduleCreate(Tool):
    name = "schedule_create"
    description = ("Set a reminder (posts text in the chat when due) or a recurring/one-off job (runs `text` as a request "
                   "through the planner when due, with the usual approvals).")
    schema = {"type": "object", "properties": {"kind": {"type": "string", "enum": ["reminder", "job"]},
                                               "text": {"type": "string", "maxLength": 1000}, "when": WHEN},
              "required": ["kind", "text", "when"]}  # fmt: skip
    effect = "write"
    needs_owner_quote = True

    async def run(self, args: dict[str, Any], call: Call) -> Any:
        try:
            s = call.ctx.scheduler.create(args["kind"], args["text"], args["when"])
        except ScheduleError as e:
            raise ToolError(str(e)) from e
        return {**s, "verified": [f"{s['kind']} {s['id']} saved, next {s['next']}"]}


class ScheduleList(Tool):
    name = "schedule_list"
    description = "Reminders and scheduled jobs that are still active, with when they next run."
    schema = {"type": "object", "properties": {}}

    async def run(self, args: dict[str, Any], call: Call) -> Any:
        return {"schedules": call.ctx.scheduler.list()}


class ScheduleCancel(Tool):
    name = "schedule_cancel"
    description = "Cancel reminders/jobs by id, or by words in their text."
    schema = {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}
    effect = "write"
    needs_owner_quote = True

    async def run(self, args: dict[str, Any], call: Call) -> Any:
        gone = call.ctx.scheduler.cancel(args["query"])
        if not gone:
            raise ToolError("no active reminder or job matches that")
        return {"cancelled": [{"id": s["id"], "text": s["text"]} for s in gone],
                "verified": [f"{len(gone)} schedule(s) no longer active"]}  # fmt: skip


TOOLS = [ScheduleCreate(), ScheduleList(), ScheduleCancel()]
