"""Durable store for order/shipment events (F-18), resolving current state in code.

Events are parsed from untrusted email by friday/capabilities/orders.py; this persists them (deduped
on message id, so a re-scan adds nothing) and resolves the current per-item state on demand. Nothing
here scans by itself - a scan happens only when the owner asks in chat or through a schedule they
created. Everything stored is redacted first; no email body, no instructions, only the parsed fields.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

from friday.capabilities.orders import Item, OrderEvent, resolve_orders
from friday.util import iso


class OrderStore:
    def __init__(self, ctx: Any):
        self.ctx = ctx

    def add_events(self, events: list[OrderEvent]) -> dict[str, int]:
        red = self.ctx.redactor.redact
        new = 0
        with self.ctx.db.tx() as c:
            for ev in events:
                items = json.dumps([{"name": red(i.name)[:80], "qty": i.qty} for i in ev.items])
                cur = c.execute(
                    "INSERT OR IGNORE INTO order_events (message_id, order_key, merchant, order_id, kind, source,"
                    " items_json, carrier, tracking_id, amount, currency, refund_status, event_date, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (ev.message_id, red(ev.order_key)[:160], red(ev.merchant)[:80], red(ev.order_id)[:80], ev.kind,
                     ev.source, items, red(ev.carrier)[:40] if ev.carrier else None,
                     red(ev.tracking_id)[:60] if ev.tracking_id else None,
                     str(ev.amount) if ev.amount is not None else None, ev.currency, ev.refund_status,
                     ev.date.isoformat(), iso()),
                )  # fmt: skip
                new += cur.rowcount
        self.ctx.audit.record("orders.add_events", actor="assistant", events=len(events), new=new)
        return {"new": new, "already_known": len(events) - new}

    def _all_events(self) -> list[OrderEvent]:
        rows = self.ctx.db.query("SELECT * FROM order_events ORDER BY event_date, created_at")
        out = []
        for r in rows:
            items = tuple(Item(i["name"], int(i["qty"])) for i in json.loads(r["items_json"]))
            out.append(OrderEvent(
                kind=r["kind"], merchant=r["merchant"], order_id=r["order_id"], message_id=r["message_id"],
                date=date.fromisoformat(r["event_date"]), source=r["source"], items=items,
                carrier=r["carrier"], tracking_id=r["tracking_id"],
                amount=None if r["amount"] is None else __import__("decimal").Decimal(r["amount"]),
                currency=r["currency"], refund_status=r["refund_status"],
            ))  # fmt: skip
        return out

    def resolve(self) -> list[dict[str, Any]]:
        return resolve_orders(self._all_events())

    def open_orders(self) -> list[dict[str, Any]]:
        return [o for o in self.resolve() if o["open"]]

    def find(self, query: str) -> list[dict[str, Any]]:
        q = query.strip().lower()
        return [o for o in self.resolve() if q and (q in o["merchant"].lower() or q in o["order_id"].lower())]

    def mark_scanned(self, when: str | None = None) -> None:
        self.ctx.prefs.set("orders_last_scan", when or iso())

    def last_scanned(self) -> str | None:
        return self.ctx.prefs.get("orders_last_scan")
