"""Phase 6: file intake (CSV/XLSX/PDF) and one-to-one reconciliation of email hints."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from friday.capabilities.transactions import Transaction
from friday.reconcile import match_one_to_one
from friday.tools.base import Call, ToolError

CSV = b"""Date,Description,Amount
2026-10-01,COFFEE HOUSE,120.00
2026-10-02,BOOK SHOP,540.50
2026-10-02,BOOK SHOP,540.50
"""


def _call(ctx):
    return Call(ctx=ctx, owner_text="import this statement into hdfc.me", plan_id="p1")


def _put(ctx, data, name, mime="text/csv"):
    return ctx.files.put(data, name=name, kind="upload", mime=mime)


# ---- CSV / XLSX intake --------------------------------------------------------------------------


async def test_import_a_csv(ctx):
    f = _put(ctx, CSV, "oct.csv")
    out = await ctx.tools.call("import_transactions_file", {"file": f.id, "account": "hdfc.me"}, _call(ctx))
    assert out["imported"] == 3  # two identical BOOK SHOP rows are kept distinct, not collapsed
    rows = ctx.transactions.list(source="hdfc.me")
    assert len(rows) == 3 and all(r["status"] == "bank" for r in rows)


async def test_import_resolves_a_file_by_name(ctx):
    _put(ctx, CSV, "statement-oct.csv")
    out = await ctx.tools.call("import_transactions_file", {"file": "statement-oct", "account": "hdfc.me"}, _call(ctx))
    assert out["imported"] == 3


async def test_import_an_xlsx(ctx):
    openpyxl = pytest.importorskip("openpyxl")
    from io import BytesIO

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Date", "Description", "Amount"])
    ws.append(["2026-10-03", "GROCERY", "300"])
    buf = BytesIO()
    wb.save(buf)
    f = _put(ctx, buf.getvalue(), "oct.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    out = await ctx.tools.call("import_transactions_file", {"file": f.id, "account": "hdfc.me"}, _call(ctx))
    assert out["imported"] == 1


def _blank_pdf(encrypt: str | None = None) -> bytes:
    from io import BytesIO

    from pypdf import PdfWriter

    w = PdfWriter()
    w.add_blank_page(width=200, height=200)  # a page with no text layer, like a scan
    if encrypt:
        w.encrypt(encrypt)
    buf = BytesIO()
    w.write(buf)
    return buf.getvalue()


async def test_a_password_protected_pdf_asks_for_the_password(ctx):
    f = _put(ctx, _blank_pdf(encrypt="secret"), "locked.pdf", mime="application/pdf")
    with pytest.raises(ToolError, match="password-protected"):
        await ctx.tools.call("import_transactions_file", {"file": f.id, "account": "hdfc.me"}, _call(ctx))


async def test_a_scanned_pdf_is_unsupported(ctx):
    f = _put(ctx, _blank_pdf(), "scan.pdf", mime="application/pdf")  # no text layer
    with pytest.raises(ToolError, match="scanned|no transactions"):
        await ctx.tools.call("import_transactions_file", {"file": f.id, "account": "hdfc.me"}, _call(ctx))


# ---- reconciliation: one-to-one, no collapse ----------------------------------------------------


def test_match_is_one_to_one_and_does_not_collapse():
    email = [{"date": "2026-10-02", "amount": "540.50", "description": "BOOK SHOP", "hash": "e1"},
             {"date": "2026-10-02", "amount": "540.50", "description": "BOOK SHOP", "hash": "e2"}]
    bank = [{"date": "2026-10-02", "amount": "540.50", "description": "BOOK SHOP", "hash": "b1"},
            {"date": "2026-10-02", "amount": "540.50", "description": "BOOK SHOP", "hash": "b2"}]
    res = match_one_to_one(email, bank, window_days=3)
    assert len(res["matched"]) == 2  # each email matched a DISTINCT bank row
    assert {m["bank"]["hash"] for m in res["matched"]} == {"b1", "b2"}
    assert res["unmatched_email"] == [] and res["unmatched_bank"] == []


def test_one_email_two_identical_bank_rows_matches_only_one():
    email = [{"date": "2026-10-02", "amount": "540.50", "description": "BOOK SHOP", "hash": "e1"}]
    bank = [{"date": "2026-10-02", "amount": "540.50", "description": "BOOK SHOP", "hash": "b1"},
            {"date": "2026-10-02", "amount": "540.50", "description": "BOOK SHOP", "hash": "b2"}]
    res = match_one_to_one(email, bank)
    assert len(res["matched"]) == 1 and len(res["unmatched_bank"]) == 1


def test_match_respects_the_date_window_and_merchant():
    email = [{"date": "2026-10-01", "amount": "100", "description": "CAFE MOCHA", "hash": "e1"}]
    far = [{"date": "2026-10-20", "amount": "100", "description": "CAFE MOCHA", "hash": "b1"}]
    assert match_one_to_one(email, far, window_days=3)["matched"] == []
    wrong_merchant = [{"date": "2026-10-02", "amount": "100", "description": "PETROL PUMP", "hash": "b1"}]
    assert match_one_to_one(email, wrong_merchant, window_days=3)["matched"] == []


async def test_reconcile_through_the_store_marks_confirmed(ctx):
    # an email hint, then a bank import a day later within the window -> confirmed, not duplicated
    ctx.transactions.add_evidence(
        [Transaction(date(2026, 10, 5), "NETFLIX", Decimal(499), "INR", "debit", None, "amex.me", "mail1")],
        evidence_refs=["gmail:mail1"],
    )
    assert ctx.transactions.list(status="email")
    ctx.transactions.add([Transaction(date(2026, 10, 6), "NETFLIX SUBSCRIPTION", Decimal(499), "INR", "debit", None, "amex.me", "bank1")])
    report = ctx.transactions.reconcile(window_days=3)
    assert report["confirmed"] == 1
    assert not ctx.transactions.list(status="email")  # the hint is now confirmed
    assert "hint" in report["note"]
