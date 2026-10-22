"""End-to-end (login-free): the owner asks for Amex expenses from email -> scan -> approval card ->
PENDING rows in the sheet -> the reply says they are unverified. Real chat, planner, tool registry
and Sheets writer; fake Google and a scripted planner (no API key, no browser)."""

from __future__ import annotations

import asyncio

import pytest

from friday.profiles.models import Site

from .fake_google import SID, FakeGoogle, install
from .planner_fakes import PlanLLM, attach, chk, last_friday, plan, settle, step
from .test_mail_evidence_tools import ALERT, MARKETING, SAFEKEY, approve_next

HEADER = ["Date", "Description", "Amount", "Status"]


async def test_amex_expenses_from_email(ctx):
    ctx.sites.upsert(Site(id="amex", name="American Express", login_url="https://americanexpress.com/",
                          domains=["americanexpress.com"], otp_senders=["AmericanExpress@welcome.americanexpress.com"]))  # fmt: skip
    ctx.profiles.create("amex", "me")
    fake = FakeGoogle(sheets={SID: {"Oct": [list(HEADER)]}}, titles={SID: "Expenses 2026"}, mail=[SAFEKEY, ALERT, MARKETING])
    install(ctx, fake)
    ctx.prefs.set("expense_sheet", {"spreadsheet": SID, "tab": "Oct", "include_credits": False, "column_map": None})

    attach(ctx, PlanLLM([plan(
        step("gmail_scan_expenses", {"account": "amex.me", "start": "2026-10-01", "end": "2026-10-31"},
             checks=[chk("new", "gt", 0, says="email evidence was found and stored")], progress="Scanning Gmail for evidence"),
        step("sheets_add_evidence", {"account": "amex.me", "start": "2026-10-01", "end": "2026-10-31"},
             checks=[chk("ok", "true", says="the PENDING rows were read back")], progress="Adding PENDING rows"),
    )], reports=["I added 2 email-evidence charges for American Express to the Oct tab, marked PENDING. "
                 "These are from emails, not a statement, so they are unverified until a bank fetch confirms them."]))  # fmt: skip

    await ctx.chat.owner_says("Get my Amex expenses from email for Oct and put them in the sheet.")
    await approve_next(ctx, True)  # the deliberate approval for adding unverified rows
    await settle(ctx)

    rows = fake.sheets[SID]["Oct"]
    assert rows[0] == HEADER
    assert len(rows) == 3  # two evidence charges appended
    assert all(row[3] == "PENDING" for row in rows[1:])
    msg = last_friday(ctx).lower()
    assert "pending" in msg or "unverified" in msg or "evidence" in msg
    # the approval really happened, and no mail was ever written/sent
    assert ctx.gate.history() and fake.sent == [] and fake.drafts == {}
    assert not any("/drafts" in p or "/send" in p for _, p in fake.calls)
    # evidence rows are marked email-status in the store
    assert len(ctx.transactions.list(status="email")) == 2


async def test_denied_approval_adds_nothing(ctx):
    ctx.sites.upsert(Site(id="amex", name="Amex", login_url="https://americanexpress.com/", domains=["americanexpress.com"]))
    ctx.profiles.create("amex", "me")
    fake = FakeGoogle(sheets={SID: {"Oct": [list(HEADER)]}}, titles={SID: "E"}, mail=[SAFEKEY])
    install(ctx, fake)
    ctx.prefs.set("expense_sheet", {"spreadsheet": SID, "tab": "Oct", "include_credits": False, "column_map": None})
    attach(ctx, PlanLLM([plan(
        step("gmail_scan_expenses", {"account": "amex.me", "start": "2026-10-01", "end": "2026-10-31"}),
        step("sheets_add_evidence", {"account": "amex.me", "start": "2026-10-01", "end": "2026-10-31"}),
    )], reports=["You declined, so nothing was added to the sheet."]))
    await ctx.chat.owner_says("Add my Amex email expenses for Oct.")
    await approve_next(ctx, False)
    await settle(ctx)
    assert fake.sheets[SID]["Oct"] == [HEADER]  # nothing appended
