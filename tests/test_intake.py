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


# ---- the chat upload endpoint (Part 5) ----------------------------------------------------------


def test_upload_endpoint_stores_a_file_for_import(ctx):
    from fastapi.testclient import TestClient

    from friday.web.app import create_app
    from tests.conftest import TEST_PASSWORD

    with TestClient(create_app(ctx), base_url="http://testserver") as c:
        c.post("/login", data={"password": TEST_PASSWORD}, follow_redirects=False)
        r = c.post("/api/upload", files={"file": ("oct.csv", CSV, "text/csv")})
        assert r.status_code == 200
        fid = r.json()["id"]
        # a non-statement type is refused, and oversize is refused
        assert c.post("/api/upload", files={"file": ("notes.txt", b"hi", "text/plain")}).status_code == 415
        assert c.post("/api/upload", files={"file": ("big.csv", b"x" * 8_000_001, "text/csv")}).status_code == 413
    # the stored file is importable by id
    assert ctx.files.meta(fid) is not None and ctx.files.meta(fid).kind == "upload"


# ---- delete_file: owner uploads only, approval-carded, audited (Part 1.3) ------------------------


async def test_delete_uploaded_file_after_the_tap(ctx):
    import asyncio

    from .test_tools import approve_next

    f = _put(ctx, CSV, "friday-test-upload.csv")
    call = Call(ctx=ctx, owner_text="delete that test file I uploaded", plan_id="p1")
    task = asyncio.create_task(ctx.tools.call("delete_file", {"file": "friday-test-upload"}, call))
    await approve_next(ctx, True)  # deleting always needs the owner's tap
    out = await task
    assert out["deleted"] == "friday-test-upload.csv"
    assert ctx.files.meta(f.id) is None  # blob + row gone
    assert ctx.audit.list(action_prefix="file.delete")  # the deletion is recorded in the audit log


async def test_delete_refuses_a_file_the_owner_did_not_upload(ctx):
    # a downloaded statement is not an owner upload: refused outright, no approval card, file kept
    f = ctx.files.put(CSV, name="downloaded-statement.csv", kind="download", mime="text/csv")
    call = Call(ctx=ctx, owner_text="delete the downloaded statement", plan_id="p1")
    with pytest.raises(ToolError, match="only delete files you uploaded"):
        await ctx.tools.call("delete_file", {"file": "downloaded-statement"}, call)
    assert ctx.gate.pending() == [] and ctx.files.meta(f.id) is not None


async def test_delete_denied_keeps_the_file(ctx):
    import asyncio

    from friday.tools.base import ApprovalDenied

    from .test_tools import approve_next

    f = _put(ctx, CSV, "keep-me.csv")
    call = Call(ctx=ctx, owner_text="delete keep-me", plan_id="p1")
    task = asyncio.create_task(ctx.tools.call("delete_file", {"file": "keep-me"}, call))
    await approve_next(ctx, False)
    with pytest.raises(ApprovalDenied):
        await task
    assert ctx.files.meta(f.id) is not None  # a denied tap leaves the file in place


def test_chat_page_has_a_wired_upload_control(ctx):
    # the live defect was that the upload endpoint worked but the chat UI had no control reaching it
    from fastapi.testclient import TestClient

    from friday.web.app import create_app
    from tests.conftest import TEST_PASSWORD

    with TestClient(create_app(ctx), base_url="http://testserver") as c:
        c.post("/login", data={"password": TEST_PASSWORD}, follow_redirects=False)
        page = c.get("/chat").text
        assert 'id="attach"' in page and 'id="file"' in page  # the control exists on the page
        assert 'accept=".csv,.xlsx,.xlsm,.pdf"' in page  # limited to statement types
        js = c.get("/static/chat.js").text
        assert "/api/upload" in js and "fileInput.onchange" in js  # and it is wired to the endpoint


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
