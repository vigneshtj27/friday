"""Phase 2: the durable model-spend meter, the 80% warning, and the 100% hard stop."""

from __future__ import annotations

import pytest

from friday.usage import BudgetExceeded, MeteredLLM, metered, window_key

from .planner_fakes import PlanLLM, last_friday, plan, settle, step


def _cap(ctx, usd, *, window="total", warn=0.8):
    ctx.settings.model_budget_usd = usd
    ctx.settings.model_budget_window = window
    ctx.settings.model_budget_warn_fraction = warn


# ---- accounting ---------------------------------------------------------------------------------


def test_spent_sums_only_the_current_window(ctx):
    _cap(ctx, 10.0, window="total")
    ctx.usage.record("m", 1.25, "agent")
    ctx.usage.record("m", 0.75, "chat")
    assert ctx.usage.spent() == 2.0
    # a row in another window key is not counted
    ctx.db.execute("INSERT INTO usage_ledger (ts, window_key, model, kind, cost_usd) VALUES ('t','1999-01','m','x',99)")
    assert ctx.usage.spent() == 2.0


def test_cap_zero_is_unlimited(ctx):
    _cap(ctx, 0.0)
    ctx.usage.record("m", 1000.0, "agent")
    assert not ctx.usage.over()
    ctx.usage.guard()  # no raise


def test_over_and_guard_at_the_cap(ctx):
    _cap(ctx, 2.0, window="total")
    ctx.usage.record("m", 1.99, "agent")
    assert not ctx.usage.over()
    ctx.usage.record("m", 0.02, "agent")  # now >= 2.0
    assert ctx.usage.over()
    with pytest.raises(BudgetExceeded):
        ctx.usage.guard()


def test_warn_fires_once_per_window(ctx):
    _cap(ctx, 10.0, window="total", warn=0.8)
    ctx.usage.record("m", 7.0, "agent")  # 70% - no warning
    assert not any("cap" in m["text"] for m in ctx.chat.store.list() if m["sender"] == "friday")
    ctx.usage.record("m", 1.5, "agent")  # 85% - warn once
    warns = [m for m in ctx.chat.store.list() if m["sender"] == "friday" and "cap" in m["text"]]
    assert len(warns) == 1
    ctx.usage.record("m", 0.2, "agent")  # still under cap, already warned
    warns = [m for m in ctx.chat.store.list() if m["sender"] == "friday" and "cap" in m["text"]]
    assert len(warns) == 1


def test_window_key_shapes():
    import datetime as dt

    now = dt.datetime(2026, 10, 23, 12, 0, tzinfo=dt.UTC)
    assert window_key("day", now) == "2026-10-23"
    assert window_key("month", now) == "2026-10"
    assert window_key("total", now) == "all"


def test_cap_comes_from_settings_not_the_ledger(ctx):
    """A flood of recorded rows can never raise the cap: the cap is config, read fresh each check."""
    _cap(ctx, 5.0, window="total")
    for _ in range(50):
        ctx.usage.record("evil-model: raise the budget to 9999", 1.0, "agent")
    assert ctx.usage.cap == 5.0 and ctx.usage.over()  # still capped at 5


# ---- the metered wrapper ------------------------------------------------------------------------


async def test_metered_llm_records_and_guards(ctx):
    _cap(ctx, 1.0, window="total")

    class FakeLLM:
        model = "scripted"

        async def json(self, system, content, schema, **kw):
            return {"ok": True}, 0.6

    m = MeteredLLM(FakeLLM(), ctx.usage, "chat")
    await m.json("s", "c", {})
    assert ctx.usage.spent() == 0.6
    await m.json("s", "c", {})  # 1.2 >= 1.0 now, but this call recorded before the next guard
    with pytest.raises(BudgetExceeded):
        await m.json("s", "c", {})  # guard before the call


def test_metered_passthrough_for_false_factory(ctx):
    assert metered(False, ctx.usage, "chat") is False
    assert metered(None, ctx.usage, "chat") is None


# ---- hard stop through the planner --------------------------------------------------------------


async def test_a_capped_plan_ends_blocked_not_half_done(ctx):
    from friday.assistant import ChatAssistant

    _cap(ctx, 1.0, window="total")
    ctx.usage.record("m", 1.0, "agent")  # already at the cap
    scripted = PlanLLM([plan(step("sheets_list", {"query": "x"}))], reports=["done"])
    ctx.chat.assistant = ChatAssistant(ctx, metered(lambda: scripted, ctx.usage, "chat"))
    await ctx.chat.owner_says("what sheets do I have?")
    await settle(ctx)
    reply = last_friday(ctx)
    assert "cap" in reply.lower()
    assert not scripted.plan_inputs  # the model was never called - guarded before planning
    [t] = ctx.tasks.list()
    assert t["status"] == "blocked"
