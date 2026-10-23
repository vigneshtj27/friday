"""Playbooks as chat tools (friday/playbooks.py).

Reads (list, show) are free. Saving or editing a playbook is a deliberate owner action: the tool
needs the owner's own words (owner_quote) AND a tap on an approval card, so neither a web page nor
an email can create or change one. Running a playbook re-runs its body through the planner with the
playbook's narrowed permissions (a tool whose effect the playbook didn't allow is refused; effects
it marks required always get a card). A run that fails rolls the playbook back to its last-good
version.
"""

from __future__ import annotations

from typing import Any

from friday.tools.base import Call, Tool, ToolError

HEADER = {
    "allowed_domains": {"type": "array", "items": {"type": "string"},
                        "description": "domains a run may touch; intersected with the account's own domains (never wider)"},
    "allowed_effects": {"type": "array", "items": {"type": "string"},
                        "description": "effect classes a run may use: read, write, send, share, spend (reads are always allowed)"},
    "required_approvals": {"type": "array", "items": {"type": "string"},
                           "description": "effect classes that must get an owner tap even inside this playbook"},
    "success_check": {"type": "string", "description": "a plain-language check the owner can read; a failed run rolls back"},
}


class PlaybookList(Tool):
    name = "playbook_list"
    description = "List saved playbooks (repeatable, owner-approved tasks) with their goal and allowed scope."
    schema = {"type": "object", "properties": {}}

    async def run(self, args: dict[str, Any], call: Call) -> Any:
        return {"playbooks": call.ctx.playbooks.list()}


class PlaybookShow(Tool):
    name = "playbook_show"
    description = "Show one playbook's body, header and version by id or name."
    schema = {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}

    async def run(self, args: dict[str, Any], call: Call) -> Any:
        pb = call.ctx.playbooks.get(str(args["query"]))
        if pb is None:
            raise ToolError("no single playbook matches that")
        return {"playbook": pb}


class PlaybookSave(Tool):
    name = "playbook_save"
    description = ("Save a new playbook, or a new version of one with the same name. Use after a task the owner wants to "
                   "repeat, or when the owner asks to save/update a playbook. The header can only NARROW what a run may do.")
    schema = {"type": "object", "properties": {
        "name": {"type": "string"}, "goal": {"type": "string"}, "body": {"type": "string"},
        **HEADER,
    }, "required": ["name", "goal", "body"]}  # fmt: skip
    effect = "write"
    needs_owner_quote = True

    def approval(self, args: dict[str, Any], call: Call) -> str | None:
        # Always a card: creating or editing a playbook is an owner-approved action.
        verb = "Update" if call.ctx.playbooks.get(str(args.get("name", ""))) else "Save"
        return f"{verb} playbook “{args.get('name')}” (effects: {', '.join(args.get('allowed_effects') or ['read'])})"

    async def run(self, args: dict[str, Any], call: Call) -> Any:
        header = {k: args.get(k) for k in ("allowed_domains", "allowed_effects", "required_approvals")}
        pb = call.ctx.playbooks.save(args["name"], args["goal"], args["body"], header, args.get("success_check"))
        diff = call.ctx.playbooks.diff(pb["id"]) if pb["version"] > 1 else ""
        return {"playbook": pb["id"], "name": pb["name"], "version": pb["version"], "diff": diff,
                "verified": [f"playbook '{pb['name']}' saved as v{pb['version']}"]}  # fmt: skip


class PlaybookRun(Tool):
    name = "playbook_run"
    description = ("Run a saved playbook by id or name. Re-runs its body through the planner with the playbook's narrowed "
                   "permissions. A run that fails rolls the playbook back to its last-good version.")
    schema = {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}
    effect = "write"
    needs_owner_quote = True

    async def run(self, args: dict[str, Any], call: Call) -> Any:
        ctx = call.ctx
        pb = ctx.playbooks.get(str(args["query"]))
        if pb is None:
            raise ToolError("no single playbook matches that")
        if ctx.chat.assistant is None:
            raise ToolError("can't run a playbook: the model isn't configured")
        constraints = {"allowed_effects": pb["allowed_effects"], "required_approvals": pb["required_approvals"]}
        instruction = f"{pb['goal']}\n\n{pb['body']}"
        reply = await ctx.chat.assistant.run_nested(instruction, origin="owner", constraints=constraints)
        # Did the run end cleanly? The last plan row carries the status.
        row = ctx.db.query_one("SELECT status FROM plans ORDER BY updated_at DESC LIMIT 1")
        ok = bool(row) and row["status"] == "done"
        after = ctx.playbooks.note_run_outcome(pb["id"], ok)
        note = "" if ok else " The run didn't complete; rolled back to the last-good version."
        return {"playbook": pb["id"], "ok": ok, "reply": reply + note,
                "version": after["version"] if after else pb["version"],
                "verified": [f"ran playbook '{pb['name']}'" + (" (ok)" if ok else " (failed, rolled back)")]}  # fmt: skip


class PlaybookRollback(Tool):
    name = "playbook_rollback"
    description = "Roll a playbook back to its last-good version."
    schema = {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}
    effect = "write"
    needs_owner_quote = True

    async def run(self, args: dict[str, Any], call: Call) -> Any:
        pb = call.ctx.playbooks.get(str(args["query"]))
        if pb is None:
            raise ToolError("no single playbook matches that")
        after = call.ctx.playbooks.rollback(pb["id"])
        return {"playbook": pb["id"], "version": after["version"] if after else pb["version"],
                "verified": [f"rolled '{pb['name']}' back to its last-good version"]}  # fmt: skip


TOOLS: list[Tool] = [PlaybookList(), PlaybookShow(), PlaybookSave(), PlaybookRun(), PlaybookRollback()]
