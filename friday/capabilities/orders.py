"""Order and shipment tracking from Gmail evidence - deterministically, in code.

An email is untrusted content: it supplies dated facts (an order was confirmed, a parcel shipped, an
item was delivered), never instructions. Bodies are read only with fixed patterns, never passed to the
model, never logged, and nothing in them triggers a tool call, plan, memory write or schedule.

The model is an ORDER with LINE ITEMS, and a stream of dated EVENTS (confirmation, shipment, delivery,
cancellation, return, refund) that each name which items/quantities they cover. Current state is
resolved per item from the newest, most reliable evidence (a carrier's delivery outranks a merchant's
marketing). A split order stays partially open until every item is delivered or cancelled. A refund is
a PAYMENT event - it never fulfils or closes a shipment. A return is its own lifecycle. "No delivery
email found" is NOT "verified undelivered" - the two are reported differently. OTP, payment-auth and
payment-reminder mail are not orders; marketing and digital goods (vouchers, subscriptions) are
excluded from physical-parcel answers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from friday.capabilities.mail_evidence import find_amount

# Event kinds, in the order we break ties when two events share a date (confirmation first, delivery
# before a same-day shipment is impossible in practice; this only orders bookkeeping).
KINDS = ("order_confirmation", "shipment", "out_for_delivery", "delivery", "cancellation", "return", "refund")
_KIND_RANK = {k: i for i, k in enumerate(KINDS)}

# Carrier senders/domains: their delivery/shipment events outrank a merchant's own ("source": carrier).
CARRIERS = {
    "bluedart": "BlueDart", "delhivery": "Delhivery", "fedex": "FedEx", "dhl": "DHL", "ups": "UPS",
    "usps": "USPS", "dtdc": "DTDC", "ekart": "Ekart", "shadowfax": "Shadowfax", "xpressbees": "XpressBees",
    "aramex": "Aramex", "shiprocket": "Shiprocket", "amazon logistics": "Amazon Logistics",
}

# Default merchant/carrier senders a scan may look at. The owner extends via prefs 'order_senders';
# the Gmail query is always restricted to senders, never a blanket inbox scan.
DEFAULT_ORDER_SENDERS = [
    "auto-confirm@amazon.in", "shipment-tracking@amazon.in", "order-update@amazon.in", "ship-confirm@amazon.in",
    "noreply@flipkart.com", "order@myntra.com", "noreply@ajio.com", "orders@nykaa.com",
    "track@bluedart.com", "noreply@delhivery.com", "track@dtdc.com", "info@ekartlogistics.com",
    "TrackingUpdates@fedex.com", "noreply@dhl.com", "auto-reply@ups.com",
]

# Exclusions: these are never orders. Checked before any order pattern.
_OTP_RE = re.compile(r"\b(one[- ]time (pass)?code|verification code|otp|security code|2fa|two[- ]factor|safekey|3-?d secure|login code)\b", re.IGNORECASE)
_PAYMENT_ONLY_RE = re.compile(r"\b(payment reminder|autopay|auto-?debit|bill (is )?due|minimum (amount )?due|pay your bill|statement is ready|e-?mandate|payment (failed|declined)|renew your)\b", re.IGNORECASE)
_MARKETING_RE = re.compile(r"\b(% off|\d+% (off|discount)|sale ends|flash sale|deal of the day|newsletter|shop now|limited time|unsubscribe to stop|exclusive offer|coupon code inside)\b", re.IGNORECASE)
_DIGITAL_RE = re.compile(r"\b(e-?gift|gift card|gift voucher|voucher code|promo code|digital (download|code)|subscription (renewed|activated|confirmed)|membership (renewed|activated)|recharge successful)\b", re.IGNORECASE)

# An order id: the token after "order [#/id/no]", required to contain a digit so plain words
# ("order total", "order confirmed") are never mistaken for one.
_ORDER_ID_RE = re.compile(r"\border\s*(?:#|id|no\.?|number)?\s*[:#]?\s*((?=[A-Za-z0-9-]*\d)[A-Z0-9][A-Z0-9-]{1,})\b", re.IGNORECASE)
_TRACKING_RE = re.compile(r"\b(?:tracking|awb|consignment|waybill)\s*(?:#|id|no\.?|number)?\s*[:#]?\s*([A-Z0-9][A-Z0-9-]{5,})\b", re.IGNORECASE)
# Line items: "2 x Widget", "2x Widget", "Widget x 2", "Widget (Qty: 2)". Line-anchored (MULTILINE),
# end matched by lookahead so adjacent lines both match.
_ITEM_RES = [
    re.compile(r"^\s*(?P<qty>\d{1,3})\s*[x×]\s*(?P<name>[A-Za-z0-9][\w &'./\-]{2,70}?)\s*(?=$|,)", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^\s*(?P<name>[A-Za-z0-9][\w &'./\-]{2,70}?)\s*[x×]\s*(?P<qty>\d{1,3})\b", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^\s*(?P<name>[A-Za-z0-9][\w &'./\-]{2,70}?)\s*\(?\s*qty\.?\s*[:=]?\s*(?P<qty>\d{1,3})\s*\)?", re.IGNORECASE | re.MULTILINE),
]
# Inline "1 x Blue Mug from your order ... was delivered": real merchant/carrier mail names the item
# mid-sentence, not on its own line. Matched anywhere; the name runs word by word until a preposition,
# status verb or "order" ends it, so prose after the item is not captured. Best-effort, and _NAME_STOP
# trims any status word that still slips in. Without this, an inline delivery parses as itemless - which
# the resolver (below) would otherwise spread across the whole order and hide a missing parcel.
_INLINE_STOP = (r"from|for|of|in|to|on|at|and|with|has|have|had|was|were|is|are|been|will|shipped|"
                r"delivered|dispatched|cancell\w*|returned?|refund\w*|tracking|awb|consignment|waybill|"
                r"order|out|arriving|expected|your|the|a|an")
_INLINE_ITEM_RE = re.compile(
    rf"(?<![\w/])(?P<qty>\d{{1,3}})[^\S\n]*[x×][^\S\n]+(?P<name>[A-Za-z][\w'&.\-]*(?:[^\S\n]+(?!(?:{_INLINE_STOP})\b)[A-Za-z0-9][\w'&.\-]*){{0,6}})",
    re.IGNORECASE)

# The item name's FIRST word decides product vs prose, across ALL the patterns above. A real order line
# starts with a product/brand word ("Blue Mug", "steel kettle", "iPhone"); prose captured by a bare
# "N x ..." starts with a function or vague-amount word ("1 x the usual amount", "2 x your favourite
# snacks", "1 x the points"). Rejecting a name whose first word is one of these drops the prose without a
# blanket stop-list that would also kill lowercase product names. (Done as a post-filter, not in each
# regex, so it covers the line-anchored and inline forms alike.)
_FIRST_STOP_WORDS = frozenset(
    "the a an this that these those your my our their his her its "
    "of for to in on at by with and or from as "
    "usual amount favourite favorite lot lots couple bunch few some several many "
    "more less most any all each every same other another number kind sort type bit".split()
)


@dataclass(frozen=True)
class OrderEmail:
    sender: str
    subject: str
    body: str
    message_id: str
    received_date: date
    default_currency: str = "INR"


@dataclass(frozen=True)
class Item:
    name: str
    qty: int


@dataclass(frozen=True)
class OrderEvent:
    kind: str
    merchant: str
    order_id: str
    message_id: str
    date: date
    source: str = "merchant"  # merchant | carrier
    items: tuple[Item, ...] = ()  # empty = applies to the whole order
    carrier: str | None = None
    tracking_id: str | None = None
    amount: Decimal | None = None
    currency: str | None = None
    refund_status: str | None = None  # initiated | completed (for kind == refund)

    @property
    def order_key(self) -> str:
        # Key by order id alone: a carrier's delivery email (merchant "BlueDart") must link to the
        # merchant's order of the same id. Order ids colliding across merchants in one mailbox is rare.
        return self.order_id.lower()


def _merchant_of(e: OrderEmail) -> str:
    low = (e.sender + " " + e.subject).lower()
    for key, label in CARRIERS.items():
        if key in low:
            return label
    dom = e.sender.split("@")[-1].strip(">") if "@" in e.sender else e.sender
    parts = [p for p in dom.split(".") if p not in ("com", "in", "co", "net", "org", "www")]
    return (parts[-1] if parts else dom).title()


def _is_carrier(e: OrderEmail) -> bool:
    low = (e.sender + " " + e.subject + " " + e.body[:200]).lower()
    return any(k in low for k in CARRIERS)


# Status words that end an item name (so "1 x Mug was delivered" yields "Mug", not the whole phrase).
_NAME_STOP = re.compile(r"\b(was|were|has|have|had|is|are|been|will|shipped|delivered|dispatched|cancell?ed|returned|refund\w*|tracking|awb|consignment|out for delivery|on (its|the) way|arriving|expected)\b", re.IGNORECASE)


def _items(body: str) -> tuple[Item, ...]:
    found: dict[str, int] = {}
    for rx in (*_ITEM_RES, _INLINE_ITEM_RE):
        for m in rx.finditer(body):
            name = re.sub(r"\s+", " ", m.group("name"))
            cut = _NAME_STOP.search(name)  # drop any trailing status prose captured on the same line
            if cut:
                name = name[: cut.start()]
            name = name.strip(" .,-")
            try:
                qty = int(m.group("qty"))
            except (ValueError, TypeError):
                continue
            first = name.split()[0].lower() if name else ""
            if (name and 0 < qty <= 999 and first not in _FIRST_STOP_WORDS
                    and name.lower() not in {"order", "total", "qty", "quantity", "item", "items"}):  # fmt: skip
                found[name] = max(found.get(name, 0), qty)
    return tuple(Item(n, q) for n, q in found.items())


def _excluded(e: OrderEmail) -> bool:
    hay = e.subject + "\n" + e.body
    if _OTP_RE.search(hay) or _PAYMENT_ONLY_RE.search(hay) or _DIGITAL_RE.search(hay):
        return True
    # Marketing: promo language AND no order id (a real order confirmation can mention a discount).
    return bool(_MARKETING_RE.search(hay) and not _ORDER_ID_RE.search(hay))


def _order_id(e: OrderEmail) -> str | None:
    m = _ORDER_ID_RE.search(e.subject) or _ORDER_ID_RE.search(e.body)
    return m.group(1) if m else None


# ---- classification --------------------------------------------------------

_DELIVERED_RE = re.compile(r"\b(has been delivered|was delivered|delivered (on|to|successfully)|delivery (complete|successful)|package delivered|your (order|parcel|item|package) (has been|was) delivered)\b", re.IGNORECASE)
_OUT_FOR_DELIVERY_RE = re.compile(r"\bout for delivery|arriving today|will be delivered today\b", re.IGNORECASE)
_SHIPPED_RE = re.compile(r"\b(has shipped|was shipped|shipped|dispatched|on its way|on the way|handed to the carrier|picked up by)\b", re.IGNORECASE)
_CANCEL_RE = re.compile(r"\b(cancell?ed|cancellation (confirmed|complete)|your (order|item) (has been|was) cancell?ed)\b", re.IGNORECASE)
_RETURN_RE = re.compile(r"\b(return (initiated|requested|received|complete|picked up)|pickup scheduled|has been returned)\b", re.IGNORECASE)
_REFUND_RE = re.compile(r"\brefund\b", re.IGNORECASE)
_REFUND_DONE_RE = re.compile(r"\brefund (processed|completed|credited|issued|of .* (processed|credited))\b", re.IGNORECASE)
_CONFIRM_RE = re.compile(r"\b(order\b[\s#:\w-]{0,24}?(confirmed|placed|received)|(confirmed|placed|received)[\s:]{0,4}your order|thank you for your (order|purchase)|your order (is|has been)|order summary|we('| ha)ve received your order)\b", re.IGNORECASE)


def parse_order_email(e: OrderEmail) -> OrderEvent | None:
    """One dated order event from an email, or None (excluded or unrecognised). Patterns are checked
    most-specific first; refund/return/cancel/delivery outrank a generic confirmation."""
    if _excluded(e):
        return None
    oid = _order_id(e)
    if not oid:
        return None  # without an order id we can't link events reliably; skip rather than guess
    hay = e.subject + "\n" + e.body
    merchant = _merchant_of(e)
    carrier = next((label for key, label in CARRIERS.items() if key in hay.lower()), None)
    src = "carrier" if _is_carrier(e) else "merchant"
    common = {"merchant": merchant, "order_id": oid, "message_id": e.message_id, "date": e.received_date, "source": src}

    if _REFUND_RE.search(hay) and not _CONFIRM_RE.search(e.subject):
        amt = find_amount(e.body, default_currency=e.default_currency, prefer_near=r"refund")
        status = "completed" if _REFUND_DONE_RE.search(hay) else "initiated"
        return OrderEvent(kind="refund", amount=amt[0] if amt else None, currency=amt[1] if amt else None,
                          refund_status=status, **common)  # fmt: skip
    if _RETURN_RE.search(hay):
        return OrderEvent(kind="return", items=_items(e.body), **common)
    if _CANCEL_RE.search(hay):
        return OrderEvent(kind="cancellation", items=_items(e.body), **common)
    if _DELIVERED_RE.search(hay):
        return OrderEvent(kind="delivery", items=_items(e.body),
                          carrier=carrier, tracking_id=(_TRACKING_RE.search(hay) or [None, None])[1]
                          if _TRACKING_RE.search(hay) else None, **common)  # fmt: skip
    if _OUT_FOR_DELIVERY_RE.search(hay):
        t = _TRACKING_RE.search(hay)
        return OrderEvent(kind="out_for_delivery", items=_items(e.body), carrier=carrier,
                          tracking_id=t.group(1) if t else None, **common)  # fmt: skip
    if _SHIPPED_RE.search(hay):
        t = _TRACKING_RE.search(hay)
        return OrderEvent(kind="shipment", items=_items(e.body), carrier=carrier,
                          tracking_id=t.group(1) if t else None, **common)  # fmt: skip
    if _CONFIRM_RE.search(hay):
        amt = find_amount(e.body, default_currency=e.default_currency,
                          prefer_near=r"order total|grand total|total (?:amount|paid)")  # fmt: skip
        return OrderEvent(kind="order_confirmation", items=_items(e.body),
                          amount=amt[0] if amt else None, currency=amt[1] if amt else None, **common)  # fmt: skip
    return None


def parse_order_messages(messages: list[OrderEmail]) -> tuple[list[OrderEvent], int, int]:
    """Parse a batch; return (events, unrecognised_count, excluded_count)."""
    events, unparsed, excluded = [], 0, 0
    for e in messages:
        if _excluded(e):
            excluded += 1
            continue
        ev = parse_order_email(e)
        if ev is None:
            unparsed += 1
        else:
            events.append(ev)
    return events, unparsed, excluded


# ---- resolution ------------------------------------------------------------


@dataclass
class _ItemState:
    ordered: int = 0
    shipped: int = 0
    delivered: int = 0
    cancelled: int = 0
    returned: int = 0
    delivered_source: str | None = None
    delivered_date: date | None = None
    conflict: str | None = None  # a human note naming the two contradictory evidences


def _apply_to_items(states: dict[str, _ItemState], ev_items: tuple[Item, ...], field_name: str) -> None:
    targets = {i.name: i.qty for i in ev_items} if ev_items else {n: max(st.ordered, 1) for n, st in states.items()}
    if not targets and not ev_items:
        targets = {"(whole order)": 1}
    for name, qty in targets.items():
        st = states.setdefault(name, _ItemState())
        setattr(st, field_name, getattr(st, field_name) + qty)


def resolve_order(events: list[OrderEvent]) -> dict[str, Any]:
    """Resolve one order's current per-item state from its dated event stream."""
    evs = sorted(events, key=lambda e: (e.date, _KIND_RANK.get(e.kind, 99)))
    order_id = evs[0].order_id
    # The merchant is the seller, not the carrier: prefer the confirmation, then any non-carrier event.
    merchant = next((e.merchant for e in evs if e.kind == "order_confirmation"),
                    next((e.merchant for e in evs if e.source != "carrier"), evs[0].merchant))  # fmt: skip
    states: dict[str, _ItemState] = {}
    total = currency = placed_at = None
    refunds: list[dict[str, Any]] = []
    returns: list[dict[str, Any]] = []
    notes: list[str] = []
    last_evidence = evs[-1].date

    def _closing_is_ambiguous(ev: OrderEvent, verb: str) -> bool:
        # A delivery/cancellation that names no parseable item must NOT be spread across every item of a
        # multi-item order: doing so would mark pending parcels delivered/cancelled from one vague email -
        # the direction that hides a missing parcel. Keep the items in their current state and note it.
        # A single-item (or not-yet-itemised) order may still apply it: there is no other item to confuse.
        if ev.items or len(states) <= 1:
            return False
        notes.append(f"a {verb} email on {ev.date} (msg {ev.message_id}) didn't say which item(s) - not "
                     f"applied; the order's parcels are kept in their known state")  # fmt: skip
        return True

    for ev in evs:
        if ev.kind == "order_confirmation":
            placed_at = placed_at or ev.date
            total = total or (str(ev.amount) if ev.amount is not None else None)
            currency = currency or ev.currency
            for it in ev.items:
                st = states.setdefault(it.name, _ItemState())
                st.ordered = max(st.ordered, it.qty)
        elif ev.kind in ("shipment", "out_for_delivery"):
            # a shipment of an item already delivered (earlier) contradicts that delivery
            for name in ({i.name for i in ev.items} or set(states)):
                st = states.get(name)
                if st and st.delivered_date and ev.date > st.delivered_date and st.conflict is None:
                    st.conflict = (f"a delivery on {st.delivered_date} then a later {ev.kind} on {ev.date} "
                                   f"(msg {ev.message_id}) - state unknown")  # fmt: skip
            _apply_to_items(states, ev.items, "shipped")
        elif ev.kind == "delivery":
            if _closing_is_ambiguous(ev, "delivery"):
                continue
            _apply_to_items(states, ev.items, "delivered")
            for name in ({i.name for i in ev.items} or set(states)):
                st = states.setdefault(name, _ItemState())
                # carrier delivery outranks a merchant one; keep the most reliable, newest
                if st.delivered_source != "carrier" or ev.source == "carrier":
                    st.delivered_source, st.delivered_date = ev.source, ev.date
        elif ev.kind == "cancellation":
            if _closing_is_ambiguous(ev, "cancellation"):
                continue
            _apply_to_items(states, ev.items, "cancelled")
        elif ev.kind == "return":
            _apply_to_items(states, ev.items, "returned")
            returns.append({"date": ev.date.isoformat(), "items": [i.name for i in ev.items], "message_id": ev.message_id})
        elif ev.kind == "refund":
            refunds.append({"status": ev.refund_status, "amount": str(ev.amount) if ev.amount is not None else None,
                            "currency": ev.currency, "date": ev.date.isoformat(), "message_id": ev.message_id})  # fmt: skip

    items_out = []
    for name, st in states.items():
        items_out.append({"name": name, "ordered": st.ordered, "state": _item_state(st),
                          "delivered_by": st.delivered_source, "delivered_on": st.delivered_date.isoformat() if st.delivered_date else None,
                          "conflict": st.conflict})  # fmt: skip
    open_items = [i for i in items_out if i["state"] in ("not_shipped", "in_transit", "partially_delivered", "conflicting")]
    return {
        "merchant": merchant, "order_id": order_id, "placed_at": placed_at.isoformat() if placed_at else None,
        "total": total, "currency": currency, "items": items_out, "open": bool(open_items),
        "open_items": open_items, "refunds": refunds, "returns": returns,
        "notes": notes, "last_evidence": last_evidence.isoformat(),
    }


def _item_state(st: _ItemState) -> str:
    ordered = st.ordered
    if st.conflict:
        return "conflicting"
    if ordered and st.cancelled >= ordered:
        return "cancelled"
    if ordered and st.delivered >= ordered:
        return "delivered"
    if st.delivered > 0 and (not ordered or st.delivered < ordered):
        return "partially_delivered"
    if st.shipped > 0:
        return "in_transit"
    if st.cancelled > 0 and not ordered:
        return "cancelled"
    if st.delivered > 0:
        return "delivered"
    return "not_shipped"


def resolve_orders(events: list[OrderEvent]) -> list[dict[str, Any]]:
    by_key: dict[str, list[OrderEvent]] = {}
    for ev in events:
        by_key.setdefault(ev.order_key, []).append(ev)
    out = [resolve_order(evs) for evs in by_key.values()]
    out.sort(key=lambda o: o["last_evidence"], reverse=True)
    return out
