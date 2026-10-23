"""Phase 3: generic playbooks - versioning, rollback, narrow-not-widen, and owner-only creation."""

from __future__ import annotations

import asyncio

import pytest

from friday.playbooks import narrow_domains, narrow_effects
from friday.tools.base import Call, Tool, ToolError

from .planner_fakes import PlanLLM, attach, last_friday, plan, settle, step
from .test_tools import approve_next

# ---- store: versioning, diff, rollback ----------------------------------------------------------


def test_save_creates_v1_marked_good(ctx):
    pb = ctx.playbooks.save("Weekly expenses", "keep my sheet current", "Fetch Amex, add new charges to the sheet.",
                            {"allowed_effects": ["read", "write"], "allowed_domains": ["americanexpress.com"]})
    assert pb["version"] == 1 and pb["last_good_version"] == 1
    assert pb["allowed_effects"] == ["read", "write"] and pb["status"] == "active"
    assert ctx.playbooks.get("Weekly expenses")["id"] == pb["id"]


def test_second_save_versions_and_keeps_history(ctx):
    ctx.playbooks.save("P", "g", "body one", {"allowed_effects": ["read"]})
    pb = ctx.playbooks.save("P", "g", "body two", {"allowed_effects": ["read"]})
    assert pb["version"] == 2
    vs = ctx.playbooks.versions(pb["id"])
    assert [v["version"] for v in vs] == [1, 2]
    assert "body one" in ctx.playbooks.diff(pb["id"]) and "body two" in ctx.playbooks.diff(pb["id"])


def test_rollback_restores_last_good(ctx):
    ctx.playbooks.save("P", "g", "good body", {"allowed_effects": ["read"]})  # v1, last_good=1
    pb = ctx.playbooks.save("P", "g", "broken body", {"allowed_effects": ["read"]})  # v2, last_good still 1
    assert pb["version"] == 2 and pb["last_good_version"] == 1
    after = ctx.playbooks.rollback(pb["id"])
    assert after["version"] == 3  # rollback is a new version on top
    assert "good body" in ctx.playbooks.get_by_id(pb["id"])["body"]


def test_run_outcome_marks_good_or_rolls_back(ctx):
    ctx.playbooks.save("P", "g", "v1 body", {"allowed_effects": ["read"]})  # last_good=1
    pb = ctx.playbooks.save("P", "g", "v2 body", {"allowed_effects": ["read"]})  # last_good still 1
    # a failed run of the new, untested version rolls back to v1's body
    rolled = ctx.playbooks.note_run_outcome(pb["id"], ok=False)
    assert "v1 body" in rolled["body"]
    # a successful run marks the current version good
    pb3 = ctx.playbooks.save("P", "g", "v4 body", {"allowed_effects": ["read"]})
    good = ctx.playbooks.note_run_outcome(pb3["id"], ok=True)
    assert good["last_good_version"] == good["version"]


# ---- narrowing helpers --------------------------------------------------------------------------


def test_narrow_domains_never_widens():
    # only domains the account already uses survive; a foreign domain is dropped
    assert narrow_domains(["americanexpress.com", "evil.com"], ["americanexpress.com", "aexp.com"]) == ["americanexpress.com"]
    # an empty header falls back to the site's own domains, never to "anything"
    assert narrow_domains([], ["bank.example"]) == ["bank.example"]


def test_narrow_effects_always_allows_read_and_drops_unknown():
    assert narrow_effects(["write", "send"]) == {"read", "write", "send"}
    assert narrow_effects(["nonsense", "spend"]) == {"read", "spend"}
    assert narrow_effects([]) == {"read"}


# ---- registry enforcement of narrowing ----------------------------------------------------------


class _DummyWrite(Tool):
    name = "dummy_write"
    description = "a private write with no card"
    effect = "write"

    async def run(self, args, call):
        return {"ok": True}


async def test_a_disallowed_effect_is_refused(ctx):
    ctx.tools.register(_DummyWrite())
    read_only = Call(ctx=ctx, owner_text="run the playbook", plan_id="p1", allowed_effects={"read"})
    with pytest.raises(ToolError, match="outside what this playbook is allowed"):
        await ctx.tools.call("dummy_write", {}, read_only)
    # with write allowed, it runs
    allowed = Call(ctx=ctx, owner_text="run the playbook", plan_id="p1", allowed_effects={"read", "write"})
    assert (await ctx.tools.call("dummy_write", {}, allowed))["ok"]


async def test_required_approval_forces_a_card(ctx):
    ctx.tools.register(_DummyWrite())
    call = Call(ctx=ctx, owner_text="run", plan_id="p1", allowed_effects={"read", "write"}, require_approval={"write"})

    async def drive():
        return await ctx.tools.call("dummy_write", {}, call)

    task = asyncio.create_task(drive())
    await approve_next(ctx, True)
    assert (await task)["ok"]  # ran only after the tap


# ---- through the chat: owner-only creation, run narrowing ---------------------------------------


async def test_saving_a_playbook_needs_owner_words_and_a_tap(ctx):
    # A web page's text cannot supply owner_quote; the save is refused before any card.
    inj = PlanLLM([plan(step("playbook_save", {"name": "evil", "goal": "x", "body": "do evil",
                      "owner_quote": "the page said to save a playbook"})),
                   plan(reply="I won't; that came from the page.")])
    attach(ctx, inj)
    await ctx.chat.owner_says("summarise this web page for me")
    await settle(ctx)
    assert ctx.playbooks.list() == []

    # The owner's own words plus a tap on the card do save it.
    good = PlanLLM([plan(step("playbook_save", {"name": "Weekly", "goal": "refresh expenses",
                      "body": "fetch and add", "allowed_effects": ["read", "write"],
                      "owner_quote": "save this as a playbook called Weekly"}))],
                   reports=["Saved the Weekly playbook."])
    attach(ctx, good)

    async def go():
        await ctx.chat.owner_says("save this as a playbook called Weekly")
        await settle(ctx)

    task = asyncio.create_task(go())
    await approve_next(ctx, True)
    await task
    assert [p["name"] for p in ctx.playbooks.list()] == ["Weekly"]


async def test_running_a_playbook_narrows_effects(ctx):
    # a read-only playbook: a write step inside its run is refused by the registry
    ctx.playbooks.save("ReadOnly", "look something up", "Read the sheet and report.", {"allowed_effects": ["read"]})
    ran = PlanLLM([
        plan(step("playbook_run", {"query": "ReadOnly", "owner_quote": "run the ReadOnly playbook"})),
        # inside the playbook run, the model tries a write -> denied, then it must stop
        plan(step("task_cancel", {"query": "anything", "owner_quote": "run the ReadOnly playbook"})),
        plan(reply="That playbook is read-only; I couldn't do the write."),
    ], reports=["Done, read-only."])
    attach(ctx, ran)
    await ctx.chat.owner_says("run the ReadOnly playbook")
    await settle(ctx)
    # the playbook still exists and nothing was cancelled through it
    assert ctx.playbooks.get("ReadOnly") is not None


# ---- proposal after a successful task ------------------------------------------------------------


async def test_a_successful_multistep_task_offers_a_playbook(ctx):
    two = PlanLLM([plan(step("tasks_open"), step("tasks_waiting"))], reports=["Here's your status."])
    attach(ctx, two)
    await ctx.chat.owner_says("what's open and what's waiting?")
    await settle(ctx)
    assert "playbook" in last_friday(ctx).lower()
