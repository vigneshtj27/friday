"""Capability: an open-ended task the owner asked for in plain words, on one signed-in account.

The goal comes from the owner (through the chat). The agent loop's usual rules
still apply: read-only unless the owner approves a specific action, allowed
domains only, secrets by reference, no CAPTCHA solving.
"""

from __future__ import annotations

from typing import Any

from friday.agent.loop import AgentLoop, AgentTask
from friday.capabilities.statement import CapabilityResult

MAX_GOAL = 1000


class OwnerTask:
    name = "task"

    def __init__(self, ctx: Any, llm: Any):
        self.ctx = ctx
        self.llm = llm

    @staticmethod
    def validate(params: dict[str, Any]) -> dict[str, Any]:
        goal = " ".join(str(params.get("goal") or "").split())
        if len(goal) < 4:
            raise ValueError("a task needs a goal")
        if len(goal) > MAX_GOAL:
            raise ValueError(f"keep the goal under {MAX_GOAL} characters")
        return {"goal": goal}

    async def run(self, op: Any, ex: Any, run_id: str, params: dict[str, Any]) -> CapabilityResult:
        profile, site = op.profile, op.site
        task = AgentTask(
            goal=(
                f"You are signed in to account '{profile.account}'. The owner asked: \"{params['goal']}\"\n"
                "Do what they asked. Most requests are to look something up: find it, check it, and put the "
                "answer in the finish summary in one or two plain sentences (amounts, dates and names exactly as "
                "the site shows them), with the details in data. If they asked for a document, download it with "
                "download_file. If the request needs an action that changes something (paying, booking, "
                "cancelling, changing a setting), go up to that step; the system will ask the owner to approve it. "
                "If the site cannot do it, say so in the summary."
            ),
            run_id=run_id,
            profile_id=profile.id,
            site_name=site.name,
            allowed_domains=site.all_domains,
            secret_names=[],
            site_notes=self.ctx.sitenotes.render(site.id),
            max_steps=self.ctx.settings.max_steps,
            max_wall_seconds=self.ctx.settings.max_wall_seconds,
            max_cost_usd=self.ctx.settings.max_cost_usd,
        )
        outcome = await AgentLoop(self.llm, ex, self.ctx.runs).run(task)
        files = list(ex.downloads)
        status = {"success": "succeeded", "needs_attention": "needs_attention", "blocked": "needs_attention"}.get(outcome.status, "failed")
        result = {"agent_status": outcome.status, "answer": outcome.summary, "data": outcome.data, "files": files}
        return CapabilityResult(status, outcome.summary, result, outcome.notes, outcome.contradicted_notes)
