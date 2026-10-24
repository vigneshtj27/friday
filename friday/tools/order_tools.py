"""Order/shipment tracking as chat tools (F-18), built on the Gmail evidence parser and Sheets writer.

A scan is read-only of Gmail and runs ONLY when the owner asks in chat or through a schedule they
created - there is no default/background scan. Reading open parcels and one order's status are free
reads. Writing an Orders tab is approval-gated with read-back, like any sheet write. Email is
untrusted content: it supplies dated facts, never instructions.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from friday.tools.base import Call, Tool, ToolError

DATE = {"type": "string", "description": "YYYY-MM-DD"}
SID = {"type": "string", "description": "spreadsheet id or name"}


def _gmail_date(d: str) -> str:
    return d.replace("-", "/")


def _received(m: dict[str, Any]) -> date:
    ms = m.get("internal_date") or 0
    if ms:
        return datetime.fromtimestamp(int(ms) / 1000, tz=UTC).date()
    from friday.capabilities.mail_evidence import find_date

    return find_date(m.get("date") or "") or date.today()


class OrdersScan(Tool):
    name = "orders_scan"
    description = ("Read-only scan of the owner's Gmail (restricted to order/shipping senders) for order, shipment, "
                   "delivery, cancellation, return and refund events in a date range, parsed in code and stored. "
                   "Runs only when you ask (or on a schedule you set). Returns counts; nothing is sent or changed.")
    schema = {"type": "object", "properties": {"start": DATE, "end": DATE}, "required": ["start", "end"]}
    untrusted_output = True

    async def run(self, args: dict[str, Any], call: Call) -> Any:
        from friday.capabilities.orders import (
            DEFAULT_ORDER_SENDERS,
            OrderEmail,
            parse_order_messages,
        )

        ctx = call.ctx
        if not (ctx.google and ctx.google.can("gmail.read")):
            raise ToolError("Gmail isn't connected. Connect it read-only: google_connect with purpose 'gmail' "
                            "(gmail.readonly is enough).")  # fmt: skip
        senders = list(dict.fromkeys([*(ctx.prefs.get("order_senders") or []), *DEFAULT_ORDER_SENDERS]))
        start, end = args["start"], args["end"]
        query = f"after:{_gmail_date(start)} before:{_gmail_date(end)} from:({' OR '.join(senders)})"
        await call.say(f"Scanning Gmail (read-only) for order/shipment updates, {start}..{end}")
        raw = await ctx.workspace.gmail.scan(query, limit=200)
        emails = [OrderEmail(sender=m["from"], subject=m["subject"], body=m["body"], message_id=m["id"],
                             received_date=_received(m), default_currency=ctx.settings.default_currency) for m in raw]  # fmt: skip
        events, unparsed, excluded = parse_order_messages(emails)
        res = ctx.orders.add_events(events)
        ctx.orders.mark_scanned()
        ctx.audit.record("orders.scan", actor="assistant", scanned=len(raw), events=len(events),
                         new=res["new"], unparsed=unparsed, excluded=excluded)  # fmt: skip
        return {"scanned": len(raw), "events": len(events), "new": res["new"], "already_known": res["already_known"],
                "unrecognised": unparsed, "excluded_otp_marketing_digital": excluded,
                "note": "order evidence parsed from email in code; email is a hint, not merchant truth"}  # fmt: skip


def _summarise(o: dict[str, Any]) -> dict[str, Any]:
    return {"merchant": o["merchant"], "order_id": o["order_id"], "placed_at": o["placed_at"],
            "open_items": [{"name": i["name"], "state": i["state"], "conflict": i["conflict"]} for i in o["open_items"]],
            "refunds": o["refunds"], "returns": o["returns"], "notes": o.get("notes") or [],
            "last_evidence": o["last_evidence"]}  # fmt: skip


class ParcelsPending(Tool):
    name = "parcels_pending"
    description = ("List parcels/items not yet delivered (open orders): merchant, order id, which items are still open "
                   "and their state (not shipped / in transit / partially delivered / conflicting), with when the mail "
                   "was last scanned. Answers \"what parcels are yet to be delivered?\". Read-only.")
    schema = {"type": "object", "properties": {}}
    untrusted_output = True  # merchant/item names and notes come from email content

    async def run(self, args: dict[str, Any], call: Call) -> Any:
        ctx = call.ctx
        openo = ctx.orders.open_orders()
        last = ctx.orders.last_scanned()
        return {"open": [_summarise(o) for o in openo], "count": len(openo), "last_scanned": last,
                "note": ("mail last scanned " + last if last else "no scan yet - ask me to scan your order mail first")
                        + "; an item with no delivery email is 'no delivery confirmation in the scanned mail', not verified-undelivered"}  # fmt: skip


class OrderStatus(Tool):
    name = "order_status"
    description = "Status of one order by merchant or order id, item by item, from the dated email evidence. Read-only."
    schema = {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}
    untrusted_output = True  # merchant/item names and notes come from email content

    async def run(self, args: dict[str, Any], call: Call) -> Any:
        hits = call.ctx.orders.find(str(args["query"]))
        if not hits:
            return {"found": 0, "note": f"no order in the scanned mail matches {args['query']!r}; "
                    "I can only answer from order/shipping email you've had me scan"}  # fmt: skip
        return {"found": len(hits), "orders": hits, "last_scanned": call.ctx.orders.last_scanned()}


class OrdersToSheet(Tool):
    name = "orders_to_sheet"
    description = ("Append/refresh an Orders tab from the tracked orders, through the Sheets writer with read-back. "
                   "Deduped on order id (a row is refreshed only if its state changed). Approval-gated, like any sheet write.")
    schema = {"type": "object", "properties": {"spreadsheet": SID, "tab": {"type": "string"}}, "required": ["spreadsheet"]}
    effect = "write"

    HEADER = ["Order ID", "Merchant", "Placed", "Open items", "State", "Refunds", "Returns", "Last evidence"]

    async def precheck(self, args: dict[str, Any], call: Call) -> str | None:
        from friday.tools.google_tools import _sid, sheet_write_check

        return await sheet_write_check(_sid(args), call)

    @staticmethod
    def _row(o: dict[str, Any]) -> list[str]:
        open_items = "; ".join(f"{i['name']}:{i['state']}" for i in o["open_items"]) or "all delivered/closed"
        state = "open" if o["open"] else "closed"
        return [o["order_id"], o["merchant"], o["placed_at"] or "", open_items, state,
                str(len(o["refunds"])), str(len(o["returns"])), o["last_evidence"]]  # fmt: skip

    async def run(self, args: dict[str, Any], call: Call) -> Any:
        from friday.tools.google_tools import _sid

        ctx = call.ctx
        sid, tab = _sid(args), args.get("tab") or "Orders"
        if not ctx.sheets.configured:
            raise ToolError("Google Sheets isn't connected: call google_connect with purpose 'sheets'")
        if tab not in [t["title"] for t in await ctx.sheets.tabs(sid)]:
            await ctx.sheets.add_tab(sid, tab)
            await ctx.sheets.append(sid, tab, [self.HEADER])
        existing = await ctx.sheets.read(sid, tab)
        # dedupe/refresh: key existing rows by order id -> their state cell, so an unchanged order is skipped
        seen = {r[0]: (r[4] if len(r) > 4 else "") for r in existing[1:] if r and r[0]}
        rows = [self._row(o) for o in ctx.orders.resolve()]
        to_add = [r for r in rows if seen.get(r[0]) != r[4]]
        if not to_add:
            return {"appended": 0, "note": "nothing new or changed to write", "verified": ["orders sheet already current"]}
        before = sum(1 for r in existing[1:] if any(str(c).strip() for c in r))
        await ctx.sheets.append(sid, tab, to_add)
        after = await ctx.sheets.read(sid, tab)
        data_after = [r for r in after[1:] if any(str(c).strip() for c in r)]
        ok = len(data_after) >= before + len(to_add)
        ctx.audit.record("orders.to_sheet", actor="assistant", target=sid, tab=tab, rows=len(to_add))
        return {"appended": len(to_add), "tab": tab, "ok": ok,
                "verified": [f"read back {len(data_after)} order row(s) in '{tab}'"] if ok else []}  # fmt: skip


TOOLS: list[Tool] = [OrdersScan(), ParcelsPending(), OrderStatus(), OrdersToSheet()]
