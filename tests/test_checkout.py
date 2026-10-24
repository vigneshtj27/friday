"""Phase 4 (Part 2): payments go through a tested checkout adapter + the spend gate, or fail closed.

These drive the SpendRouter with a fake page (same shape as a Playwright page), so the routing, the
gate drive, the void-on-change and the commit/confirm/uncertain outcomes are proven without a browser.
The live loop wiring and the real mock-shop selectors are exercised by a Chrome-marked test.
"""

from __future__ import annotations

import asyncio

from friday.checkout import MockShopAdapter, SpendRouter

from .test_tools import approve_next

URL = "http://localhost:8099/checkout"
ORDER = {
    "#order-total": "500", "#order-currency": "INR", "#merchant": "Mock Shop", "#order-items": "Widget, Gizmo",
    "#delivery-address": "Home", "#payment-method": "Visa 1234", "#order-id": "ORD1",
}  # fmt: skip


class FakePage:
    def __init__(self, url: str, els: dict[str, str]):
        self.url = url
        self.els = dict(els)
        self.clicked: list[str] = []

    async def query_selector(self, sel: str):
        return object() if sel in self.els else None

    async def inner_text(self, sel: str) -> str:
        return self.els.get(sel, "")

    async def click(self, sel: str) -> None:
        self.clicked.append(sel)
        if sel == "#place-order" and self.els.get("_confirm_on_commit"):
            self.els["#order-confirmed"] = self.els["_confirm_on_commit"]  # the shop shows a confirmation


def _router(ctx):
    return SpendRouter(ctx.spend, [MockShopAdapter()])


# ---- adapter reads the intent -------------------------------------------------------------------


async def test_adapter_reads_intent_from_the_page():
    a = MockShopAdapter()
    assert a.supports(URL) and not a.supports("http://localhost:8099/account")
    intent = await a.read_intent(FakePage(URL, ORDER))
    assert intent.amount() == __import__("decimal").Decimal("500")
    assert intent.merchant == "Mock Shop" and intent.items == ("Widget", "Gizmo")


async def test_adapter_returns_none_when_total_missing():
    els = {k: v for k, v in ORDER.items() if k != "#order-total"}
    assert await MockShopAdapter().read_intent(FakePage(URL, els)) is None


# ---- the happy path: approve, commit, confirm ---------------------------------------------------


async def test_approve_commit_and_confirm(ctx):
    ctx.prefs.set("spend_caps", {})
    page = FakePage(URL, {**ORDER, "_confirm_on_commit": "ORD1"})
    task = asyncio.create_task(_router(ctx).run(page, task_id="t", run_id="r"))
    await approve_next(ctx, True)
    ok, msg = await task
    assert ok and "ORD1" in msg
    assert "#place-order" in page.clicked
    assert ctx.spend.ledger.list()[0]["outcome"] == "committed"


async def test_no_confirmation_is_uncertain_not_committed(ctx):
    ctx.prefs.set("spend_caps", {})
    page = FakePage(URL, ORDER)  # no _confirm_on_commit -> no #order-confirmed appears after click
    task = asyncio.create_task(_router(ctx).run(page, task_id="t", run_id="r"))
    await approve_next(ctx, True)
    ok, msg = await task
    assert not ok and "uncertain" in msg
    assert "#place-order" in page.clicked  # we did submit
    assert ctx.spend.ledger.list()[0]["outcome"] == "uncertain"  # but never claimed success


# ---- void on total change -----------------------------------------------------------------------


async def test_a_total_change_before_commit_voids_and_does_not_pay(ctx):
    ctx.prefs.set("spend_caps", {})
    page = FakePage(URL, {**ORDER, "_confirm_on_commit": "ORD1"})
    task = asyncio.create_task(_router(ctx).run(page, task_id="t", run_id="r"))
    # the card is pending; the page's total changes before the owner's tap is acted on
    for _ in range(200):
        if ctx.gate.pending():
            page.els["#order-total"] = "650"
            ctx.gate.decide(ctx.gate.pending()[0].id, True)
            break
        await asyncio.sleep(0.01)
    ok, _ = await task
    assert not ok and "#place-order" not in page.clicked  # never committed the changed order
    assert ctx.spend.ledger.list()[0]["outcome"] == "void"


# ---- caps, denial -------------------------------------------------------------------------------


async def test_over_cap_refuses_without_a_card(ctx):
    ctx.prefs.set("spend_caps", {"per_order": "100", "currency": "INR"})
    page = FakePage(URL, ORDER)
    ok, msg = await _router(ctx).run(page, task_id="t", run_id="r")  # 500 > 100, refused synchronously
    assert not ok and "per-order cap" in msg
    assert not ctx.gate.pending() and "#place-order" not in page.clicked


async def test_denied_tap_does_not_pay(ctx):
    ctx.prefs.set("spend_caps", {})
    page = FakePage(URL, ORDER)
    task = asyncio.create_task(_router(ctx).run(page, task_id="t", run_id="r"))
    await approve_next(ctx, False)
    ok, _ = await task
    assert not ok and "#place-order" not in page.clicked
    assert ctx.spend.ledger.list()[0]["outcome"] == "denied"


# ---- fail closed on an unsupported checkout -----------------------------------------------------


async def test_unsupported_checkout_fails_closed(ctx):
    page = FakePage("https://some-shop.example/checkout", ORDER)
    ok, msg = await _router(ctx).run(page, task_id="t", run_id="r")
    assert not ok and "remote view" in msg
    assert not ctx.gate.pending() and not ctx.spend.ledger.list()  # nothing attempted, nothing recorded


def test_fail_closed_message_mentions_remote_view():
    assert "remote view" in SpendRouter.fail_closed_message("https://x.example/checkout")


# ---- the executor routes a SPEND verdict through the gate (not the generic tap) ------------------


def _deps(ctx, spend_router):
    from friday.agent.policy import ActionPolicy, PolicyContext
    from friday.agent.tools import ExecutorDeps
    from friday.vault.injector import InjectionScope

    return ExecutorDeps(
        policy=ActionPolicy(PolicyContext(("localhost", "mock-shop.local"), ctx.redactor)),
        gate=ctx.gate, spend_router=spend_router, injector=ctx.injector,
        scope=InjectionScope("mybank.me", "secret:mybank/me/", ("localhost",)),
        files=ctx.files, runs=ctx.runs, audit=ctx.audit, redactor=ctx.redactor,
        downloads_dir=ctx.settings.data_dir, run_id="r", profile_id="mybank.me",
    )  # fmt: skip


def _pay_button():
    from friday.agent.snapshot import ElementInfo

    return ElementInfo("e1", 0, {"name": "Pay now", "isSubmit": True,
                                 "form": {"fieldTypes": ["submit"], "fieldNames": [], "text": "Pay"}})  # fmt: skip


async def test_executor_fails_closed_on_a_payment_without_a_router(ctx):
    from friday.agent.tools import ToolExecutor

    ex = ToolExecutor(None, FakePage(URL, ORDER), _deps(ctx, None))
    res = await ex._gate("click", {"ref": "e1"}, _pay_button())
    assert res is not None and res.is_error and "remote view" in res.content
    assert not ctx.gate.pending()  # never a generic approval card for money


async def test_executor_refuses_navigating_onto_a_payment_url_on_an_unsupported_site(ctx):
    # Part 2: a page that self-submits can't be intercepted after the fact, so navigating onto a
    # payment-commit endpoint that no adapter claims is refused BEFORE the goto.
    from friday.agent.policy import ActionPolicy, PolicyContext
    from friday.agent.tools import ExecutorDeps, ToolExecutor
    from friday.vault.injector import InjectionScope

    deps = ExecutorDeps(
        policy=ActionPolicy(PolicyContext(("shop.example", "localhost", "mock-shop.local"), ctx.redactor)),
        gate=ctx.gate, spend_router=_router(ctx), injector=ctx.injector,
        scope=InjectionScope("p", "secret:p/", ("shop.example",)), files=ctx.files, runs=ctx.runs,
        audit=ctx.audit, redactor=ctx.redactor, downloads_dir=ctx.settings.data_dir, run_id="r", profile_id="p",
    )  # fmt: skip
    ex = ToolExecutor(None, FakePage("https://shop.example/account", {}), deps)
    res = await ex._gate("goto", {"url": "https://shop.example/checkout/pay"}, None)
    assert res is not None and res.is_error and "remote view" in res.content


async def test_executor_routes_a_payment_through_the_gate(ctx):
    from friday.agent.tools import ToolExecutor

    ctx.prefs.set("spend_caps", {})
    page = FakePage(URL, {**ORDER, "_confirm_on_commit": "ORD1"})
    ex = ToolExecutor(None, page, _deps(ctx, _router(ctx)))
    task = asyncio.create_task(ex._gate("click", {"ref": "e1"}, _pay_button()))
    await approve_next(ctx, True)
    res = await task
    assert res is not None and not res.is_error and "ORD1" in res.content
    assert "#place-order" in page.clicked
    assert ctx.spend.ledger.list()[0]["outcome"] == "committed"
