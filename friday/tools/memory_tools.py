"""Long-term memory as tools. Remembering and forgetting act only on the owner's own words."""

from __future__ import annotations

from typing import Any

from friday.tools.base import Call, Tool, ToolError


class MemoryRemember(Tool):
    name = "memory_remember"
    description = ("Remember a durable fact or decision the owner stated (e.g. 'my expense sheet is Expenses 2026'). "
                   "subject: a short key so a newer fact replaces an older one (e.g. 'expense sheet').")
    schema = {"type": "object", "properties": {"text": {"type": "string", "maxLength": 1000},
                                               "kind": {"type": "string", "enum": ["fact", "decision"]},
                                               "subject": {"type": "string"}}, "required": ["text"]}  # fmt: skip
    effect = "write"
    needs_owner_quote = True

    async def run(self, args: dict[str, Any], call: Call) -> Any:
        m = call.ctx.memory.remember(args["text"], kind=args.get("kind") or "fact", subject=args.get("subject"), source="owner")
        return {"remembered": m["text"], "id": m["id"]}


class MemorySearch(Tool):
    name = "memory_search"
    description = "What friday remembers about something (facts, decisions, outcomes of past tasks)."
    schema = {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}

    async def run(self, args: dict[str, Any], call: Call) -> Any:
        return {"memories": [{"id": m["id"], "kind": m["kind"], "text": m["text"], "when": m["created_at"][:10]}
                             for m in call.ctx.memory.search(args["query"])]}  # fmt: skip


class MemoryForget(Tool):
    name = "memory_forget"
    description = "Forget memories matching words (or an id) the owner asked to forget."
    schema = {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}
    effect = "write"
    needs_owner_quote = True

    async def run(self, args: dict[str, Any], call: Call) -> Any:
        gone = call.ctx.memory.forget(args["query"])
        if not gone:
            raise ToolError("nothing in memory matches that")
        if any("expense sheet" in (m["subject"] or "") for m in gone):
            call.ctx.prefs.set("expense_sheet", None)
        return {"forgotten": [m["text"] for m in gone]}


TOOLS = [MemoryRemember(), MemorySearch(), MemoryForget()]
