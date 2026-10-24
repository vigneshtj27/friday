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


class FakeNotifier:
    def __init__(self):
        self.sent = []

    async def send(self, text, url=None, *, meta=None):
        self.sent.append(text)


def _deps(ctx, spend_router, *, domains=("localhost", "mock-shop.local"), notifier=None, run_id="r"):
    from friday.agent.policy import ActionPolicy, PolicyContext
    from friday.agent.tools import ExecutorDeps
    from friday.vault.injector import InjectionScope

    return ExecutorDeps(
        policy=ActionPolicy(PolicyContext(tuple(domains), ctx.redactor)),
        gate=ctx.gate, spend_router=spend_router, notifier=notifier, injector=ctx.injector,
        scope=InjectionScope("mybank.me", "secret:mybank/me/", ("localhost",)),
        files=ctx.files, runs=ctx.runs, audit=ctx.audit, redactor=ctx.redactor,
        downloads_dir=ctx.settings.data_dir, run_id=run_id, profile_id="mybank.me",
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


# ---- follow-up 3: landing checks, ambiguity-in-payment-context, breach telemetry -----------------

def _exec(ctx, page, *, router=None, notifier=None, run_id="r"):
    from friday.agent.tools import ToolExecutor

    return ToolExecutor(None, page, _deps(ctx, router if router is not None else _router(ctx),
                                          domains=("localhost", "mock-shop.local", "shop.example"),
                                          notifier=notifier, run_id=run_id))  # fmt: skip


async def test_landing_on_payment_url_unsupported_halts(ctx):
    ex = _exec(ctx, FakePage("https://shop.example/checkout/pay", {}))
    ex._page_has_payment = lambda: _async(False)
    ex._page_confirmation = lambda: _async(False)
    res = await ex._landing_check()
    assert res is not None and res.finish == {"status": "blocked", "summary": "landed on an unsupported payment surface"}
    assert "remote view" in res.content


async def test_landing_on_card_form_unsupported_halts(ctx):
    ex = _exec(ctx, FakePage("https://shop.example/step2", {}))
    ex._page_has_payment = lambda: _async(True)   # a card field is on the page
    ex._page_confirmation = lambda: _async(False)
    res = await ex._landing_check()
    assert res is not None and res.finish["status"] == "blocked"


async def test_landing_on_ordinary_page_continues(ctx):
    ex = _exec(ctx, FakePage("https://shop.example/cart", {}))
    ex._page_has_payment = lambda: _async(False)
    ex._page_confirmation = lambda: _async(False)
    assert await ex._landing_check() is None


async def test_supported_adapter_checkout_landing_not_halted(ctx):
    ex = _exec(ctx, FakePage(URL, {}))  # URL is the mock-shop /checkout, which the adapter claims
    ex._page_has_payment = lambda: _async(True)
    ex._page_confirmation = lambda: _async(False)
    assert await ex._landing_check() is None


async def test_ambiguous_control_in_payment_context_fails_closed_not_tapped(ctx):
    # a generic NEEDS_APPROVAL submit (no pay word, plain form) on a payment-URL page, no adapter
    from friday.agent.snapshot import ElementInfo

    ex = _exec(ctx, FakePage("https://shop.example/checkout/pay", {}), router=_router(ctx))
    ex._page_has_payment = lambda: _async(False)  # context comes from the PAY_URL
    el = ElementInfo("e1", 0, {"name": "Submit", "isSubmit": True,
                               "form": {"fieldTypes": ["text", "submit"], "fieldNames": ["notes"], "text": "A form"}})
    res = await ex._gate("click", {"ref": "e1"}, el)
    assert res is not None and res.is_error and "remote view" in res.content
    assert not ctx.gate.pending()  # NOT a generic approval tap


async def test_ambiguous_control_outside_payment_context_keeps_the_tap(ctx):
    from friday.agent.snapshot import ElementInfo

    ex = _exec(ctx, FakePage("https://shop.example/account", {}), router=_router(ctx))
    ex._page_has_payment = lambda: _async(False)
    el = ElementInfo("e1", 0, {"name": "Submit", "isSubmit": True,
                               "form": {"fieldTypes": ["text", "submit"], "fieldNames": ["notes"], "text": "A form"}})
    task = asyncio.create_task(ex._gate("click", {"ref": "e1"}, el))
    await approve_next(ctx, True)  # a normal approval card appeared (today's behaviour preserved)
    assert await task is None  # approved -> _gate lets the action proceed


async def test_breach_signal_recorded_and_owner_alerted(ctx):
    notifier = FakeNotifier()
    ex = _exec(ctx, FakePage("https://shop.example/thank-you", {}), router=_router(ctx), notifier=notifier, run_id="runX")
    ex._page_has_payment = lambda: _async(False)
    ex._page_confirmation = lambda: _async(True)  # a confirmation page, but no gate ran this run
    res = await ex._landing_check()
    assert res is not None and res.finish["status"] == "blocked"
    rows = [r for r in ctx.spend.ledger.list() if r["run_id"] == "runX"]
    assert rows and rows[0]["outcome"] == "uncertain" and rows[0]["detail"].startswith("BREACH")
    assert notifier.sent and "unauthorised payment" in notifier.sent[0].lower()


async def test_confirmation_with_a_gate_row_is_not_a_breach(ctx):
    # a legitimately gated+committed payment this run -> a confirmation page is expected, not a breach
    ctx.prefs.set("spend_caps", {})
    page = FakePage(URL, {**ORDER, "_confirm_on_commit": "ORD1"})
    task = asyncio.create_task(_router(ctx).run(page, task_id="t", run_id="runY"))
    await approve_next(ctx, True)
    await task  # committed row written for runY
    ex = _exec(ctx, FakePage("https://shop.example/thank-you", {}), notifier=FakeNotifier(), run_id="runY")
    ex._page_has_payment = lambda: _async(False)
    ex._page_confirmation = lambda: _async(True)
    assert await ex._landing_check() is None  # not a breach: a gate row exists for this run


def _async(value):
    async def _f():
        return value
    return _f()
