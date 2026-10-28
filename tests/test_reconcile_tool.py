"""Part 1.3: the reconcile executor/tool and Cards-summary update against the fake Google (no live site).

Proves the write path the append-only writer lacked: dry-run first, approval-gated write with snapshot
conflict detection and read-back, settle-in-place with Comments preserved, and no payment navigation.
"""

from __future__ import annotations

import asyncio

import pytest

from friday.reconcile_sheet import PENDING_MARK
from friday.tools.base import Call
from friday.tools.reconcile_tools import apply_cards_summary, apply_reconcile

from .fake_google import SID, FakeGoogle, install
from .test_tools import approve_next

HEADER = ["No", "Date", "Narration", "", "", "Debit", "Credit", "Comments", "Bank", "Txn ID"]
START, END = "2026-10-01", "2026-10-31"


def _row(no, d, narr, debit="", credit="", comment="", bank="Amex", txnid=""):
    return [str(no), d, narr, "", "", str(debit) if debit else "", str(credit) if credit else "", comment, bank, txnid]


@pytest.fixture
def g(ctx):
    sheet = [list(HEADER),
             _row(1, "05/10/2026", "Blue Mart groceries", debit="500", comment="food", txnid="BM1"),
             _row(2, "10/10/2026", f"Hotel Zurich {PENDING_MARK}", debit="730.85", comment="trip", txnid="HZ9")]
    fake = FakeGoogle(sheets={SID: {"Expenses": sheet}}, titles={SID: "Expense Tracker"})
    install(ctx, fake)
    return ctx, fake


def _portal():
    # real portals supply transaction ids; reliable identity makes re-runs idempotent and settles robust
    return (
        [{"date": "2026-10-09", "narration": "Kettle Co", "amount": "300", "txn_id": "K1"},           # new posted
         {"date": "2026-10-12", "narration": "Hotel Zurich", "amount": "745.20", "txn_id": "HZ9"}],   # posts the pending (id + FX)
        [{"date": "2026-10-15", "narration": "Cafe Mocha", "amount": "222", "txn_id": "CM1"}],        # new pending
    )


async def _call(ctx, **kw):
    posted, pending = _portal()
    from datetime import date

    from friday.tools.reconcile_tools import _to_portal
    portal = _to_portal(posted, "posted") + _to_portal(pending, "pending")
    return await apply_reconcile(ctx, spreadsheet=SID, tab="Expenses", portal=portal, source_label="Amex",
                                 start=date(2026, 10, 1), end=date(2026, 10, 31), **kw)  # fmt: skip


async def test_dry_run_writes_nothing_but_reports_the_plan(g):
    ctx, fake = g
    before = [list(r) for r in fake.sheets[SID]["Expenses"]]
    out = await _call(ctx, dry_run=True)
    assert out["ok"] and out["dry_run"] and out["append"] == 2 and out["settle"] == 1
    assert fake.sheets[SID]["Expenses"] == before  # nothing written


async def test_write_appends_settles_in_place_and_reads_back(g):
    ctx, fake = g
    out = await _call(ctx, dry_run=False)
    assert out["ok"] and out["written"] and out["verified_idempotent"]
    rows = [r for r in fake.sheets[SID]["Expenses"][1:] if any(c.strip() for c in r)]
    # 2 original + 2 appended, the pending one settled in place (not doubled) -> 4 data rows
    assert len(rows) == 4
    narrs = [r[2] for r in rows]
    assert not any(PENDING_MARK in n for n in narrs if "Hotel" in n)  # Hotel Zurich settled
    assert sum(1 for n in narrs if "Hotel Zurich" in n) == 1  # no double count
    assert any(PENDING_MARK in n for n in narrs if "Cafe" in n)  # Cafe Mocha entered pending
    # sorted ascending by date, No renumbered, Comments preserved with their rows
    # (the fake sheet renders a written number with 2dp, like an en-IN locale; compare by value)
    assert [int(float(r[0])) for r in rows] == [1, 2, 3, 4]
    assert [r[2].split(" (PEND")[0] for r in rows] == ["Blue Mart groceries", "Kettle Co", "Hotel Zurich", "Cafe Mocha"]
    hotel = next(r for r in rows if "Hotel Zurich" in r[2])
    assert hotel[7] == "trip" and hotel[5] == "745.20"  # comment kept, amount corrected to the FX total


async def test_concurrent_edit_pauses_and_writes_nothing(g):
    ctx, fake = g
    orig = ctx.sheets.read
    n = {"c": 0}

    async def flaky(sid, tab):
        n["c"] += 1
        if n["c"] == 2:  # someone edits the tab between our snapshot and the write
            fake.sheets[SID]["Expenses"].append(_row(9, "18/10/2026", "Concurrent edit", debit="42"))
        return await orig(sid, tab)

    ctx.sheets.read = flaky
    before_rows = len([r for r in fake.sheets[SID]["Expenses"][1:] if any(c.strip() for c in r)])
    out = await _call(ctx, dry_run=False)
    assert out["paused"] and not out.get("written") and out["conflicts"]
    # only the concurrent row was added; the reconcile wrote nothing of its own
    assert len([r for r in fake.sheets[SID]["Expenses"][1:] if any(c.strip() for c in r)]) == before_rows + 1


async def test_tool_dry_run_needs_no_card_then_write_is_carded(g):
    ctx, fake = g
    posted, pending = _portal()
    call = Call(ctx=ctx, owner_text="refresh my amex expenses", plan_id="p1")
    args = {"spreadsheet": SID, "tab": "Expenses", "source_label": "Amex", "start": START, "end": END,
            "posted": posted, "pending": pending}  # fmt: skip
    dry = await ctx.tools.call("reconcile_expense_sheet", {**args, "dry_run": True}, call)
    assert dry["dry_run"] and ctx.gate.pending() == []  # a dry run shows no approval card

    task = asyncio.create_task(ctx.tools.call("reconcile_expense_sheet", {**args, "dry_run": False}, call))
    await approve_next(ctx, True)  # the real write asks for a tap
    out = await task
    assert out["written"] and out["ok"]


async def test_reconcile_makes_no_payment_navigation(g):
    ctx, fake = g
    await _call(ctx, dry_run=False)
    # the reconciler only talks to Sheets - never a bank page or a payment/dispute/settings control
    assert all(path.startswith("/v4/spreadsheets") or path.startswith("/drive") for _, path in fake.calls)


# ---- Cards summary refresh ----------------------------------------------------------------------


async def test_reconcile_reads_a_stored_fetch_not_model_supplied_rows(ctx):
    from datetime import date as D
    from decimal import Decimal

    from friday.reconcile_sheet import PortalTxn
    from friday.tools.base import ToolError

    sheet = [list(HEADER), _row(1, "05/10/2026", "Blue Mart groceries", debit="500", txnid="BM1")]
    fake = FakeGoogle(sheets={SID: {"Expenses": sheet}}, titles={SID: "T"})
    install(ctx, fake)
    txns = [PortalTxn(D(2026, 10, 9), "Kettle Co", Decimal("300"), status="posted", txn_id="K1"),
            PortalTxn(D(2026, 10, 15), "Cafe Mocha", Decimal("222"), status="pending", txn_id="CM1")]
    assert ctx.portal_activity.record("fetch-1", "amex.me", txns)["new"] == 2
    assert ctx.portal_activity.record("fetch-1", "amex.me", txns)["new"] == 0  # re-record is deduped

    call = Call(ctx=ctx, owner_text="reconcile amex", plan_id="p1")
    args = {"spreadsheet": SID, "tab": "Expenses", "source_label": "Amex", "account": "amex.me",
            "start": START, "end": END, "dry_run": True}  # fmt: skip
    out = await ctx.tools.call("reconcile_expense_sheet", args, call)
    assert out["activity_source"] == "stored_fetch" and out["append"] == 2  # read from the store, not args
    # a source with no stored fetch fails with a clear, honest error (never silently empty)
    with pytest.raises(ToolError, match="no stored portal activity"):
        await ctx.tools.call("reconcile_expense_sheet", {**args, "account": "nope.me"}, call)


async def test_write_range_with_a_formula_pauses_and_writes_nothing(ctx):
    # 1.6: a data cell is a formula; rewriting the block from displayed values would destroy it -> pause
    sheet = [list(HEADER),
             _row(1, "05/10/2026", "Blue Mart", debit="500", txnid="BM1"),
             _row(2, "10/10/2026", "Running total", debit="=SUM(F2:F2)")]  # a formula cell in the range
    fake = FakeGoogle(sheets={SID: {"Expenses": sheet}}, titles={SID: "T"})
    install(ctx, fake)
    before = [list(r) for r in fake.sheets[SID]["Expenses"]]
    out = await _call(ctx, dry_run=False)
    assert out["paused"] and not out.get("written")
    assert any("formula" in c for c in out["conflicts"]) and "F3" in " ".join(out["conflicts"])
    assert fake.sheets[SID]["Expenses"] == before  # nothing written


def test_derived_figure_only_goes_into_a_derived_labelled_cell():
    from friday.tools.reconcile_tools import plan_cards_update

    summary = {"total_exposure": "827751.38", "posted_balance": "827020.53"}
    # label NOT marked derived -> total exposure is refused; posted balance still written
    plain = plan_cards_update([["Posted balance", "0"], ["Total exposure", "0"]], summary)
    keys = {r for r, _, _ in plain}
    assert 0 in keys and 1 not in keys  # posted written (row 0), total-exposure cell (row 1) refused
    # label explicitly marked derived -> written
    marked = plan_cards_update([["Total exposure (derived)", "0"]], summary)
    assert len(marked) == 1 and marked[0][2] == "827751.38"


async def test_cards_summary_updates_cells_posted_and_pending_separate(ctx):
    cards = [["Field", "Value"],
             ["Posted balance", "0"], ["Pending auths", "0"], ["Available credit", "0"],
             ["Payment due date", ""], ["Minimum due", "0"], ["MR balance", "0"],
             ["Total exposure (derived)", "0"]]
    fake = FakeGoogle(sheets={SID: {"Cards": cards}}, titles={SID: "Tracker"})
    install(ctx, fake)
    summary = {"posted_balance": "827020.53", "pending_auths": "730.85", "available_credit": "0",
               "due_date": "2026-11-17", "minimum_due": "71883", "mr_balance": "48640",
               "total_exposure": "827751.38"}  # fmt: skip
    out = await apply_cards_summary(ctx, spreadsheet=SID, tab="Cards", summary=summary)
    assert out["ok"] and out["updated"] == 7 and out["posted_vs_pending_separate"]
    grid = {r[0]: r[1] for r in fake.sheets[SID]["Cards"][1:]}
    assert grid["Posted balance"] == "827,020.53" and grid["Pending auths"] == "730.85"  # kept separate
    assert grid["Total exposure (derived)"] == "827,751.38"  # derived, explicitly labelled on the tab
