"""The task ledger as chat tools (friday/tasks.py).

Reads ("what's open", "what's waiting on me") are free. Owner actions on a task - cancel, resume,
drop, set a deadline - change only friday's own queued work (nothing external), so they need
owner_quote (untrusted web/email text can't cancel or resume a task through the model) but not an
approval card. Resuming re-runs the task's stored request through the planner; a side effect that
was in flight at the crash is re-checked by its own tool, never blindly repeated.
"""

from __future__ import annotations

from typing import Any

from friday.tools.base import Call, Tool, ToolError

DATE = {"type": "string", "description": "YYYY-MM-DDTHH:MM in the owner's timezone, or null to clear"}


class TasksOpen(Tool):
    name = "tasks_open"
    description = "List open tasks (planned/running/waiting on you/interrupted): what friday is carrying and their state."
    schema = {"type": "object", "properties": {}}

    async def run(self, args: dict[str, Any], call: Call) -> Any:
        return {"open": call.ctx.tasks.open_tasks()}


class TasksWaiting(Tool):
    name = "tasks_waiting"
    description = "List tasks waiting on you (an approval/credential) or interrupted by a restart."
    schema = {"type": "object", "properties": {}}

    async def run(self, args: dict[str, Any], call: Call) -> Any:
        return {"waiting": call.ctx.tasks.waiting_tasks()}


class TaskCancel(Tool):
    name = "task_cancel"
    description = "Cancel one open task by its id or words from its request. Stops friday's own queued work; nothing external."
    schema = {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}
    effect = "write"
    needs_owner_quote = True

    async def run(self, args: dict[str, Any], call: Call) -> Any:
        t = call.ctx.tasks.cancel(str(args["query"]))
        if t is None:
            raise ToolError("no single open task matches that (give its id or distinctive words)")
        return {"cancelled": t["id"], "verified": [f"task {t['id']} cancelled"]}


class TaskDrop(Tool):
    name = "task_drop"
    description = "Drop an interrupted task (don't resume it). Marks it cancelled."
    schema = {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}
    effect = "write"
    needs_owner_quote = True

    async def run(self, args: dict[str, Any], call: Call) -> Any:
        t = call.ctx.tasks.drop(str(args["query"]))
        if t is None:
            raise ToolError("no single interrupted task matches that")
        return {"dropped": t["id"], "verified": [f"task {t['id']} dropped"]}


class TaskResume(Tool):
    name = "task_resume"
    description = ("Resume an interrupted task: re-run its stored request. A side effect that was in flight at the crash is "
                   "re-checked by its own tool (e.g. the sheet is read back and rows already there are skipped), never "
                   "blindly repeated. Use for “resume <id>”.")
    schema = {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}
    effect = "write"
    needs_owner_quote = True

    async def run(self, args: dict[str, Any], call: Call) -> Any:
        ctx = call.ctx
        t = ctx.tasks.to_resume(str(args["query"]))
        if t is None:
            raise ToolError("no single interrupted task matches that")
        uncertain = ctx.tasks.uncertain_steps(t["id"])
        ctx.tasks.mark_running_again(t["id"])
        if ctx.chat.assistant is None:
            raise ToolError("can't resume: the model isn't configured")
        note = (" Note: a step was in flight when it stopped; its tool re-checks state before acting." if uncertain else "")
        # Re-run the stored request as a fresh plan. Its tools re-verify state (read-back), so an
        # already-applied write is skipped rather than duplicated. run_nested avoids re-taking the
        # chat lock (we're already inside a reply) and keeps the outer plan's state intact.
        reply = await ctx.chat.assistant.run_nested(t["request"], origin="owner")
        return {"resumed": t["id"], "uncertain_steps": len(uncertain), "reply": reply + note,
                "verified": [f"task {t['id']} resumed"]}  # fmt: skip


class TaskDeadline(Tool):
    name = "task_set_deadline"
    description = ("Set (or clear) a deadline on an open task, so friday re-notifies you about it if it's still waiting then. "
                   "Opt-in per task: without a deadline a waiting task just stays listed.")
    schema = {"type": "object", "properties": {"query": {"type": "string"}, "when": DATE}, "required": ["query"]}
    effect = "write"
    needs_owner_quote = True

    async def run(self, args: dict[str, Any], call: Call) -> Any:
        when = args.get("when")
        iso_when = None
        if when:
            from datetime import datetime
            from zoneinfo import ZoneInfo

            tz = call.ctx.settings.default_timezone or "UTC"
            try:
                dt = datetime.fromisoformat(str(when))
                dt = dt.replace(tzinfo=ZoneInfo(tz)) if dt.tzinfo is None else dt
                iso_when = dt.astimezone(ZoneInfo("UTC")).isoformat()
            except ValueError as e:
                raise ToolError("when must look like 2026-10-23T18:00") from e
        t = call.ctx.tasks.set_deadline(str(args["query"]), iso_when)
        if t is None:
            raise ToolError("no single open task matches that")
        return {"task": t["id"], "deadline_at": iso_when, "verified": [f"deadline {'set' if iso_when else 'cleared'} on {t['id']}"]}


TOOLS: list[Tool] = [TasksOpen(), TasksWaiting(), TaskCancel(), TaskDrop(), TaskResume(), TaskDeadline()]
