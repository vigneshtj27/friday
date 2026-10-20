"""A scripted model for the planner: hand it plans, fills and reports in order."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from friday.planner import FILL_SCHEMA, PLAN_SCHEMA, REPORT_SCHEMA


def step(tool: str, args: dict[str, Any] | None = None, *, checks: list[dict[str, Any]] | None = None,
         fill: str | None = None, progress: str | None = None, id: str | None = None) -> dict[str, Any]:  # fmt: skip
    return {"id": id or "", "tool": tool, "args_json": json.dumps(args or {}), "fill": fill,
            "progress": progress or f"Running {tool}", "checks": checks or []}  # fmt: skip


def chk(path: str, op: str, value: Any = None, says: str = "") -> dict[str, Any]:
    return {"path": path, "op": op, "value_json": None if value is None else json.dumps(value), "says": says or f"{path} {op}"}


def plan(*steps: dict[str, Any], reply: str | None = None, goal: str = "the task") -> dict[str, Any]:
    out = []
    for i, s in enumerate(steps, 1):
        out.append({**s, "id": s["id"] or f"s{i}"})
    return {"reply": reply, "goal": goal, "steps": out}


class PlanLLM:
    model = "scripted"

    def __init__(self, plans: list[dict[str, Any]], fills: list[dict[str, Any]] | None = None, reports: list[str] | None = None):
        self.plans, self.fills, self.reports = list(plans), list(fills or []), list(reports or [])
        self.plan_inputs: list[str] = []
        self.fill_inputs: list[str] = []
        self.report_inputs: list[str] = []

    async def json(self, system: str, content: Any, schema: dict[str, Any], **kw: Any) -> tuple[dict[str, Any], float]:
        if schema is PLAN_SCHEMA:
            self.plan_inputs.append(content)
            if not self.plans:
                raise AssertionError("the planner asked for more plans than the test scripted")
            return self.plans.pop(0), 0.0
        if schema is FILL_SCHEMA:
            self.fill_inputs.append(content)
            return self.fills.pop(0), 0.0
        if schema is REPORT_SCHEMA:
            self.report_inputs.append(content)
            return {"message": self.reports.pop(0) if self.reports else "Done."}, 0.0
        raise AssertionError(f"unexpected schema {schema}")


def attach(ctx: Any, llm: PlanLLM) -> PlanLLM:
    from friday.assistant import ChatAssistant

    ctx.chat.assistant = ChatAssistant(ctx, lambda: llm)
    return llm


async def settle(ctx: Any) -> None:
    while ctx.chat._tasks:
        await asyncio.gather(*ctx.chat._tasks)


def last_friday(ctx: Any) -> str:
    return [m for m in ctx.chat.store.list() if m["sender"] == "friday"][-1]["text"]
