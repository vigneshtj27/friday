"""Activity fetch: tables read from an activity page become verified, deduped transactions."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from friday.capabilities.activity import grounded_rows, infer_columns, parse_activity_date, parse_tables

START, END = date(2026, 10, 1), date(2026, 10, 20)

# A card's activity page: an ARIA grid (posted) and a plain table (pending), year-less dates,
# a refund shown with a minus sign and a payment marked CR.
FIXTURE = """<!doctype html><html><body>
<h2>Recent activity</h2>
<div role="grid" aria-label="Posted transactions">
  <div role="row"><span role="columnheader">Date</span><span role="columnheader">Description</span><span role="columnheader">Amount</span></div>
  <div role="row"><span role="gridcell">Oct 18</span><span role="gridcell">SWIGGY BANGALORE</span><span role="gridcell">₹642.00</span></div>
  <div role="row"><span role="gridcell">Oct 12</span><span role="gridcell">AMAZON PAY INDIA</span><span role="gridcell">₹1,299.00</span></div>
  <div role="row"><span role="gridcell">Oct 10</span><span role="gridcell">AMAZON REFUND</span><span role="gridcell">-₹199.00</span></div>
  <div role="row"><span role="gridcell">Oct 05</span><span role="gridcell">PAYMENT RECEIVED - THANK YOU</span><span role="gridcell">₹45,210.55 CR</span></div>
  <div role="row"><span role="gridcell">Sep 28</span><span role="gridcell">UBER INDIA</span><span role="gridcell">₹310.00</span></div>
</div>
<h3>Pending</h3>
<table><tr><td>19 Oct</td><td>BLINKIT</td><td>REF 88231907</td><td>₹415.50</td></tr></table>
</body></html>"""

GRID_ROWS = [
    ["Date", "Description", "Amount"],
    ["Oct 18", "SWIGGY BANGALORE", "₹642.00"],
    ["Oct 12", "AMAZON PAY INDIA", "₹1,299.00"],
    ["Oct 10", "AMAZON REFUND", "-₹199.00"],
    ["Oct 05", "PAYMENT RECEIVED - THANK YOU", "₹45,210.55 CR"],
    ["Sep 28", "UBER INDIA", "₹310.00"],
]
PENDING_ROWS = [["19 Oct", "BLINKIT", "REF 88231907", "₹415.50"]]


def _parse(tables):
    return parse_tables(tables, start=START, end=END, source="amex.me", currency="INR", card=True, run_id="r1")


def test_yearless_dates_land_in_the_range():
    assert parse_activity_date("Oct 18", start=START, end=END) == date(2026, 10, 18)
    assert parse_activity_date("Sat, 19 Oct", start=START, end=END) == date(2026, 10, 19)
    assert parse_activity_date("Dec 30", start=START, end=END) == date(2025, 12, 30)  # not in the future
    assert parse_activity_date("2026-10-03", start=START, end=END) == date(2026, 10, 3)


def test_card_grid_and_pending_table():
    txns, report = _parse([{"url": "u", "rows": GRID_ROWS}, {"url": "u", "rows": PENDING_ROWS}])
    by = {t.description: t for t in txns}
    assert by["SWIGGY BANGALORE"].direction == "debit" and by["SWIGGY BANGALORE"].amount == Decimal("642.00")
    assert by["AMAZON REFUND"].direction == "credit"  # minus on a card = money back
    assert by["PAYMENT RECEIVED - THANK YOU"].direction == "credit"
    assert by["BLINKIT"].date == date(2026, 10, 19) and by["BLINKIT"].amount == Decimal("415.50")  # not the REF number
    assert [r.get("how") for r in report] == ["header", "inferred columns"]


def test_reextracted_table_counts_once():
    txns, _ = _parse([{"url": "u", "rows": GRID_ROWS}, {"url": "u", "rows": GRID_ROWS}])
    assert len(txns) == 5


def test_non_transaction_table_is_ignored():
    assert infer_columns([["Rewards", "Points"], ["Membership", "12,400"]], start=START, end=END) is None


def test_reported_rows_must_be_on_the_page():
    page = "Recent activity\nOct 12 AMAZON PAY INDIA ₹1,299.00\nOct 18 SWIGGY BANGALORE ₹642.00"
    reported = [
        {"date": "Oct 12", "description": "AMAZON PAY INDIA", "amount": "1299.00", "direction": "debit"},
        {"date": "Oct 18", "description": "SWIGGY BANGALORE", "amount": "642", "direction": "debit"},
        {"date": "Oct 14", "description": "MADE UP STORE", "amount": "999.00", "direction": "debit"},
        {"date": "Oct 15", "description": "AMAZON PAY INDIA", "amount": "5000.00", "direction": "debit"},
    ]
    kept, dropped = grounded_rows(reported, [page], start=START, end=END, source="amex.me", currency="INR", run_id="r1")
    assert [t.description for t in kept] == ["AMAZON PAY INDIA", "SWIGGY BANGALORE"] and dropped == 2


async def test_capability_stores_verified_rows(ctx):
    """FetchActivity.run with a stubbed agent loop: parse, filter to range, verify, dedupe into the store."""
    from types import SimpleNamespace

    import friday.capabilities.activity as act
    from friday.agent.loop import AgentOutcome
    from friday.profiles.models import ProfileConfig, Site

    ctx.sites.upsert(Site(id="amex", name="American Express", login_url="https://www.americanexpress.com/", domains=["americanexpress.com"]))
    profile = ctx.profiles.create("amex", "me", ProfileConfig(locale="en-IN"))

    class Loop:
        def __init__(self, *a):
            pass

        async def run(self, task):
            assert "2026-10-01" in task.goal and "extract_table" in task.goal
            return AgentOutcome("success", "Extracted the activity tables.")

    ex = SimpleNamespace(tables=[{"url": "u", "rows": GRID_ROWS}, {"url": "u", "rows": PENDING_ROWS}], page_texts=[])
    op = SimpleNamespace(profile=profile, site=ctx.sites.get("amex"))
    orig = act.AgentLoop
    act.AgentLoop = Loop
    try:
        params = act.FetchActivity.validate({"start": "2026-10-01", "end": "2026-10-20"})
        res = await act.FetchActivity(ctx, None).run(op, ex, "r1", params)
        again = await act.FetchActivity(ctx, None).run(op, ex, "r2", params)
    finally:
        act.AgentLoop = orig
    assert res.status == "succeeded", res.summary
    assert res.result["outside_range"] == 1  # Sep 28 left out
    assert res.result["stored"] == {"new": 5, "duplicates": 0}
    assert res.result["totals"]["debits"] == "2356.50"
    assert again.result["stored"] == {"new": 0, "duplicates": 5}
    assert len(ctx.transactions.list(source="amex.me")) == 5


@pytest.mark.chrome
async def test_table_js_reads_aria_grid_and_table_from_fixture_html():
    """The extract_table JavaScript against the fixture page in a real browser, then the parser."""
    import os

    from playwright.async_api import async_playwright

    from friday.agent.tools import TABLE_JS

    exe = os.environ.get("FRIDAY_CHROME_EXECUTABLE")
    async with async_playwright() as pw:
        b = await pw.chromium.launch(executable_path=exe or None, channel=None if exe else "chrome",
                                     args=["--no-sandbox"] if os.environ.get("FRIDAY_CHROME_SANDBOX") == "false" else [])
        page = await b.new_page()
        await page.set_content(FIXTURE)
        grid = await page.evaluate(TABLE_JS, [None, 0])
        pending = await page.evaluate(TABLE_JS, [None, 1])
        await b.close()
    assert grid["rows"] == GRID_ROWS and grid["total"] == 6
    assert pending["rows"] == PENDING_ROWS
    txns, _ = _parse([{"url": "u", "rows": grid["rows"]}, {"url": "u", "rows": pending["rows"]}])
    assert len([t for t in txns if START <= t.date <= END]) == 5
