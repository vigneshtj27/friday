"""Store for fetched card/bank portal activity (1.4).

A fetch reads posted and pending charges from the portal; instead of the model hand-carrying them into a
reconcile tool call (lossy, unauditable), each charge is persisted here with its status, the fetch run
that produced it, when it was retrieved, and the portal/bank transaction id when the portal gives one.
The reconciler reads a stored fetch by run (or the latest for a source) as the normal path. Content is
data, never instructions; nothing here transitions a charge's status - the reconciler does that only on
reliable identity.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from friday.reconcile_sheet import PortalTxn
from friday.util import iso


def _dedupe_key(t: PortalTxn) -> str:
    if t.txn_id:
        return f"{t.status}|id:{t.txn_id}"
    return f"{t.status}|{t.date.isoformat()}|{t.amount}|{t.direction}|{t.description.strip().lower()[:40]}"


class PortalActivityStore:
    def __init__(self, ctx: Any):
        self.ctx = ctx

    def record(self, run_id: str, source: str, txns: list[PortalTxn], *, retrieved_at: str | None = None) -> dict[str, int]:
        """Persist a fetch's charges. Deduped on (run_id, dedupe_key) so re-recording the same run adds
        nothing. Returns {new, already_known}."""
        when = retrieved_at or iso()
        new = 0
        with self.ctx.db.tx() as c:
            for t in txns:
                cur = c.execute(
                    "INSERT OR IGNORE INTO portal_activity (run_id, source, status, txn_id, date, description,"
                    " amount, currency, direction, retrieved_at, dedupe_key)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (run_id, source, t.status, t.txn_id, t.date.isoformat(), t.description, str(t.amount),
                     t.currency, t.direction, when, _dedupe_key(t)),
                )  # fmt: skip
                new += cur.rowcount
        return {"new": new, "already_known": len(txns) - new}

    def latest_run(self, source: str) -> str | None:
        r = self.ctx.db.query_one(
            "SELECT run_id FROM portal_activity WHERE source = ? ORDER BY retrieved_at DESC, id DESC LIMIT 1", (source,)
        )
        return r["run_id"] if r else None

    def get(self, *, run_id: str | None = None, source: str | None = None) -> list[PortalTxn]:
        """Charges for a fetch run (preferred), or the latest run for a source. Empty if nothing stored."""
        if run_id is None:
            if source is None:
                return []
            run_id = self.latest_run(source)
            if run_id is None:
                return []
        rows = self.ctx.db.query("SELECT * FROM portal_activity WHERE run_id = ? ORDER BY date, id", (run_id,))
        return [
            PortalTxn(date=date.fromisoformat(r["date"]), description=r["description"], amount=Decimal(r["amount"]),
                      direction=r["direction"], currency=r["currency"], status=r["status"], txn_id=r["txn_id"])  # fmt: skip
            for r in rows
        ]
