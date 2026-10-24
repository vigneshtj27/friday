"""The generic spend gate: caps in code, an approval bound to the exact action, and a ledger.

Spending money is never a keyword call. Every payment is described by a `SpendIntent` - merchant,
exact total read from the page, item summary, delivery address label, payment method label - and goes
through one path:

  1. Owner-set caps (per-order and per-day) are enforced in code. Over a cap is a refusal, not a card.
     The daily cap is a reservation: capacity is checked-and-reserved in one atomic transaction before
     the owner is asked, so two concurrent requests can never both squeeze under it. A reservation, an
     authorised payment, and a failed/uncertain one all keep consuming capacity; only a terminal
     non-payment (denied, voided, or reconciled-as-not-charged) frees it. Time passing frees nothing.
  2. A failed or uncertain earlier payment for the same order is never retried automatically: the
     owner is told to check the order page or email first.
  3. The owner gets a spend card showing the intent. The approval is bound to the intent's
     fingerprint (merchant, total, items, address, method).
  4. Before the payment is committed, the intent currently on the page is compared to the bound one.
     If the total, items, address, merchant or method changed materially, the approval is void and
     the owner is asked again - the old tap never authorises a changed order.
  5. The outcome is written to `spend_ledger`, linked to the approval that authorised it and the task.

Card numbers and CVV are never part of an intent or an argument: payment is by vault injection or the
owner paying in remote view. This module moves no money itself; it decides whether a caller may.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import asdict, dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from friday.util import iso

log = logging.getLogger(__name__)

NO_RETRY_OUTCOMES = ("failed", "uncertain")
# Outcomes that still consume daily capacity: a reservation and a tapped-but-unsettled payment all
# count as money spoken for. Only a terminal non-payment frees it. Time passing frees nothing.
CONSUMING = ("reserved", "authorised", "committed", "failed", "uncertain")
FREED = ("denied", "void", "refused", "reversed")  # reversed = reconciled as not charged


def _money(x: Any) -> Decimal:
    try:
        return Decimal(str(x).replace(",", "").strip())
    except (InvalidOperation, AttributeError, ValueError) as e:
        raise ValueError(f"not a money amount: {x!r}") from e


@dataclass(frozen=True)
class SpendIntent:
    merchant: str
    total: str  # exact, as read from the page
    currency: str
    items: tuple[str, ...] = ()
    address_label: str = ""
    method_label: str = ""
    order_id: str = ""

    def amount(self) -> Decimal:
        return _money(self.total)

    def fingerprint(self) -> dict[str, Any]:
        """The material fields an approval is bound to."""
        return {"merchant": self.merchant.strip().lower(), "total": str(self.amount()),
                "currency": self.currency.upper(), "items": tuple(sorted(i.strip().lower() for i in self.items)),
                "address": self.address_label.strip().lower(), "method": self.method_label.strip().lower()}  # fmt: skip

    def material_change(self, other: SpendIntent) -> list[str]:
        """Which material fields differ from `other` (empty == the same order)."""
        a, b = self.fingerprint(), other.fingerprint()
        return [k for k in a if a[k] != b[k]]

    def card(self) -> dict[str, Any]:
        d = asdict(self)
        d["items"] = list(self.items)
        return d


@dataclass
class SpendCaps:
    per_order: Decimal | None = None
    daily: Decimal | None = None
    currency: str = "INR"

    @classmethod
    def from_prefs(cls, ctx: Any) -> SpendCaps:
        raw = ctx.prefs.get("spend_caps") or {}
        po = raw.get("per_order")
        da = raw.get("daily")
        return cls(per_order=_money(po) if po not in (None, "") else None,
                   daily=_money(da) if da not in (None, "") else None,
                   currency=(raw.get("currency") or ctx.settings.default_currency or "INR").upper())  # fmt: skip

    def static_refusal(self, intent: SpendIntent) -> str | None:
        """Checks that don't depend on today's running total (currency, per-order cap)."""
        if intent.currency.upper() != self.currency and (self.per_order is not None or self.daily is not None):
            return (f"spend caps are set in {self.currency} but this order is in {intent.currency}; "
                    "I can't compare them, so I won't pay. Set a cap in this currency or pay it yourself.")
        if self.per_order is not None and intent.amount() > self.per_order:
            return f"this order ({intent.currency} {intent.amount()}) is over your per-order cap of {self.currency} {self.per_order}."
        return None

    def daily_refusal(self, intent: SpendIntent, spent_today: Decimal) -> str | None:
        if self.daily is not None and spent_today + intent.amount() > self.daily:
            return (f"this order would take today's spend to {self.currency} {spent_today + intent.amount()}, "
                    f"over your daily cap of {self.currency} {self.daily}.")
        return None


@dataclass
class SpendDecision:
    approved: bool
    reason: str
    ledger_id: str | None = None
    changed: list[str] = field(default_factory=list)


class SpendLedger:
    def __init__(self, ctx: Any):
        self.ctx = ctx

    def _insert(self, c: Any, intent: SpendIntent, outcome: str, *, task_id: str | None, run_id: str | None,
                approval_id: str | None, detail: str | None = None) -> str:  # fmt: skip
        """Insert one ledger row on connection `c` (so it can share a caller's transaction)."""
        red = self.ctx.redactor.redact
        sid = secrets.token_hex(8)
        now = iso()
        c.execute(
            "INSERT INTO spend_ledger (id, task_id, run_id, approval_id, merchant, total, currency, items,"
            " address_label, method_label, order_id, outcome, detail, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (sid, task_id, run_id, approval_id, red(intent.merchant)[:200], str(intent.amount()), intent.currency.upper(),
             red(", ".join(intent.items))[:500], red(intent.address_label)[:200], red(intent.method_label)[:120],
             red(intent.order_id)[:120] or None, outcome, red(detail)[:500] if detail else None, now, now),
        )  # fmt: skip
        return sid  # audit is recorded by the caller AFTER the transaction commits (no nested tx)

    def _audit(self, sid: str, intent: SpendIntent, outcome: str) -> None:
        self.ctx.audit.record("spend.ledger", actor="assistant", target=sid, outcome=outcome,
                              merchant=self.ctx.redactor.redact(intent.merchant))  # fmt: skip

    def _record(self, intent: SpendIntent, outcome: str, *, task_id: str | None, run_id: str | None,
                approval_id: str | None, detail: str | None = None) -> str:  # fmt: skip
        with self.ctx.db.tx() as c:
            sid = self._insert(c, intent, outcome, task_id=task_id, run_id=run_id, approval_id=approval_id, detail=detail)
        self._audit(sid, intent, outcome)
        return sid

    def reserve_daily(self, intent: SpendIntent, caps: SpendCaps, *, task_id: str | None, run_id: str | None) -> tuple[bool, str, str | None]:
        """Atomically check the daily cap against everything already consuming capacity today and, if
        there is room, insert a 'reserved' row - all in one transaction, so two concurrent requests
        can never both squeeze under the cap. Returns (ok, reason, ledger_id)."""
        today = date.today().isoformat()
        with self.ctx.db.tx() as c:
            rows = c.execute(
                f"SELECT total FROM spend_ledger WHERE currency = ? AND substr(created_at, 1, 10) = ?"
                f" AND outcome IN ({','.join('?' * len(CONSUMING))})",
                (intent.currency.upper(), today, *CONSUMING),
            ).fetchall()
            spent = sum((_money(r["total"]) for r in rows), Decimal(0))
            reason = caps.daily_refusal(intent, spent)
            outcome = "refused" if reason else "reserved"
            sid = self._insert(c, intent, outcome, task_id=task_id, run_id=run_id, approval_id=None, detail=reason)
        self._audit(sid, intent, outcome)
        return (False, reason, None) if reason else (True, "", sid)

    def set_approval(self, ledger_id: str, approval_id: str) -> None:
        self.ctx.db.execute("UPDATE spend_ledger SET approval_id = ?, updated_at = ? WHERE id = ?",
                            (approval_id, iso(), ledger_id))  # fmt: skip

    def set_outcome(self, ledger_id: str, outcome: str, *, order_id: str | None = None, detail: str | None = None) -> None:
        red = self.ctx.redactor.redact
        self.ctx.db.execute(
            "UPDATE spend_ledger SET outcome = ?, order_id = COALESCE(?, order_id), detail = ?, updated_at = ? WHERE id = ?",
            (outcome, red(order_id)[:120] if order_id else None, red(detail)[:500] if detail else None, iso(), ledger_id),
        )  # fmt: skip
        self.ctx.audit.record("spend.outcome", actor="assistant", target=ledger_id, outcome=outcome)

    def reconcile(self, ledger_id: str, went_through: bool, *, evidence: str, order_id: str | None = None) -> None:
        """Settle an authorised/failed/uncertain payment against the merchant side. If it did NOT go
        through (refunded / never submitted / declined), mark it 'reversed', which frees the capacity
        it held - with the evidence recorded. If it did, mark 'committed'. Only this frees capacity."""
        if not evidence or not evidence.strip():
            raise ValueError("reconciliation needs evidence of what actually happened")
        self.set_outcome(ledger_id, "committed" if went_through else "reversed",
                         order_id=order_id, detail=f"reconciled: {evidence}")  # fmt: skip

    def spent_today(self, currency: str) -> Decimal:
        today = date.today().isoformat()
        rows = self.ctx.db.query(
            f"SELECT total FROM spend_ledger WHERE currency = ? AND substr(created_at, 1, 10) = ?"
            f" AND outcome IN ({','.join('?' * len(CONSUMING))})",
            (currency.upper(), today, *CONSUMING),
        )
        return sum((_money(r["total"]) for r in rows), Decimal(0))

    def blocking_prior(self, intent: SpendIntent) -> dict[str, Any] | None:
        """A failed/uncertain earlier payment for the same order blocks an automatic retry."""
        if not intent.order_id.strip():
            return None
        red = self.ctx.redactor.redact
        row = self.ctx.db.query_one(
            "SELECT * FROM spend_ledger WHERE order_id = ? AND outcome IN ('failed', 'uncertain') ORDER BY created_at DESC LIMIT 1",
            (red(intent.order_id)[:120],),
        )
        return dict(row) if row else None

    def list(self, *, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.ctx.db.query("SELECT * FROM spend_ledger ORDER BY created_at DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]


class SpendGate:
    """Decides whether a payment may proceed, and records the outcome."""

    def __init__(self, ctx: Any):
        self.ctx = ctx
        self.ledger = SpendLedger(ctx)

    @staticmethod
    def describe(intent: SpendIntent) -> str:
        parts = [f"Pay {intent.currency} {intent.amount()} to {intent.merchant}"]
        if intent.items:
            parts.append("for " + ", ".join(intent.items))
        if intent.address_label:
            parts.append("to " + intent.address_label)
        if intent.method_label:
            parts.append("via " + intent.method_label)
        return " ".join(parts)

    async def authorise(self, intent: SpendIntent, *, task_id: str | None = None, run_id: str | None = None,
                        verify: Any = None) -> SpendDecision:  # fmt: skip
        """Caps + no-auto-retry + a bound owner tap, then re-verify the page didn't change. Capacity is
        reserved atomically BEFORE the owner is asked, so two concurrent requests can't both pass a
        daily cap. On an approval that still matches, returns a decision with a ledger id (the row is
        'authorised'); the caller commits and calls `committed` / `failed` / `uncertain`. A refused,
        denied or voided row frees its reservation; an authorised/failed/uncertain one keeps consuming
        capacity until reconciled."""
        caps = SpendCaps.from_prefs(self.ctx)
        static = caps.static_refusal(intent)
        if static:
            self.ledger._record(intent, "refused", task_id=task_id, run_id=run_id, approval_id=None, detail=static)
            return SpendDecision(False, static)
        prior = self.ledger.blocking_prior(intent)
        if prior:
            reason = (f"an earlier payment for order {intent.order_id} ended '{prior['outcome']}'. "
                      "I won't retry it automatically; check the order page or your email first, then tell me to go ahead.")
            self.ledger._record(intent, "refused", task_id=task_id, run_id=run_id, approval_id=None, detail=reason)
            return SpendDecision(False, reason)
        # Reserve capacity atomically before asking the owner (so concurrent requests can't both fit).
        ok, reason, ledger_id = self.ledger.reserve_daily(intent, caps, task_id=task_id, run_id=run_id)
        if not ok:
            return SpendDecision(False, reason)
        action = self.describe(intent)
        details = {"spend": intent.card(), "bound_fingerprint": intent.fingerprint()}
        approved, approval_id = await self.ctx.gate.request_id(profile_id="spend", run_id=run_id, action=action, details=details)
        self.ledger.set_approval(ledger_id, approval_id)  # link the ledger row to the tap that decided it
        if not approved:
            self.ledger.set_outcome(ledger_id, "denied", detail="owner declined")  # frees the reservation
            return SpendDecision(False, "the owner did not approve the payment")
        if verify is not None:
            current = await verify()
            changed = intent.material_change(current) if current is not None else ["gone"]
            if changed:
                detail = f"order changed before submit: {', '.join(changed)}; approval void, asking again"
                self.ledger.set_outcome(ledger_id, "void", detail=detail)  # frees the reservation
                return SpendDecision(False, detail, changed=changed)
        self.ledger.set_outcome(ledger_id, "authorised")
        return SpendDecision(True, "approved", ledger_id=ledger_id)

    def committed(self, ledger_id: str, *, order_id: str | None = None) -> None:
        self.ledger.set_outcome(ledger_id, "committed", order_id=order_id)

    def failed(self, ledger_id: str, detail: str | None = None) -> None:
        self.ledger.set_outcome(ledger_id, "failed", detail=detail)

    def uncertain(self, ledger_id: str, detail: str | None = None) -> None:
        self.ledger.set_outcome(ledger_id, "uncertain", detail=detail)

    def reconcile(self, ledger_id: str, went_through: bool, *, evidence: str, order_id: str | None = None) -> None:
        """Settle an unsettled payment against the merchant side; only this frees held capacity."""
        self.ledger.reconcile(ledger_id, went_through, evidence=evidence, order_id=order_id)
