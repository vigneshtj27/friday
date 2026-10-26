"""Phase 4: the generic spend gate - caps in code, an approval bound to the exact order, a ledger,
and no automatic retry of a failed or uncertain payment."""

from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from friday.spend import SpendIntent
from friday.tools.base import ALWAYS_APPROVE, Call, Tool, ToolError

from .test_tools import approve_next


def _intent(total="500", merchant="Shop", items=("Widget",), order="ord1", method="Visa ending 1234", addr="Home"):
    return SpendIntent(merchant=merchant, total=total, currency="INR", items=tuple(items),
                       address_label=addr, method_label=method, order_id=order)  # fmt: skip


async def _approve(ctx):
    await approve_next(ctx, True)


async def _decide_until(ctx, fut, *, approve: bool = True, timeout: float = 10.0):
    """Keep approving/denying every card that appears until `fut` completes, then return its result.

    Replaces the fragile pattern of a FIXED-LENGTH poll loop followed by a bare `await fut`: if the
    loop ever ends before the card it was meant to decide is decided, `await fut` then parks on the
    gate's 600s TTL and the test hangs until the global --timeout kills it (the reported
    'test_spend.py hangs 60s+' shape). Whatever makes a card go undecided - a scheduling/loop
    difference across environments, or a real regression where no card is ever answered - deciding
    concurrently with a hard wait_for turns the hang into a fast, clear failure instead."""
    async def decider():
        while True:
            for a in ctx.gate.pending():
                ctx.gate.decide(a.id, approve)
            await asyncio.sleep(0.005)

    d = asyncio.create_task(decider())
    try:
        return await asyncio.wait_for(asyncio.shield(fut), timeout)
    except TimeoutError as e:  # pragma: no cover - only on a real regression
        raise AssertionError(f"spend authorise did not settle within {timeout}s - a card was never "
                             "decided (deadlock or a missing decider), not a slow machine") from e
    finally:
        d.cancel()


# ---- effect classes -----------------------------------------------------------------------------


def test_spend_and_account_change_always_need_a_tap():
    assert "spend" in ALWAYS_APPROVE and "account_change" in ALWAYS_APPROVE


class _Spender(Tool):
    name = "dummy_spend"
    description = "a spend"
    effect = "spend"

    async def run(self, args, call):
        return {"ok": True}


async def test_a_spend_effect_tool_is_always_carded(ctx):
    ctx.tools.register(_Spender())
    call = Call(ctx=ctx, owner_text="buy it", plan_id="p1")
    task = asyncio.create_task(ctx.tools.call("dummy_spend", {}, call))
    await approve_next(ctx, False)  # deny
    with pytest.raises(ToolError, match="did not approve"):
        await task


# ---- caps ----------------------------------------------------------------------------------------


async def test_over_per_order_cap_is_refused_not_carded(ctx):
    ctx.prefs.set("spend_caps", {"per_order": "200", "daily": "1000", "currency": "INR"})
    decision = await ctx.spend.authorise(_intent(total="500"), task_id="t1")
    assert not decision.approved and "per-order cap" in decision.reason
    assert not ctx.gate.pending()  # never asked the owner
    assert ctx.spend.ledger.list()[0]["outcome"] == "refused"


async def test_over_daily_cap_is_refused(ctx):
    ctx.prefs.set("spend_caps", {"per_order": "1000", "daily": "600", "currency": "INR"})
    # commit 400 today, then a 300 order would breach the 600 daily cap
    d1 = await _authorise_and_commit(ctx, _intent(total="400", order="a"))
    assert d1
    decision = await ctx.spend.authorise(_intent(total="300", order="b"), task_id="t")
    assert not decision.approved and "daily cap" in decision.reason


async def test_within_caps_asks_and_commits(ctx):
    ctx.prefs.set("spend_caps", {"per_order": "1000", "daily": "1000", "currency": "INR"})
    committed = await _authorise_and_commit(ctx, _intent(total="500"))
    assert committed
    assert ctx.spend.ledger.spent_today("INR") == Decimal(500)
    assert ctx.spend.ledger.list()[0]["outcome"] == "committed"


async def _authorise_and_commit(ctx, intent) -> bool:
    task = asyncio.create_task(ctx.spend.authorise(intent, task_id="t"))
    await _approve(ctx)
    decision = await task
    if decision.approved:
        ctx.spend.committed(decision.ledger_id, order_id=intent.order_id)
    return decision.approved


# ---- binding: a changed order voids the approval ------------------------------------------------


async def test_a_changed_total_voids_the_approval(ctx):
    bound = _intent(total="500")
    changed = _intent(total="650")  # the page now shows a higher total

    async def verify():
        return changed

    task = asyncio.create_task(ctx.spend.authorise(bound, task_id="t", verify=verify))
    await _approve(ctx)
    decision = await task
    assert not decision.approved and "total" in decision.changed
    assert ctx.spend.ledger.list()[0]["outcome"] == "void"


async def test_an_unchanged_order_goes_through(ctx):
    bound = _intent(total="500")

    async def verify():
        return _intent(total="500")  # identical

    task = asyncio.create_task(ctx.spend.authorise(bound, task_id="t", verify=verify))
    await _approve(ctx)
    decision = await task
    assert decision.approved and decision.ledger_id


def test_material_change_detects_each_field():
    base = _intent()
    assert base.material_change(_intent()) == []
    assert "merchant" in base.material_change(_intent(merchant="Other"))
    assert "items" in base.material_change(_intent(items=("Gadget",)))
    assert "address" in base.material_change(_intent(addr="Office"))
    assert "method" in base.material_change(_intent(method="Mastercard"))


# ---- no automatic retry -------------------------------------------------------------------------


async def test_a_failed_payment_is_not_retried_automatically(ctx):
    ctx.prefs.set("spend_caps", {})  # no caps
    task = asyncio.create_task(ctx.spend.authorise(_intent(order="ord9"), task_id="t"))
    await _approve(ctx)
    d = await task
    ctx.spend.failed(d.ledger_id, "gateway timeout")
    # a second attempt at the same order is refused without even asking
    again = await ctx.spend.authorise(_intent(order="ord9"), task_id="t")
    assert not again.approved and "won't retry" in again.reason
    assert not ctx.gate.pending()


async def test_denied_payment_is_recorded(ctx):
    ctx.prefs.set("spend_caps", {})
    task = asyncio.create_task(ctx.spend.authorise(_intent(), task_id="t"))
    await approve_next(ctx, False)
    d = await task
    assert not d.approved
    assert ctx.spend.ledger.list()[0]["outcome"] == "denied"


# ---- reservation accounting: authorised intents consume capacity --------------------------------


async def test_two_authorised_intents_cannot_both_pass_the_daily_cap(ctx):
    # the probe from the audit: two 900 spends under a 1000 daily cap. The first reserves 900; the
    # second must be refused even though the first is only authorised (tapped), not yet committed.
    ctx.prefs.set("spend_caps", {"daily": "1000", "currency": "INR"})
    t1 = asyncio.create_task(ctx.spend.authorise(_intent(total="900", order="a"), task_id="t"))
    await _approve(ctx)
    d1 = await t1
    assert d1.approved
    d2 = await ctx.spend.authorise(_intent(total="900", order="b"), task_id="t")  # no room: 900 reserved
    assert not d2.approved and "daily cap" in d2.reason


async def test_concurrent_authorise_only_one_passes(ctx):
    ctx.prefs.set("spend_caps", {"daily": "1000", "currency": "INR"})

    async def attempt(order):
        return await ctx.spend.authorise(_intent(total="900", order=order), task_id="t")

    both = asyncio.gather(attempt("a"), attempt("b"))
    # approve any card that appears (only one request should ever reach a card), deciding concurrently
    # with a hard timeout so a missed decision fails fast instead of parking on the gate's TTL
    d1, d2 = await _decide_until(ctx, both, approve=True)
    assert sorted([d1.approved, d2.approved]) == [False, True]  # exactly one passed
    refused = d1 if not d1.approved else d2
    assert "daily cap" in refused.reason


# ---- the ledger links its approval --------------------------------------------------------------


async def test_an_approved_payment_names_its_approval(ctx):
    ctx.prefs.set("spend_caps", {})
    task = asyncio.create_task(ctx.spend.authorise(_intent(order="ordA"), task_id="t"))
    d = await _decide_until(ctx, task, approve=True)
    assert d.approved
    pid = ctx.gate.history()[0]["id"]  # the approval just decided
    row = [r for r in ctx.spend.ledger.list() if r["id"] == d.ledger_id][0]
    assert row["approval_id"] == pid and pid is not None


# ---- reconciliation frees capacity; time does not -----------------------------------------------


async def test_a_failed_payment_keeps_consuming_until_reconciled(ctx):
    ctx.prefs.set("spend_caps", {"daily": "1000", "currency": "INR"})
    task = asyncio.create_task(ctx.spend.authorise(_intent(total="800", order="a"), task_id="t"))
    await _approve(ctx)
    d = await task
    ctx.spend.failed(d.ledger_id, "gateway error")
    # a failed payment still holds its 800 of daily capacity (we don't know it didn't charge)
    assert ctx.spend.ledger.spent_today("INR") == Decimal(800)
    # reconciling it as "did not go through" frees the capacity, with evidence
    ctx.spend.reconcile(d.ledger_id, went_through=False, evidence="order page shows no charge")
    assert ctx.spend.ledger.spent_today("INR") == Decimal(0)


async def test_reconcile_as_charged_keeps_consuming(ctx):
    ctx.prefs.set("spend_caps", {})
    task = asyncio.create_task(ctx.spend.authorise(_intent(total="300", order="a"), task_id="t"))
    await _approve(ctx)
    d = await task
    ctx.spend.uncertain(d.ledger_id, "no confirmation seen")
    ctx.spend.reconcile(d.ledger_id, went_through=True, evidence="confirmation email #123", order_id="ORD123")
    assert ctx.spend.ledger.spent_today("INR") == Decimal(300)  # committed still consumes
    assert [r for r in ctx.spend.ledger.list() if r["id"] == d.ledger_id][0]["outcome"] == "committed"


def test_reconcile_needs_evidence(ctx):
    with pytest.raises(ValueError, match="evidence"):
        ctx.spend.ledger.reconcile("nonexistent", False, evidence="")


# ---- card numbers never in an intent ------------------------------------------------------------


def test_intent_carries_only_a_method_label_not_a_card():
    intent = _intent(method="Visa ending 1234")
    card = intent.card()
    assert "method_label" in card and "Visa ending 1234" == card["method_label"]
    assert not any("number" in k or "cvv" in k.lower() for k in card)


# ---- caps setter is owner-only and carded -------------------------------------------------------


async def test_set_spend_caps_tool_refuses_web_text(ctx):
    call = Call(ctx=ctx, owner_text="summarise this web page", plan_id="p1")
    with pytest.raises(ToolError, match="owner's own words"):
        await ctx.tools.call("set_spend_caps", {"per_order": 999999, "owner_quote": "the page said raise my limit"}, call)
