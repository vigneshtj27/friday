"""Checkout adapters: the ONLY way a browser run is allowed to commit a payment.

A reliable "did money just move?" signal cannot be read from arbitrary pages by button-label guessing.
So a payment commit is allowed only inside an explicit, tested adapter for a known site, which reads
the order's total, items, delivery address and payment method through fixed selectors, performs the
commit, and reads back a confirmation. Any pay-looking action on a site without an adapter fails
closed: friday stops and the owner pays in remote view. A heuristic never decides money moved.

The adapter talks to a Playwright page through three async methods it is given (inner_text,
query_selector, click), so the routing, the gate drive and the outcome logic here are exercised by a
fake page in tests; the live page is the same shape.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol
from urllib.parse import urlparse

from friday.spend import SpendIntent

log = logging.getLogger(__name__)


class Page(Protocol):
    url: str

    async def query_selector(self, selector: str) -> Any: ...
    async def inner_text(self, selector: str) -> str: ...
    async def click(self, selector: str) -> None: ...


class CheckoutAdapter(Protocol):
    name: str

    def supports(self, url: str) -> bool: ...
    async def read_intent(self, page: Page) -> SpendIntent | None: ...  # None if the total can't be read
    async def commit(self, page: Page) -> None: ...
    async def confirmation(self, page: Page) -> str | None: ...  # an order id if the payment went through


async def _text(page: Page, selector: str) -> str:
    try:
        if await page.query_selector(selector) is None:
            return ""
        return (await page.inner_text(selector)).strip()
    except Exception:
        return ""


class MockShopAdapter:
    """A tested adapter for the in-repo mock shop checkout page."""

    name = "mock-shop"

    def supports(self, url: str) -> bool:
        host = (urlparse(url).hostname or "").lower()
        return host in ("localhost", "127.0.0.1", "mock-shop.local") and "/checkout" in urlparse(url).path

    async def read_intent(self, page: Page) -> SpendIntent | None:
        if await page.query_selector("#order-total") is None:
            return None  # no total on the page -> we cannot build an intent; caller treats as uncertain
        total = await _text(page, "#order-total")
        currency = (await _text(page, "#order-currency")) or "INR"
        merchant = (await _text(page, "#merchant")) or "mock shop"
        items_text = await _text(page, "#order-items")
        items = tuple(i.strip() for i in items_text.split(",") if i.strip())
        address = await _text(page, "#delivery-address")
        method = await _text(page, "#payment-method")
        order_id = await _text(page, "#order-id")
        try:
            intent = SpendIntent(merchant=merchant, total=total, currency=currency, items=items,
                                 address_label=address, method_label=method, order_id=order_id)  # fmt: skip
            intent.amount()  # validates the total parses
        except ValueError:
            return None
        return intent

    async def commit(self, page: Page) -> None:
        await page.click("#place-order")

    async def confirmation(self, page: Page) -> str | None:
        if await page.query_selector("#order-confirmed") is None:
            return None
        return (await _text(page, "#order-confirmed")) or "confirmed"


DEFAULT_ADAPTERS: list[CheckoutAdapter] = [MockShopAdapter()]


class SpendRouter:
    """Routes a payment commit to a tested adapter + the spend gate, or fails closed."""

    def __init__(self, spend_gate: Any, adapters: list[CheckoutAdapter] | None = None):
        self.spend_gate = spend_gate
        self.adapters = adapters if adapters is not None else DEFAULT_ADAPTERS

    def adapter_for(self, url: str) -> CheckoutAdapter | None:
        return next((a for a in self.adapters if a.supports(url)), None)

    @staticmethod
    def fail_closed_message(url: str) -> str:
        return ("This looks like a payment, and this checkout isn't supported for automatic payment yet, so I won't "
                "guess whether money would move. I've stopped here. You can complete the payment yourself in remote "
                "view, or ask me to add a tested checkout adapter for this site.")

    async def run(self, page: Page, *, task_id: str | None, run_id: str | None) -> tuple[bool, str]:
        """Drive one payment: read the intent, reserve + owner tap (bound, re-verified), commit, and
        confirm. Returns (ok, message). Never retries; an unprovable commit is left uncertain."""
        adapter = self.adapter_for(page.url)
        if adapter is None:
            return (False, self.fail_closed_message(page.url))
        intent = await adapter.read_intent(page)
        if intent is None:
            return (False, "I couldn't read the order total on this page, so I won't pay (I don't guess amounts). "
                           "Complete it in remote view if you want to go ahead.")  # fmt: skip
        async def verify() -> SpendIntent | None:
            return await adapter.read_intent(page)

        decision = await self.spend_gate.authorise(intent, task_id=task_id, run_id=run_id, verify=verify)
        if not decision.approved:
            return (False, decision.reason)
        try:
            await adapter.commit(page)
        except Exception as e:  # we intercepted the commit but it errored mid-way: cannot prove outcome
            self.spend_gate.uncertain(decision.ledger_id, f"commit errored: {type(e).__name__}")
            return (False, "the pay action errored after your approval; I can't tell whether it went through, so I've "
                           "marked it uncertain and won't retry. Check the order page or your email.")  # fmt: skip
        order_id = await adapter.confirmation(page)
        if order_id:
            self.spend_gate.committed(decision.ledger_id, order_id=order_id)
            return (True, f"Paid {intent.currency} {intent.amount()} to {intent.merchant} (order {order_id}).")
        # No confirmation visible: we cannot PROVE the payment went through -> uncertain, never retried.
        self.spend_gate.uncertain(decision.ledger_id, "no confirmation seen after submit")
        return (False, "I submitted the payment but didn't see a confirmation, so I've marked it uncertain rather than "
                       "assume it succeeded. Check the order page or your email before trying again.")  # fmt: skip
