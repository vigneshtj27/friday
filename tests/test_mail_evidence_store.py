from __future__ import annotations

from datetime import date
from decimal import Decimal

from friday.capabilities.mail_evidence import EvidenceItem, to_transaction
from friday.capabilities.transactions import Transaction


def ev(merchant="AMAZON", amount="4599.00", d=date(2026, 10, 20), direction="debit", mid="sk1", kind="otp"):
    return EvidenceItem(merchant, Decimal(amount), "INR", direction, d, kind, "note", mid)


def store_evidence(ctx, items, source="gmail:amex"):
    txns = [to_transaction(i, source=source, fallback_date=date(2026, 10, 1)) for i in items]
    return ctx.transactions.add_evidence(txns, evidence_refs=[i.message_id for i in items])


def bank_txn(desc="AMAZON PAYMENTS", amount="4599.00", d=date(2026, 10, 20)):
    return Transaction(d, desc, Decimal(amount), "INR", "debit", None, "amex.me", f"bank:{desc}")


def test_evidence_stored_with_status_and_ref(ctx):
    res = store_evidence(ctx, [ev()])
    assert res["new"] == 1
    rows = ctx.transactions.list(status="email")
    assert len(rows) == 1 and rows[0]["status"] == "email" and rows[0]["evidence_ref"] == "sk1"
    assert rows[0]["source"] == "gmail:amex"


def test_same_email_scanned_twice_is_deduped(ctx):
    assert store_evidence(ctx, [ev()])["new"] == 1
    second = store_evidence(ctx, [ev()])
    assert second["new"] == 0 and second["already_known"] == 1
    assert len(ctx.transactions.list(status="email")) == 1


def test_email_row_skipped_when_bank_confirmed_exists(ctx):
    ctx.transactions.add([bank_txn()])  # a confirmed charge already present
    res = store_evidence(ctx, [ev()])  # same date + amount, overlapping merchant
    assert res["new"] == 0 and res["already_confirmed"] == 1
    assert ctx.transactions.list(status="email") == []


def test_reconciliation_when_bank_row_arrives_later(ctx):
    assert store_evidence(ctx, [ev()])["new"] == 1
    assert ctx.transactions.list(status="email")
    ctx.transactions.add([bank_txn()])  # the real charge lands later
    assert ctx.transactions.list(status="email") == []  # the email row was reconciled
    confirmed = ctx.transactions.list(status="confirmed")
    assert len(confirmed) == 1 and confirmed[0]["evidence_ref"] == "sk1"


def test_unrelated_bank_row_does_not_reconcile(ctx):
    store_evidence(ctx, [ev()])
    ctx.transactions.add([bank_txn(desc="STARBUCKS", amount="300.00", d=date(2026, 10, 19))])
    assert len(ctx.transactions.list(status="email")) == 1  # different charge, untouched


def test_existing_rows_default_to_bank_status(ctx):
    ctx.transactions.add([bank_txn(desc="UBER", amount="250.00")])
    rows = ctx.transactions.list(source="amex.me")
    assert rows and all(r["status"] == "bank" for r in rows)
