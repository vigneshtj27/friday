"""F-18: order/shipment tracking from Gmail evidence - parser, resolver, store, and chat/sheet tools."""

from __future__ import annotations

import asyncio
from datetime import date

import pytest

from friday.capabilities.orders import (
    OrderEmail,
    parse_order_email,
    parse_order_messages,
    resolve_orders,
)

from .fake_google import SID, FakeGoogle, install
from .test_tools import approve_next


def _mail(sender, subject, body, mid="m1", d=(2026, 10, 1)):
    return OrderEmail(sender=sender, subject=subject, body=body, message_id=mid, received_date=date(*d))


def _items_list(body):
    from friday.capabilities.orders import _items

    return [(i.name, i.qty) for i in _items(body)]


def _events(*mails):
    evs, _, _ = parse_order_messages(list(mails))
    return evs


def _order(mails):
    return resolve_orders(_events(*mails))[0]


# ---- parsing + exclusions -----------------------------------------------------------------------


def test_order_confirmation_parsed():
    e = parse_order_email(_mail("auto-confirm@amazon.in", "Your order #ORD123 is confirmed",
                                "Thank you for your order.\n2 x Blue Mug\n1 x Kettle\nOrder total: INR 1,500"))
    assert e is not None and e.kind == "order_confirmation" and e.order_id == "ORD123"
    assert {i.name: i.qty for i in e.items} == {"Blue Mug": 2, "Kettle": 1}
    assert str(e.amount) == "1500"


@pytest.mark.parametrize("sender,subject,body", [
    ("safekey@americanexpress.com", "Your one-time passcode", "Your OTP is 123456 for order #X9999"),
    ("alerts@hdfcbank.net", "Payment reminder", "Your bill is due. Minimum amount due INR 500. Order #AB1234"),
    ("deals@shop.com", "FLASH SALE - 50% off everything!", "Shop now, limited time. Unsubscribe to stop."),
    ("gifts@shop.com", "Your e-gift card is ready", "Your gift voucher code is ABCD for order #GC1234"),
])
def test_exclusions_are_not_orders(sender, subject, body):
    assert parse_order_email(_mail(sender, subject, body)) is None


def test_email_without_order_id_is_skipped():
    assert parse_order_email(_mail("auto-confirm@amazon.in", "Order confirmed", "Thank you for your order. Total INR 100")) is None


# ---- split shipment + partial delivery ----------------------------------------------------------


def test_split_shipment_partial_delivery_stays_partially_open():
    o = _order([
        _mail("auto-confirm@amazon.in", "Order #S1 confirmed", "2 x Mug\n1 x Kettle\nOrder total: INR 900", "c", (2026, 10, 1)),
        _mail("shipment-tracking@amazon.in", "Your item has shipped - Order #S1", "1 x Mug\nhas shipped\ntracking: TRK111", "s1", (2026, 10, 2)),
        _mail("track@bluedart.com", "Delivered - Order #S1", "1 x Mug\nwas delivered\nAWB: TRK111", "d1", (2026, 10, 4)),
    ])
    states = {i["name"]: i["state"] for i in o["items"]}
    assert states["Mug"] == "partially_delivered"  # 1 of 2 mugs delivered
    assert states["Kettle"] == "not_shipped"
    assert o["open"] is True
    mug = next(i for i in o["items"] if i["name"] == "Mug")
    assert mug["delivered_by"] == "carrier"  # a carrier delivery outranks the merchant


def test_full_delivery_closes_the_order():
    o = _order([
        _mail("auto-confirm@amazon.in", "Order #F1 confirmed", "1 x Mug\nOrder total: INR 300", "c", (2026, 10, 1)),
        _mail("track@bluedart.com", "Delivered - Order #F1", "1 x Mug was delivered", "d", (2026, 10, 3)),
    ])
    assert o["open"] is False and o["items"][0]["state"] == "delivered"


# ---- cancellation closes only cancelled quantities ----------------------------------------------


def test_item_cancellation_closes_only_that_item():
    o = _order([
        _mail("noreply@flipkart.com", "Order #C1 placed", "1 x Mug\n1 x Kettle\nOrder total: INR 800", "c", (2026, 10, 1)),
        _mail("noreply@flipkart.com", "Your item is cancelled - Order #C1", "1 x Kettle\nhas been cancelled", "x", (2026, 10, 2)),
    ])
    states = {i["name"]: i["state"] for i in o["items"]}
    assert states["Kettle"] == "cancelled" and states["Mug"] == "not_shipped"
    assert o["open"] is True  # the Mug is still open


# ---- refund is a payment event, never fulfilment ------------------------------------------------


def test_refund_does_not_fulfil_or_close():
    o = _order([
        _mail("noreply@flipkart.com", "Order #R1 placed", "1 x Mug\nOrder total: INR 300", "c", (2026, 10, 1)),
        _mail("shipment-tracking@amazon.in", "Shipped - Order #R1", "1 x Mug has shipped tracking: T1", "s", (2026, 10, 2)),
        _mail("noreply@flipkart.com", "Refund initiated - Order #R1", "Your refund of INR 300 has been initiated", "rf", (2026, 10, 3)),
    ])
    assert o["items"][0]["state"] == "in_transit"  # a refunded order can still be in transit
    assert o["open"] is True
    assert o["refunds"] and o["refunds"][0]["status"] == "initiated" and o["refunds"][0]["amount"] == "300"


# ---- returns are a separate lifecycle -----------------------------------------------------------


def test_return_is_tracked_separately_not_a_close():
    o = _order([
        _mail("auto-confirm@amazon.in", "Order #RT1 confirmed", "1 x Mug\nOrder total: INR 300", "c", (2026, 10, 1)),
        _mail("track@bluedart.com", "Delivered - Order #RT1", "1 x Mug was delivered", "d", (2026, 10, 3)),
        _mail("order-update@amazon.in", "Return initiated - Order #RT1", "1 x Mug\nreturn initiated", "rt", (2026, 10, 5)),
    ])
    assert o["items"][0]["state"] == "delivered"  # the forward flow stays delivered
    assert o["returns"] and o["returns"][0]["items"] == ["Mug"]


# ---- conflicting evidence: not older-wins -------------------------------------------------------


def test_delivery_then_later_shipment_is_conflicting():
    o = _order([
        _mail("auto-confirm@amazon.in", "Order #K1 confirmed", "1 x Mug\nOrder total: INR 300", "c", (2026, 10, 1)),
        _mail("track@bluedart.com", "Delivered - Order #K1", "1 x Mug was delivered", "d", (2026, 10, 3)),
        _mail("shipment-tracking@amazon.in", "Shipped - Order #K1", "1 x Mug has shipped tracking: T2", "s", (2026, 10, 6)),
    ])
    mug = o["items"][0]
    assert mug["state"] == "conflicting" and mug["conflict"] and "delivery" in mug["conflict"].lower()


# ---- honest absence -----------------------------------------------------------------------------


def test_no_delivery_email_is_not_verified_undelivered():
    o = _order([
        _mail("auto-confirm@amazon.in", "Order #N1 confirmed", "1 x Mug\nOrder total: INR 300", "c", (2026, 10, 1)),
        _mail("shipment-tracking@amazon.in", "Shipped - Order #N1", "1 x Mug has shipped tracking: T3", "s", (2026, 10, 2)),
    ])
    assert o["items"][0]["state"] == "in_transit"  # shipped, no delivery mail -> in transit, not "undelivered"
    assert o["items"][0]["delivered_on"] is None


# ---- inline item parsing: real products in, prose out -------------------------------------------


@pytest.mark.parametrize("body", [
    "1 x the usual amount of luck",           # the reported false positive
    "2 x your favourite snacks",              # possessive + adjective prose, no product
    "You'll get 1 x the points on this purchase",  # marketing prose containing "1 x"
    "Save 2 x more when you order today",     # quantifier prose
    "worth 1 x your money",                   # pronoun prose
    "1 x the points",                         # bare function-word-led prose
    "1 x a great deal",                       # bare function-word-led prose
])
def test_inline_prose_parses_no_items(body):
    assert _items_list(body) == []  # a bare "N x <lowercase function word> ..." is prose, not an order line


@pytest.mark.parametrize("body,expected", [
    ("1 x Blue Mug", [("Blue Mug", 1)]),
    ("2 x steel kettle", [("steel kettle", 2)]),           # lowercase product must survive
    ("We delivered 1 x iPhone 15 Pro to you", [("iPhone 15 Pro", 1)]),
    ("Good news: 1 x Blue Mug from your order was delivered", [("Blue Mug", 1)]),
    # brands whose FIRST word is a function word but keep their own casing -> must still parse
    ("1 x The Ordinary Niacinamide serum delivered.", [("The Ordinary Niacinamide serum", 1)]),
    ("1 x All Saints t-shirt delivered.", [("All Saints t-shirt", 1)]),
    ("1 x Few and Far candle delivered.", [("Few and Far candle", 1)]),
])
def test_inline_real_products_still_parse(body, expected):
    assert _items_list(body) == expected


def test_line_anchored_items_unaffected():
    assert _items_list("1 x Blue Mug\n1 x Steel Kettle") == [("Blue Mug", 1), ("Steel Kettle", 1)]


def test_inline_capture_stays_bounded_to_six_words():
    # the bounded-capture guarantee: an inline name never runs away across a long prose tail
    # inline (mid-sentence) so the line-anchored patterns don't apply; the inline capture is bounded to
    # the first word + at most 6 continuation words and never runs away across the whole prose tail.
    body = "Shipped: 1 x Alpha Beta Gamma Delta Epsilon Zeta Eta Theta Iota Kappa today"
    names = [n for n, _ in _items_list(body)]
    assert names and all(1 <= len(n.split()) <= 7 for n in names)
    assert all("Kappa" not in n for n in names)


# ---- resolver fails CLOSED, not open, on unparseable / itemless closing events ------------------


def test_inline_partial_delivery_leaves_the_other_item_open():
    # The delivery names its item mid-sentence (not on its own line). It must parse as a Blue Mug
    # delivery - NOT as an itemless event that closes the whole order.
    o = _order([
        _mail("auto-confirm@amazon.in", "Order #IN1 confirmed", "1 x Blue Mug\n1 x Steel Kettle\nOrder total: INR 900", "c", (2026, 10, 1)),
        _mail("track@bluedart.com", "Delivered - Order #IN1", "Good news: 1 x Blue Mug from your order #IN1 was delivered.", "d", (2026, 10, 4)),
    ])
    states = {i["name"]: i["state"] for i in o["items"]}
    assert states["Blue Mug"] == "delivered"
    assert states["Steel Kettle"] == "not_shipped"  # the other parcel is still open, not hidden
    assert o["open"] is True


def test_itemless_delivery_of_multi_item_order_does_not_close_it():
    # A delivery email whose items don't parse at all must not be spread across every item of a
    # 2-item order - that is the direction that hides a missing parcel.
    o = _order([
        _mail("auto-confirm@amazon.in", "Order #UNK1 confirmed", "1 x Blue Mug\n1 x Steel Kettle\nOrder total: INR 900", "c", (2026, 10, 1)),
        _mail("track@bluedart.com", "Your package was delivered - Order #UNK1", "Your package has been delivered. Thank you.", "d", (2026, 10, 4)),
    ])
    states = {i["name"]: i["state"] for i in o["items"]}
    assert states["Blue Mug"] == "not_shipped" and states["Steel Kettle"] == "not_shipped"
    assert o["open"] is True
    assert o["notes"] and "didn't say which" in o["notes"][0]


def test_itemless_cancellation_of_two_item_order_does_not_cancel_both():
    o = _order([
        _mail("noreply@flipkart.com", "Order #CX1 placed", "1 x Blue Mug\n1 x Steel Kettle\nOrder total: INR 900", "c", (2026, 10, 1)),
        _mail("noreply@flipkart.com", "Cancellation confirmed - Order #CX1", "Your cancellation has been confirmed.", "x", (2026, 10, 2)),
    ])
    states = {i["name"]: i["state"] for i in o["items"]}
    assert states["Blue Mug"] != "cancelled" and states["Steel Kettle"] != "cancelled"
    assert o["open"] is True
    assert o["notes"] and "cancellation" in o["notes"][0]


def test_itemless_delivery_of_single_item_order_still_closes_it():
    # With only one item there is nothing to confuse, so an itemless delivery may still apply.
    o = _order([
        _mail("auto-confirm@amazon.in", "Order #ONE1 confirmed", "1 x Blue Mug\nOrder total: INR 300", "c", (2026, 10, 1)),
        _mail("track@bluedart.com", "Delivered - Order #ONE1", "Your package has been delivered.", "d", (2026, 10, 3)),
    ])
    assert o["items"][0]["state"] == "delivered" and o["open"] is False
    assert not o["notes"]


def test_pending_and_status_tools_mark_output_untrusted():
    from friday.tools.order_tools import OrderStatus, ParcelsPending

    assert ParcelsPending().untrusted_output is True
    assert OrderStatus().untrusted_output is True


# ---- store dedupe + resolve ---------------------------------------------------------------------


def test_store_dedupes_on_rescan(ctx):
    mails = [
        _mail("auto-confirm@amazon.in", "Order #D1 confirmed", "1 x Mug\nOrder total: INR 300", "c", (2026, 10, 1)),
        _mail("track@bluedart.com", "Delivered - Order #D1", "1 x Mug was delivered", "d", (2026, 10, 3)),
    ]
    evs = _events(*mails)
    r1 = ctx.orders.add_events(evs)
    assert r1["new"] == 2
    r2 = ctx.orders.add_events(evs)  # re-scan, same message ids
    assert r2["new"] == 0 and r2["already_known"] == 2
    orders = ctx.orders.resolve()
    assert len(orders) == 1 and orders[0]["open"] is False


# ---- scan tool + chat answers through the fake Google -------------------------------------------


ORDERS_HEADER = [["Order ID", "Merchant", "Placed", "Open items", "State", "Refunds", "Returns", "Last evidence"]]


@pytest.fixture
def g(ctx):
    fake = FakeGoogle(sheets={SID: {"Orders": [list(ORDERS_HEADER[0])]}}, titles={SID: "Tracker"})
    install(ctx, fake)
    return ctx, fake


async def test_scan_stores_events_and_pending_lists_them(g):
    from friday.tools.base import Call

    ctx, fake = g
    fake.mail = [
        {"id": "c", "threadId": "t", "from": "auto-confirm@amazon.in", "subject": "Order #Z1 confirmed",
         "date": "", "body": "Thank you for your order.\n1 x Mug\nOrder total: INR 300"},
        {"id": "s", "threadId": "t", "from": "shipment-tracking@amazon.in", "subject": "Shipped - Order #Z1",
         "date": "", "body": "1 x Mug has shipped tracking: T1"},
    ]
    call = Call(ctx=ctx, owner_text="scan my order email", plan_id="p1")
    out = await ctx.tools.call("orders_scan", {"start": "2026-10-01", "end": "2026-10-31"}, call)
    assert out["events"] == 2 and out["new"] == 2
    pending = await ctx.tools.call("parcels_pending", {}, call)
    assert pending["count"] == 1 and pending["open"][0]["merchant"] and pending["last_scanned"]


async def test_orders_to_sheet_appends_with_readback(g):
    from friday.tools.base import Call

    ctx, fake = g
    ctx.orders.add_events(_events(
        _mail("auto-confirm@amazon.in", "Order #W1 confirmed", "1 x Mug\nOrder total: INR 300", "c", (2026, 10, 1)),
        _mail("shipment-tracking@amazon.in", "Shipped - Order #W1", "1 x Mug has shipped tracking: T1", "s", (2026, 10, 2)),
    ))
    call = Call(ctx=ctx, owner_text="put my orders in a sheet", plan_id="p1")

    async def write():
        return await ctx.tools.call("orders_to_sheet", {"spreadsheet": SID, "tab": "Orders"}, call)

    task = asyncio.create_task(write())
    await approve_next(ctx, True)  # writing to a not-owner-named sheet asks for a tap
    out = await task
    assert out["appended"] == 1 and out["ok"]
    body = [r for r in fake.sheets[SID]["Orders"][1:] if any(str(c).strip() for c in r)]
    assert len(body) == 1 and body[0][0] == "W1"

    task2 = asyncio.create_task(write())
    await approve_next(ctx, True)
    again = await task2
    assert again["appended"] == 0  # dedupe on re-run (state unchanged)
