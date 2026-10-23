"""Spend controls as chat tools (friday/spend.py).

Setting spend caps is the owner changing their own limits: owner_quote plus a tap on a card, so no
web page can raise a cap. Reading the spend history is free. The actual authorising of a payment is
done by the SpendGate at the moment of paying (a per-action card bound to the exact order); these
tools are just the owner-facing configuration and record.
"""

from __future__ import annotations

from typing import Any

from friday.spend import SpendCaps, _money
from friday.tools.base import Call, Tool, ToolError


class SetSpendCaps(Tool):
    name = "set_spend_caps"
    description = ("Set the owner's spend caps: a per-order limit and a per-day limit, in a currency. Enforced in code - a "
                   "payment over a cap is refused, not carded. Clearing a field removes that cap. Use for \"set my "
                   "per-order limit to 2000\".")
    schema = {"type": "object", "properties": {
        "per_order": {"type": ["number", "string", "null"]},
        "daily": {"type": ["number", "string", "null"]},
        "currency": {"type": "string"},
    }}  # fmt: skip
    effect = "account_change"  # always a card: this changes a money control
    needs_owner_quote = True

    def approval(self, args: dict[str, Any], call: Call) -> str | None:
        cur = (args.get("currency") or call.ctx.settings.default_currency or "INR").upper()
        return f"Set spend caps: per-order {args.get('per_order')}, daily {args.get('daily')} ({cur})"

    async def run(self, args: dict[str, Any], call: Call) -> Any:
        cur = (args.get("currency") or call.ctx.settings.default_currency or "INR").upper()
        out: dict[str, Any] = {"currency": cur}
        for k in ("per_order", "daily"):
            v = args.get(k)
            if v in (None, ""):
                out[k] = None
            else:
                try:
                    out[k] = str(_money(v))
                except ValueError as e:
                    raise ToolError(str(e)) from e
        call.ctx.prefs.set("spend_caps", out)
        return {"spend_caps": out, "verified": [f"caps set: per-order {out['per_order']}, daily {out['daily']} {cur}"]}


class SpendHistory(Tool):
    name = "spend_history"
    description = "Show recent payments friday attempted and their outcome (committed/failed/uncertain/refused/denied/void)."
    schema = {"type": "object", "properties": {"limit": {"type": "integer"}}}

    async def run(self, args: dict[str, Any], call: Call) -> Any:
        rows = call.ctx.spend.ledger.list(limit=min(int(args.get("limit") or 20), 100))
        caps = SpendCaps.from_prefs(call.ctx)
        return {"spend": [{"merchant": r["merchant"], "total": r["total"], "currency": r["currency"],
                           "outcome": r["outcome"], "order_id": r["order_id"], "at": r["created_at"]} for r in rows],
                "spent_today": str(call.ctx.spend.ledger.spent_today(caps.currency)),
                "caps": {"per_order": str(caps.per_order) if caps.per_order else None,
                         "daily": str(caps.daily) if caps.daily else None, "currency": caps.currency}}  # fmt: skip


TOOLS: list[Tool] = [SetSpendCaps(), SpendHistory()]
