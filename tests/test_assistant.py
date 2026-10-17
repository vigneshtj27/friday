from __future__ import annotations

import asyncio
from datetime import date
from decimal import Decimal

import pytest

from friday.agent.llm import LLMResponse, ToolUse
from friday.assistant import ChatAssistant
from friday.capabilities.task import OwnerTask
from friday.capabilities.transactions import Transaction
from friday.chat import looks_like_code, mask_codes
from friday.profiles.models import ProfileConfig


class ScriptedLLM:
    """Plays back a fixed list of responses and records what it was sent."""

    model = "scripted"

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    async def complete(self, system, messages, tools):
        self.calls.append([dict(m) for m in messages])
        step = self.script.pop(0)
        if isinstance(step, str):
            return LLMResponse([{"type": "text", "text": step}], [], step, "end_turn")
        name, args = step
        use = ToolUse(f"t{len(self.calls)}", name, args)
        block = {"type": "tool_use", "id": use.id, "name": name, "input": args}
        return LLMResponse([block], [use], "", "tool_use")


@pytest.fixture
def setup(ctx, monkeypatch):
    ctx.profiles.create("mybank", "me", ProfileConfig())
    submitted = []

    def submit(kind, profile_id, params=None, *, force=False):
        submitted.append((kind, profile_id, params))
        return ctx.runs.create(kind, profile_id, params or {})

    monkeypatch.setattr(ctx.jobs, "submit", submit)

    def attach(script):
        llm = ScriptedLLM(script)
        ctx.chat.assistant = ChatAssistant(ctx, lambda: llm)
        return llm

    return ctx, submitted, attach


async def settle(ctx):
    while ctx.chat._tasks:
        await asyncio.gather(*ctx.chat._tasks)


async def test_free_text_starts_a_run_and_replies(setup):
    ctx, submitted, attach = setup
    await ctx.notifier.start(ctx.broker.handle_reply)
    llm = attach([
        ("start_run", {"account": "mybank", "kind": "fetch_statement", "start": "2026-09-01", "end": "2026-09-30"}),
        "Fetching your My Bank statement for September (2026-09-01 to 2026-09-30).",
    ])
    await ctx.chat.owner_says("get my bank statement for september")
    await settle(ctx)

    assert submitted == [("fetch_statement", "mybank.me", {"start": "2026-09-01", "end": "2026-09-30"})]
    rows = ctx.chat.store.list()
    assert [r["sender"] for r in rows] == ["owner", "friday", "friday"]
    assert rows[1]["kind"] == "run" and rows[1]["profile_id"] == "mybank.me"
    assert "September" in rows[2]["text"]
    first = llm.calls[0]
    assert first[0]["role"] == "user" and first[1]["role"] == "system" and "Today is" in first[1]["content"]
    assert ctx.chat.assistant.busy is False


async def test_codes_never_reach_the_model(setup):
    ctx, _, attach = setup
    await ctx.notifier.start(ctx.broker.handle_reply)
    llm = attach(["unused"])
    await ctx.chat.owner_says("482913")
    await settle(ctx)
    assert llm.calls == []
    assert "discarded" in ctx.chat.store.list()[-1]["text"]


async def test_model_failure_is_reported_politely(setup):
    ctx, _, _ = setup

    def broken():
        raise RuntimeError("no credentials")

    ctx.chat.assistant = ChatAssistant(ctx, broken)
    await ctx.chat.owner_says("check my balance")
    await settle(ctx)
    assert "couldn't reach the model" in ctx.chat.store.list()[-1]["text"]


async def test_tool_errors_go_back_to_the_model(setup):
    ctx, submitted, attach = setup
    llm = attach([("start_run", {"account": "nosuchbank", "kind": "login"}), "I don't know that account."])
    await ctx.chat.owner_says("sign in to nosuchbank")
    await settle(ctx)
    assert submitted == []
    result = llm.calls[1][-1]["content"][0]
    assert result["is_error"] and "no single account" in result["content"]


async def test_transactions_tool_filters_and_totals(setup):
    ctx, _, _ = setup
    t = lambda d, desc, amt, direction: Transaction(date.fromisoformat(d), desc, Decimal(amt), "INR", direction, None, "mybank.me", "f1")  # noqa: E731
    ctx.transactions.add([
        t("2026-09-03", "AMAZON PAY INDIA", "1299.00", "debit"),
        t("2026-09-10", "Amazon refund", "199.00", "credit"),
        t("2026-09-12", "Swiggy", "450.00", "debit"),
    ])
    a = ChatAssistant(ctx, lambda: None)
    out = await a._run_tool("query_transactions", {"contains": "amazon", "start": "2026-09-01", "end": "2026-09-30"})
    assert out["matched"] == 2
    assert out["totals"]["INR"] == {"debit": "1299.00", "credit": "199.00"}


def test_task_goal_validation():
    assert OwnerTask.validate({"goal": "  find   my electricity bill due date "}) == {"goal": "find my electricity bill due date"}
    with pytest.raises(ValueError):
        OwnerTask.validate({"goal": ""})


def test_masking_keeps_dates_and_amounts_readable():
    assert looks_like_code("284 113") and looks_like_code("G-482913")
    assert not looks_like_code("fetch 2026-09-01 to 2026-09-30")
    assert mask_codes("fetch 2026-09-01 to 2026-09-30, card 4417, ₹12,480.00") == "fetch 2026-09-01 to 2026-09-30, card 4417, ₹12,480.00"
    assert mask_codes("the code was 284113 i think") == "the code was •••••• i think"


async def test_run_finish_does_not_duplicate_its_card(setup):
    ctx, _, _ = setup
    run_id = ctx.runs.create("task", "mybank.me", {"goal": "check balance"})
    meta = {"kind": "run", "ref": run_id, "profile_id": "mybank.me"}
    await ctx.chat.send("Working on it.", None, meta=meta)
    await ctx.chat.send("mybank.me: Balance is ₹40,210.55.", None, meta=meta)
    rows = ctx.chat.store.list()
    assert [r["kind"] for r in rows] == ["run", None]
