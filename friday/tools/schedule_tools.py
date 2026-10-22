"""Reminders and recurring jobs as tools (friday/scheduler.py).

Creating, pausing, resuming, cancelling or running a schedule all change what friday will do on its
own later, so every one of them needs the owner's tap on an approval card, decided in code
(friday.agent.approvals) - the model can never resolve it. owner_quote is still required, but it is
not the security boundary: a page-influenced model can copy the owner's whole request, so the quote
alone is not proof of authority. The card shows the exact operation, the schedule and its timing.
"""

from __future__ import annotations

from typing import Any

from friday.scheduler import ScheduleError, describe, normalise
from friday.tools.base import Call, Tool, ToolError


def _match_reason(ctx: Any, op: str, query: str) -> str:
    """A short, read-only description of which schedule(s) a mutation would hit, for the card."""
    sched = ctx.scheduler
    if op == "pause":
        base = [s for s in sched.list() if not s["paused"]]
    elif op == "resume":
        base = [s for s in sched.list() if s["paused"]]
    elif op == "run":
        base = [s for s in sched.list(include_done=True) if not s["paused"] or True]
        base = [s for s in base if s["kind"] == "job"]
    else:  # cancel
        base = sched.list()
    hits = sched._match(query, base)
    return ", ".join(f"{h['id']} “{h['text'][:50]}”" for h in hits) or f"“{query}” (no match yet)"

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

    def approval(self, args: dict[str, Any], call: Call) -> str | None:
        notify = args.get("notify") or "always"
        try:
            when = describe(normalise(args["when"]))
        except Exception:
            when = str(args.get("when"))
        text = " ".join(str(args.get("text", "")).split())[:80]
        watch = " as a watch (quiet until it changes)" if notify == "on_change" else ""
        return f"Create a {args['kind']}: “{text}”, {when}{watch}."

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

    def approval(self, args: dict[str, Any], call: Call) -> str | None:
        return f"Cancel schedule(s): {_match_reason(call.ctx, 'cancel', str(args['query']))}."

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

    def approval(self, args: dict[str, Any], call: Call) -> str | None:
        return f"Pause schedule(s): {_match_reason(call.ctx, 'pause', str(args['query']))}."

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

    def approval(self, args: dict[str, Any], call: Call) -> str | None:
        return f"Resume schedule(s): {_match_reason(call.ctx, 'resume', str(args['query']))}."

    async def run(self, args: dict[str, Any], call: Call) -> Any:
        hits = call.ctx.scheduler.resume(args["query"])
        if not hits:
            raise ToolError("no paused reminder or job matches that")
        resumed = [s for s in hits if not s.get("expired")]
        expired = [s for s in hits if s.get("expired")]
        verified = [f"{len(resumed)} schedule(s) resumed" + (f", next {resumed[0]['next']}" if resumed else "")]
        if expired:
            verified.append(f"{len(expired)} one-time schedule(s) had already passed and were NOT fired; "
                            "set a new time if you still want them")  # fmt: skip
        return {"resumed": [{"id": s["id"], "next": s["next"]} for s in resumed],
                "expired": [{"id": s["id"], "text": s["text"]} for s in expired], "verified": verified}  # fmt: skip


class ScheduleRunNow(Tool):
    name = "schedule_run_now"
    description = "Run a scheduled job now, out of band, without changing its next scheduled fire. Use for 'run <id>'."
    schema = {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}
    effect = "write"
    needs_owner_quote = True

    def approval(self, args: dict[str, Any], call: Call) -> str | None:
        return f"Run now (one-off, next fire unchanged): {_match_reason(call.ctx, 'run', str(args['query']))}."

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
