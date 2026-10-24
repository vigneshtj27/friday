"""Real-DOM checkout: drive the executor + MockShopAdapter through the spend gate against a served
mock-shop checkout page in real Chromium. Chrome-marked - run on the VM:

    uv run pytest -m chrome tests/test_checkout_chrome.py

These are NOT run in the default suite here; they verify the adapter's selectors against real HTML
and the gate/landing/breach paths end to end in a browser.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from friday.config import Settings
from friday.context import build_context
from friday.profiles.models import Site
from friday.vault.masking import Redactor

from .conftest import TEST_KEY
from .fakes import HeuristicLLM, MockBankServer

pytestmark = [pytest.mark.chrome, pytest.mark.timeout(240)]


@pytest.fixture(scope="module")
def shop():
    with MockBankServer() as server:  # the same app also serves /shop/* checkout routes
        yield server


@pytest.fixture(autouse=True)
def _reset_shop(shop):
    # the server is module-scoped and reused, so shop_orders / the live total carry across tests;
    # reset them before each test (fixes the full-file ordering failure of the unsupported-URL test).
    httpx.post(shop.url("/shop/reset"))
    yield


@pytest.fixture
def ectx(tmp_path):
    s = Settings(_env_file=None, data_dir=tmp_path / "data", volume_encrypted_assert=True, display_backend="auto",
                 action_delay_ms=(0, 0), site_min_login_interval_seconds=0, public_base_url="http://testserver",
                 geoip_url=None)  # fmt: skip
    ctx = build_context(s, master_key=TEST_KEY, redactor=Redactor(),
                        llm_factory=lambda: HeuristicLLM(), chat_llm_factory=False)  # fmt: skip
    yield ctx
    ctx.db.close()


def _shop_profile(ctx, shop) -> str:
    site = Site(id="mock-shop", name="Mock Shop", login_url=shop.url("/shop/checkout"), domains=["127.0.0.1", "localhost"])
    ctx.sites.upsert(site)
    return ctx.profiles.create("mock-shop", "me").id


async def _place_order_ref(ex) -> str:
    await ex.run("read_page", {})
    el = next((e for e in ex.snapshot.elements if "place your order" in (e.name or "").lower()), None)
    assert el is not None, "no place-order button found on the checkout page"
    return el.ref


async def _approve_all(ctx):
    while True:
        for a in ctx.gate.pending():
            ctx.gate.decide(a.id, approve=True)
        await asyncio.sleep(0.03)


async def test_real_dom_approve_commit_confirm(ectx, shop):
    pid = _shop_profile(ectx, shop)
    run_id = ectx.runs.create("manual", pid, {})
    async with ectx.profiles.open(pid, purpose="test") as op:
        page = await op.session.page()
        await page.goto(shop.url("/shop/checkout"))
        ex = ectx.jobs._executor(op, page, run_id, HeuristicLLM())
        await ex.start()
        ref = await _place_order_ref(ex)
        approver = asyncio.create_task(_approve_all(ectx))
        res = await ex.run("click", {"ref": ref})
        approver.cancel()
    assert not res.is_error and "SHOP-ORD-7788" in res.text()
    assert httpx.get(shop.url("/shop/orders")).json()  # the order was actually placed
    assert ectx.spend.ledger.list()[0]["outcome"] == "committed"


async def test_real_dom_void_on_total_change(ectx, shop):
    # The void LOGIC is already proven deterministically by the non-Chrome fake-page tests; this test
    # proves the same wiring against a real DOM. We drive the timing ourselves (rather than racing a
    # page timer): the checkout-live page keeps #order-total in sync with the server, and the approver
    # changes the total AFTER the spend card appears and BEFORE it taps, so the pre-commit re-verify is
    # guaranteed to read a different total and void.
    pid = _shop_profile(ectx, shop)
    run_id = ectx.runs.create("manual", pid, {})
    async with ectx.profiles.open(pid, purpose="test") as op:
        page = await op.session.page()
        await page.goto(shop.url("/shop/checkout-live"))
        ex = ectx.jobs._executor(op, page, run_id, HeuristicLLM())
        await ex.start()
        ref = await _place_order_ref(ex)

        async def mutate_then_approve():
            for _ in range(500):  # wait for the spend approval card
                if ectx.gate.pending():
                    break
                await asyncio.sleep(0.02)
            httpx.post(shop.url("/shop/mutate-total"), params={"total": "9,999.00"})
            for _ in range(250):  # let the live page reflect the new total before we approve
                if "9,999" in (await page.inner_text("#order-total")):
                    break
                await asyncio.sleep(0.02)
            for a in ectx.gate.pending():
                ectx.gate.decide(a.id, approve=True)

        approver = asyncio.create_task(mutate_then_approve())
        res = await ex.run("click", {"ref": ref})
        await approver
    assert res.is_error  # the total changed before submit -> approval void, not paid
    assert httpx.get(shop.url("/shop/orders")).json() == []
    assert ectx.spend.ledger.list()[0]["outcome"] == "void"


async def test_real_dom_unsupported_payment_url_fails_closed(ectx, shop):
    pid = _shop_profile(ectx, shop)
    run_id = ectx.runs.create("manual", pid, {})
    async with ectx.profiles.open(pid, purpose="test") as op:
        page = await op.session.page()
        ex = ectx.jobs._executor(op, page, run_id, HeuristicLLM())
        await ex.start()
        res = await ex.run("goto", {"url": shop.url("/shop/pay")})  # a payment URL no adapter claims
    assert res.is_error and "remote view" in res.text()
    assert httpx.get(shop.url("/shop/orders")).json() == []


async def test_real_dom_self_submitting_page_raises_breach(ectx, shop):
    pid = _shop_profile(ectx, shop)
    run_id = ectx.runs.create("manual", pid, {})
    async with ectx.profiles.open(pid, purpose="test") as op:
        page = await op.session.page()
        ex = ectx.jobs._executor(op, page, run_id, HeuristicLLM())
        await ex.start()
        # this page submits itself on load and lands on the confirmation with no gate involvement
        res = await ex.run("goto", {"url": shop.url("/shop/checkout-autosubmit")})
    assert res.is_error  # landing check halts on the confirmation
    rows = [r for r in ectx.spend.ledger.list() if r["run_id"] == run_id]
    assert rows and rows[0]["outcome"] == "uncertain" and rows[0]["detail"].startswith("BREACH")
