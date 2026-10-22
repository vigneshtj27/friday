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
                   "through the planner when due, with the usual approvals). notify=on_change (recurring jobs only) makes it "
                   "a watch: it stays quiet unless the thing it checks changes - the job must end by calling watch_report.")
    schema = {"type": "object", "properties": {"kind": {"type": "string", "enum": ["reminder", "job"]},
                                               "text": {"type": "string", "maxLength": 1000}, "when": WHEN,
                                               "notify": {"type": "string", "enum": ["always", "on_change"]}},
              "required": ["kind", "text", "when"]}  # fmt: skip
    effect = "write"
    needs_owner_quote = True

    async def run(self, args: dict[str, Any], call: Call) -> Any:
        try:
            s = call.ctx.scheduler.create(args["kind"], args["text"], args["when"], notify=args.get("notify") or "always")
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


class SchedulePause(Tool):
    name = "schedule_pause"
    description = "Pause a reminder/job by id or words: it keeps its spec but stops firing until resumed."
    schema = {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}
    effect = "write"
    needs_owner_quote = True

    async def run(self, args: dict[str, Any], call: Call) -> Any:
        hits = call.ctx.scheduler.pause(args["query"])
        if not hits:
            raise ToolError("no active reminder or job matches that")
        return {"paused": [{"id": s["id"], "text": s["text"]} for s in hits],
                "verified": [f"{len(hits)} schedule(s) paused; they won't fire until resumed"]}  # fmt: skip


class ScheduleResume(Tool):
    name = "schedule_resume"
    description = "Resume a paused reminder/job by id or words. Its next run is recomputed from now (no missed slots fire)."
    schema = {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}
    effect = "write"
    needs_owner_quote = True

    async def run(self, args: dict[str, Any], call: Call) -> Any:
        hits = call.ctx.scheduler.resume(args["query"])
        if not hits:
            raise ToolError("no paused reminder or job matches that")
        return {"resumed": [{"id": s["id"], "next": s["next"]} for s in hits],
                "verified": [f"{len(hits)} schedule(s) resumed, next {hits[0]['next']}"]}  # fmt: skip


class ScheduleRunNow(Tool):
    name = "schedule_run_now"
    description = "Run a scheduled job now, out of band, without changing its next scheduled fire. Use for 'run <id>'."
    schema = {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}
    effect = "write"
    needs_owner_quote = True

    async def run(self, args: dict[str, Any], call: Call) -> Any:
        hits = call.ctx.scheduler.run_now(args["query"])
        if not hits:
            raise ToolError("no scheduled job matches that")
        return {"running": [{"id": s["id"], "text": s["text"]} for s in hits],
                "verified": [f"running {len(hits)} job(s) now; the schedule's next fire is unchanged"]}  # fmt: skip


class WatchReport(Tool):
    name = "watch_report"
    description = ("Report what a WATCH (on_change) scheduled job observed: `value` is a short canonical string (numbers/"
                   "tokens only, no prose) and `summary` is one or two sentences for the owner. Returns changed=true when "
                   "`value` differs from last time. Only usable as the final step of an on_change scheduled job.")
    schema = {"type": "object", "properties": {"value": {"type": "string", "maxLength": 300},
                                               "summary": {"type": "string", "maxLength": 1000}},
              "required": ["value", "summary"]}  # fmt: skip
    effect = "read"

    async def run(self, args: dict[str, Any], call: Call) -> Any:
        sid = call.watch_id
        if not sid:
            raise ToolError("watch_report is only usable inside an on_change scheduled job")
        st = call.ctx.scheduler._state(sid)
        last = st["last_value"] if st else None
        value = " ".join(str(args["value"]).split())
        changed = last is None or call.ctx.redactor.redact(value) != last
        call.ctx.scheduler.record_watch(sid, value, str(args["summary"]), changed)
        return {"changed": changed, "first_run": last is None}


TOOLS = [ScheduleCreate(), ScheduleList(), ScheduleCancel(), SchedulePause(), ScheduleResume(), ScheduleRunNow(), WatchReport()]
