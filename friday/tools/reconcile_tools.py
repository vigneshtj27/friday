"""Reconcile a card/bank portal's activity into the owner's expense sheet, and refresh the Cards summary.

READ and RECONCILE only. This never touches a payment, dispute or card-setting control - it only reads
the portal's activity (supplied by a fetch) and writes to the sheet, behind the owner's approval card,
with a dry-run first, snapshot/version conflict detection, and a read-back check. Pending charges are
entered with a "(PENDING)" marker; when they post, the existing row is updated in place (never doubled);
ambiguous pairs are reported, never auto-settled; Comments are never changed.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from typing import Any

from friday.reconcile_sheet import PortalTxn, reconcile
from friday.sheets import safe_text, spreadsheet_id
from friday.tools.base import Call, Tool, ToolError

DATEARG = {"type": "string", "description": "YYYY-MM-DD"}
_TXN = {"type": "object", "properties": {
    "date": {"type": "string"}, "narration": {"type": "string"}, "amount": {"type": "string"},
    "direction": {"type": "string", "enum": ["debit", "credit"]}, "currency": {"type": "string"},
}, "required": ["date", "narration", "amount"]}  # fmt: skip


def _to_portal(items: list[dict[str, Any]], status: str) -> list[PortalTxn]:
    out = []
    for it in items or []:
        out.append(PortalTxn(
            date=date.fromisoformat(str(it["date"])[:10]),
            description=str(it["narration"]),
            amount=abs(Decimal(str(it["amount"]).replace(",", ""))),
            direction=("credit" if str(it.get("direction") or "debit").lower().startswith("c") else "debit"),
            currency=str(it.get("currency") or "INR").upper()[:3],
            status=status,
        ))  # fmt: skip
    return out


async def apply_reconcile(
    ctx: Any, *, spreadsheet: str, tab: str, portal: list[PortalTxn], source_label: str,
    start: date, end: date, dry_run: bool, column_map: dict[str, str] | None = None,
) -> dict[str, Any]:  # fmt: skip
    """Snapshot the tab, build the plan, and (unless dry_run) write it in place after a second read
    confirms nothing changed under us, then read back and verify the plan is now a no-op (idempotent)."""
    sid = spreadsheet_id(spreadsheet)
    if not ctx.sheets.configured:
        raise ToolError("Google Sheets isn't connected: call google_connect with purpose 'sheets'")
    snap1 = await ctx.sheets.read(sid, tab)
    plan = reconcile(snap1, portal, source_label=source_label, start=start, end=end, column_map=column_map)
    report = {"spreadsheet": sid, "tab": tab, "dry_run": dry_run, **plan.summary()}
    if plan.conflicts:
        return {"ok": False, "paused": True, **report}
    report["would_change"] = len(plan.appends) + len(plan.settles)
    if dry_run:
        report["ok"] = True
        report["note"] = "dry run - nothing written; review the plan, then run again with dry_run=false to apply"
        return report
    # conflict detection: a concurrent edit between the snapshot and the write pauses the run, writes nothing
    snap2 = await ctx.sheets.read(sid, tab)
    if snap2 != snap1:
        return {"ok": False, "paused": True, **report,
                "conflicts": ["the tab changed between reading it and writing (a concurrent edit); nothing written"]}  # fmt: skip
    width = len(snap1[plan.header_row])
    prev_data = sum(1 for r in snap1[plan.header_row + 1:] if any(str(c).strip() for c in r))
    block = [list(r) + [""] * max(0, width - len(r)) for r in plan.new_data_rows]
    while len(block) < prev_data:  # clear any trailing rows the new (shorter) block leaves behind
        block.append([""] * width)
    await ctx.sheets.update(sid, tab, f"A{plan.header_row + 2}", block)
    ctx.audit.record("sheets.reconcile", actor="assistant", target=sid, tab=tab,
                     appended=len(plan.appends), settled=len(plan.settles))  # fmt: skip
    after = await ctx.sheets.read(sid, tab)
    verify = reconcile(after, portal, source_label=source_label, start=start, end=end, column_map=column_map)
    idempotent = not verify.appends and not verify.settles and not verify.conflicts
    report.update(ok=idempotent, written=True, verified_idempotent=idempotent,
                  verified=[f"wrote {len(plan.appends)} new and {len(plan.settles)} settled row(s); "
                            f"re-read leaves nothing to do" if idempotent else "re-read still shows work - check the tab"])  # fmt: skip
    return report


class ReconcileExpenseSheet(Tool):
    name = "reconcile_expense_sheet"
    description = ("Reconcile a card/bank portal's posted and pending charges into an expense sheet: append new "
                   "charges, enter pending ones marked (PENDING), settle a pending row in place when it posts "
                   "(with a before/after diff), leave ambiguous pairs for you, report absent ones, sort by date and "
                   "renumber - Comments never changed. Always dry_run first; a real write is approval-gated with "
                   "read-back. Pass the portal's `posted` and `pending` rows (e.g. from a fetch).")
    schema = {"type": "object", "properties": {
        "spreadsheet": {"type": "string"}, "tab": {"type": "string"},
        "source_label": {"type": "string", "description": "the Card/Bank label written in the sheet"},
        "start": DATEARG, "end": DATEARG,
        "posted": {"type": "array", "items": _TXN}, "pending": {"type": "array", "items": _TXN},
        "dry_run": {"type": "boolean", "description": "default true; false actually writes (approval-gated)"},
    }, "required": ["spreadsheet", "tab", "source_label", "start", "end"]}  # fmt: skip
    effect = "write"

    @staticmethod
    def _dry(args: dict[str, Any]) -> bool:
        return args.get("dry_run", True) is not False

    def approval(self, args: dict[str, Any], call: Call) -> str | None:
        if self._dry(args):
            return None  # a dry run writes nothing, so it needs no card
        return f"write reconciled charges into '{args.get('tab')}' of the expense sheet (append/settle, read back)"

    async def precheck(self, args: dict[str, Any], call: Call) -> str | None:
        if self._dry(args):
            return None
        from friday.tools.google_tools import sheet_write_check

        return await sheet_write_check(spreadsheet_id(str(args["spreadsheet"])), call)

    async def run(self, args: dict[str, Any], call: Call) -> Any:
        portal = _to_portal(args.get("posted") or [], "posted") + _to_portal(args.get("pending") or [], "pending")
        return await apply_reconcile(
            call.ctx, spreadsheet=str(args["spreadsheet"]), tab=str(args["tab"]), portal=portal,
            source_label=str(args["source_label"]), start=date.fromisoformat(args["start"]),
            end=date.fromisoformat(args["end"]), dry_run=self._dry(args),
        )  # fmt: skip


# ---- Cards summary refresh (update cells, posted vs pending kept separate) ------------------------

# label -> (summary key, is it a derived figure?). Order matters: more specific labels first.
_CARD_LABELS: list[tuple[re.Pattern[str], str, bool]] = [
    (re.compile(r"minimum\s*(amount)?\s*due", re.I), "minimum_due", False),
    (re.compile(r"(payment\s*)?due\s*date", re.I), "due_date", False),
    (re.compile(r"available\s*credit", re.I), "available_credit", False),
    (re.compile(r"pending\s*(auth|charge|amount)", re.I), "pending_auths", False),
    (re.compile(r"(posted|statement|current|total)\s*balance", re.I), "posted_balance", False),
    (re.compile(r"(membership\s*reward|mr\s*balance|reward\s*point|point\s*balance)", re.I), "mr_balance", False),
    (re.compile(r"total\s*exposure", re.I), "total_exposure", True),
]


def plan_cards_update(values: list[list[str]], summary: dict[str, Any]) -> list[tuple[int, int, str]]:
    """Cells to overwrite on the Cards tab: find each known label in column A-ish and set the value cell
    to its right. Posted balance and pending auths stay in separate cells; a total-exposure cell is only
    written when `summary` provides it and its label is marked derived. Returns (row, col, value)."""
    updates: list[tuple[int, int, str]] = []
    for r, raw in enumerate(values):
        cells = [str(c) for c in raw]
        label = cells[0].strip() if cells else ""
        if not label:
            continue
        for pat, key, _derived in _CARD_LABELS:
            if pat.search(label) and key in summary and summary[key] is not None:
                vcol = 1 if len(cells) > 1 else 1
                updates.append((r, vcol, safe_text(str(summary[key]))))
                break
    return updates


async def apply_cards_summary(ctx: Any, *, spreadsheet: str, tab: str, summary: dict[str, Any]) -> dict[str, Any]:
    """Update the Cards tab's summary cells in place from a dashboard reading (no append). Read-back verified."""
    sid = spreadsheet_id(spreadsheet)
    before = await ctx.sheets.read(sid, tab)
    updates = plan_cards_update(before, summary)
    if not updates:
        return {"ok": True, "updated": 0, "note": "no matching summary labels found on the Cards tab"}
    col_letter = lambda i: chr(ord("A") + i)  # noqa: E731 (summary cells live in the first columns)
    for r, cidx, val in updates:
        await ctx.sheets.update(sid, tab, f"{col_letter(cidx)}{r + 1}", [[val]])
    ctx.audit.record("sheets.cards_summary", actor="assistant", target=sid, tab=tab, cells=len(updates))
    after = await ctx.sheets.read(sid, tab)
    from friday.sheets import _norm_cell  # compare by value, ignoring the locale's display format

    applied = sum(1 for r, cidx, val in updates
                  if cidx < len(after[r]) and _norm_cell(after[r][cidx]) == _norm_cell(val))  # fmt: skip
    return {"ok": applied == len(updates), "updated": len(updates), "verified_cells": applied,
            "posted_vs_pending_separate": True}  # fmt: skip


TOOLS: list[Tool] = [ReconcileExpenseSheet()]
