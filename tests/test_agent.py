from __future__ import annotations

import asyncio
from datetime import date
from decimal import Decimal

import pytest

from friday.agent.approvals import ApprovalGate
from friday.agent.policy import ActionPolicy, PolicyContext, Verdict, form_kind
from friday.capabilities.transactions import Transaction, TransactionStore, parse_amount, parse_date, verify, with_hashes
from friday.sitenotes import SiteNotes
from friday.vault.masking import Redactor

DOMAINS = ("mybank.example", "auth.mybank.example")

AUTH_FORM = {"hasPassword": True, "hasOtp": False, "fieldTypes": ["text", "password", "submit"], "fieldNames": ["user", "password"], "text": "Sign in"}
OTP_FORM = {"hasPassword": False, "hasOtp": True, "fieldTypes": ["text", "submit"], "fieldNames": ["otp"], "text": "Enter code"}
FILTER_FORM = {"hasPassword": False, "hasOtp": False, "fieldTypes": ["date", "date", "select", "submit"], "fieldNames": ["from", "to", "format"], "text": "Statements From To Format"}
TRANSFER_FORM = {"hasPassword": False, "hasOtp": False, "fieldTypes": ["text", "text", "submit"], "fieldNames": ["payee", "amount"], "text": "Payee account Amount"}
SETTINGS_FORM = {"hasPassword": False, "hasOtp": False, "fieldTypes": ["email", "submit"], "fieldNames": ["email address"], "text": "Contact email"}


@pytest.fixture
def policy():
    r = Redactor()
    r.register("mybank/me/password", "Hunter-2-Strong")
    return ActionPolicy(PolicyContext(DOMAINS, r))


def el(name, *, form=None, submit=False, href=None, type_="button", hint=""):
    return {"name": name, "form": form, "isSubmit": submit, "href": href, "type": type_, "fieldHint": hint}


@pytest.mark.parametrize(
    "tool,args,element,verdict",
    [
        ("read_page", {}, None, Verdict.ALLOW),
        ("goto", {"url": "https://www.mybank.example/statements"}, None, Verdict.ALLOW),
        ("goto", {"url": "https://evil.example/collect?x=1"}, None, Verdict.DENY),
        ("goto", {"url": "http://www.mybank.example/"}, None, Verdict.DENY),  # no plain http
        ("goto", {"url": "javascript:alert(1)"}, None, Verdict.DENY),
        ("click", {"ref": "e1"}, el("Sign in", form=AUTH_FORM, submit=True), Verdict.ALLOW),
        ("click", {"ref": "e1"}, el("Verify", form=OTP_FORM, submit=True), Verdict.ALLOW),
        ("click", {"ref": "e1"}, el("Download statement", form=FILTER_FORM, submit=True), Verdict.ALLOW),
        ("click", {"ref": "e1"}, el("Pay now", form=TRANSFER_FORM, submit=True), Verdict.SPEND),
        ("click", {"ref": "e1"}, el("Place order", form=TRANSFER_FORM, submit=True), Verdict.SPEND),
        ("click", {"ref": "e1"}, el("Buy now"), Verdict.SPEND),
        ("click", {"ref": "e1"}, el("Save", form=SETTINGS_FORM, submit=True), Verdict.NEEDS_APPROVAL),
        ("click", {"ref": "e1"}, el("Add beneficiary"), Verdict.NEEDS_APPROVAL),
        ("click", {"ref": "e1"}, el("Cancel subscription"), Verdict.NEEDS_APPROVAL),
        ("click", {"ref": "e1"}, el("I agree to the terms", type_="checkbox"), Verdict.NEEDS_APPROVAL),
        ("click", {"ref": "e1"}, el("Remember this device", form=AUTH_FORM, type_="checkbox"), Verdict.ALLOW),
        ("click", {"ref": "e1"}, el("Statements", href="https://mybank.example/stmts"), Verdict.ALLOW),
        ("click", {"ref": "e1"}, el("Transfer money", href="https://mybank.example/transfer"), Verdict.ALLOW),
        ("click", {"ref": "e1"}, el("Cancel my subscription", href="https://mybank.example/sub/cancel"), Verdict.NEEDS_APPROVAL),
        ("click", {"ref": "e1"}, el("Partner offer", href="https://ads.example/x"), Verdict.DENY),
        ("click", {"ref": "e1"}, el("Accounts"), Verdict.ALLOW),
        ("press", {"key": "Enter"}, el("Amount", form=TRANSFER_FORM, type_="text"), Verdict.NEEDS_APPROVAL),
        ("press", {"key": "Enter"}, el("Password", form=AUTH_FORM, type_="password"), Verdict.ALLOW),
        ("press", {"key": "Tab"}, None, Verdict.ALLOW),
        ("type", {"text": "secret:mybank/me/password"}, el("Password", form=AUTH_FORM, type_="password"), Verdict.ALLOW),
        ("type", {"text": "Hunter-2-Strong"}, el("Password", form=AUTH_FORM, type_="password"), Verdict.DENY),
        ("type", {"text": "my note Hunter-2-Strong"}, el("Search", type_="text", hint="q search"), Verdict.DENY),
        ("type", {"text": "hunter"}, el("Password", form=AUTH_FORM, type_="password"), Verdict.DENY),
        ("type", {"text": "2026-03-01"}, el("From date", form=FILTER_FORM, type_="date"), Verdict.ALLOW),
        ("type", {"text": "123456789"}, el("Payee account", form=TRANSFER_FORM, type_="text", hint="payee"), Verdict.NEEDS_APPROVAL),
        ("type", {"text": "new@example.com"}, el("Contact email", form=SETTINGS_FORM, type_="email", hint="email"), Verdict.NEEDS_APPROVAL),
        ("type", {"text": "rent"}, el("Search transactions", type_="text", hint="search"), Verdict.ALLOW),
        ("click", {"ref": "zz"}, None, Verdict.DENY),
        ("evaluate_js", {}, None, Verdict.DENY),
        ("download_file", {"ref": "e1"}, el("Download PDF"), Verdict.ALLOW),
    ],
)
def test_policy(policy, tool, args, element, verdict):
    assert policy.decide(tool, args, element).verdict == verdict


def test_form_kinds():
    assert form_kind(AUTH_FORM) == "auth"
    assert form_kind(FILTER_FORM) == "filter"
    assert form_kind(TRANSFER_FORM) == "other"
    assert form_kind(None) == "none"
    username_first = {"hasPassword": False, "hasOtp": False, "fieldTypes": ["email", "submit"], "fieldNames": ["email"], "text": "Sign in Email Continue"}
    assert form_kind(username_first) == "auth"


async def test_approval_gate_approve_deny_expire(ctx):
    gate = ApprovalGate(ctx.db, ctx.audit, ctx.redactor)
    notified = []

    async def notify(text, url, *, meta=None):
        notified.append(text)

    gate.notify = notify

    async def decide_soon(approve):
        while not gate.pending():
            await asyncio.sleep(0.01)
        a = gate.pending()[0]
        assert "Pay now" in a.action
        gate.decide(a.id, approve)

    for approve in (True, False):
        t = asyncio.create_task(decide_soon(approve))
        ok = await gate.request(profile_id="p", run_id="r", action='Click "Pay now"', details={})
        await t
        assert ok is approve
    assert await gate.request(profile_id="p", run_id="r", action='Click "Pay now"', details={}, ttl_seconds=0) is False
    statuses = [r["status"] for r in gate.history()]
    assert sorted(statuses) == ["approved", "denied", "expired"]
    assert len(notified) == 3


async def test_approval_details_are_redacted(ctx):
    ctx.vault.put("mybank/me/password", "Very-Secret-99")
    gate = ApprovalGate(ctx.db, ctx.audit, ctx.redactor)
    await gate.request(profile_id="p", run_id=None, action="type Very-Secret-99", details={"form_text": "pw Very-Secret-99"}, ttl_seconds=0)
    row = gate.history()[0]
    assert "Very-Secret-99" not in str(row)


# ---- site notes ----------------------------------------------------------------


def test_site_notes_dedupe_stale_and_redaction(ctx):
    notes = SiteNotes(ctx.db, ctx.redactor)
    ctx.vault.put("mybank/me/password", "Note-Secret-1")
    a = notes.add("mybank", "otp", "OTP page appears after the username step.")
    b = notes.add("mybank", "otp", "OTP page appears after the username step")
    assert a.id == b.id
    notes.add("mybank", "quirk", "password is Note-Secret-1")
    assert "Note-Secret-1" not in notes.render("mybank")
    notes.set_singleton("mybank", "login_url", "Login form at https://mybank.example/login")
    notes.set_singleton("mybank", "login_url", "Login form at https://mybank.example/signin")
    stale = [n for n in notes.list("mybank") if n.kind == "login_url" and n.stale]
    assert len(stale) == 1 and "STALE" in notes.render("mybank")


# ---- transactions ----------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [("1,234.50", (Decimal("1234.50"), None)), ("1,234.50 Dr", (Decimal("1234.50"), "debit")), ("(12.00)", (Decimal("12.00"), "debit")),
     ("-5", (Decimal("5"), "debit")), ("1.234,56", (Decimal("1234.56"), None)), ("300 CR", (Decimal("300"), "credit")), ("", None)],
)
def test_parse_amount(text, expected):
    assert parse_amount(text) == expected


def test_parse_date_dayfirst_and_us():
    assert parse_date("05/03/2026") == date(2026, 3, 5)
    assert parse_date("05/03/2026", dayfirst=False) == date(2026, 5, 3)
    assert parse_date("2026-03-05") == date(2026, 3, 5)
    assert parse_date("5 Mar 2026") == date(2026, 3, 5)


def T(d, amt, direction, bal=None, desc="COFFEE"):
    return Transaction(date.fromisoformat(d), desc, Decimal(amt), "INR", direction, Decimal(bal) if bal else None, "mybank.me", "f:1")


def test_dedupe_keeps_identical_rows_within_a_file_but_not_across_files(ctx):
    store = TransactionStore(ctx.db)
    jan = [T("2026-01-02", "100", "debit"), T("2026-01-02", "100", "debit"), T("2026-01-05", "50", "credit")]
    assert len({h for h, _ in with_hashes(jan)}) == 3  # two real identical coffees
    assert store.add(jan) == (3, 0)
    assert store.add(jan) == (0, 3)  # same statement downloaded again
    assert store.add(jan[:1] + [T("2026-01-09", "70", "debit")]) == (1, 1)


def test_verify_balance_continuity_and_stated_totals():
    good = [T("2026-03-01", "100", "debit", "900"), T("2026-03-02", "50", "credit", "950"), T("2026-03-20", "25", "debit", "925")]
    v = verify(good, start=date(2026, 3, 1), end=date(2026, 3, 31), stated={"total_debits": "125.00", "opening_balance": "1000", "closing_balance": "925"})
    assert v["ok"] and v["balance_continuity"] == 1.0
    bad = [T("2026-03-01", "100", "debit", "900"), T("2026-03-02", "50", "credit", "990"), T("2026-03-20", "25", "debit", "925")]
    assert not verify(bad, start=date(2026, 3, 1), end=date(2026, 3, 31))["ok"]
    wrong_total = verify(good, start=date(2026, 3, 1), end=date(2026, 3, 31), stated={"total_debits": "999"})
    assert not wrong_total["ok"]
    out_of_range = [T("2025-06-01", "1", "debit")] * 5
    assert not verify(out_of_range, start=date(2026, 3, 1), end=date(2026, 3, 31))["ok"]
    assert not verify([], start=date(2026, 3, 1), end=date(2026, 3, 31))["ok"]


def test_date_order_inferred_from_data_not_locale():
    from friday.capabilities.transactions import infer_dayfirst, parse_csv

    assert infer_dayfirst(["05/03/2026", "28/03/2026"], default=False) is True
    assert infer_dayfirst(["03/05/2026", "03/28/2026"], default=True) is False
    assert infer_dayfirst(["01/02/2026"], default=True) is True  # ambiguous -> locale default
    csv_data = b"Date,Narration,Withdrawal,Deposit,Balance\n05/03/2026,A,10.00,,90.00\n28/03/2026,B,,5.00,95.00\n"
    tx = parse_csv(csv_data, source="s", raw_ref="f", currency="INR", dayfirst=False)
    assert [t.date.isoformat() for t in tx] == ["2026-03-05", "2026-03-28"]
