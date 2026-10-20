"""Google Sheets writer against a mocked Google API, plus the Google OAuth flow."""

from __future__ import annotations

import json
import re
from datetime import date
from decimal import Decimal
from urllib.parse import unquote

import httpx
import pytest

from friday.capabilities.statement import CapabilityResult
from friday.capabilities.transactions import Transaction
from friday.google_oauth import PURPOSES, GoogleAuth
from friday.profiles.models import ProfileConfig, Site
from friday.sheets import SheetsClient, SheetsError, export_transactions, plan_append, validate_then

SID = "1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"
START, END = date(2026, 10, 1), date(2026, 10, 20)


class FakeGoogle:
    """Enough of the Sheets/Drive API: values get, values append (INSERT_ROWS), tabs, file list.

    Like a sheet in an en-IN locale, appended USER_ENTERED values come back formatted: dates as
    dd/mm/yyyy, numbers with thousands separators, a leading apostrophe hidden.
    """

    def __init__(self, tabs):
        self.tabs = {k: [list(r) for r in v] for k, v in tabs.items()}
        self.appends = []

    @staticmethod
    def _display(v):
        v = str(v)
        if v.startswith("'"):
            return v[1:]
        m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", v)
        if m:
            return f"{m.group(3)}/{m.group(2)}/{m.group(1)}"
        if re.fullmatch(r"-?\d+(\.\d+)?", v):
            return f"{Decimal(v):,.2f}"
        return v

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer tok"
        path = unquote(request.url.path)
        if path.startswith("/drive/v3/files"):
            return httpx.Response(200, json={"files": [{"id": SID, "name": "Expenses 2026", "modifiedTime": "2026-10-19T10:00:00Z"}]})
        m = re.match(r"/v4/spreadsheets/([^/]+)/values/'(.+?)'(!A1)?(:append)?$", path)
        if m:
            tab = m.group(2).replace("''", "'")
            rows = self.tabs[tab]
            if m.group(4):  # append, INSERT_ROWS: new rows go right after the table's last row
                assert request.url.params["insertDataOption"] == "INSERT_ROWS"
                new = json.loads(request.content)["values"]
                self.appends.append(new)
                last = max((i for i, r in enumerate(rows) if any(str(c).strip() for c in r)), default=-1)
                rows[last + 1 : last + 1] = [[self._display(c) for c in r] for r in new]
                return httpx.Response(200, json={"updates": {"updatedRows": len(new)}})
            return httpx.Response(200, json={"values": rows})
        m = re.match(r"/v4/spreadsheets/([^/]+)$", path)
        if m:
            return httpx.Response(200, json={"sheets": [{"properties": {"title": t, "gridProperties": {"rowCount": 1000}}} for t in self.tabs]})
        return httpx.Response(404)


class FakeAuth:
    def connected(self, purpose):
        return True

    def can(self, capability):
        return True

    async def access_token(self, purpose, http):
        return "tok"

    async def token_for(self, capability, http):
        return "tok"


EXISTING = [
    ["Date", "Description", "Category", "Amount", "Notes"],
    ["28/09/2026", "UBER INDIA", "Travel", "310.00", "airport"],
    ["12/10/2026", "AMAZON PAY INDIA", "Shopping", "1,299.00", "headphones - keep receipt"],
]


def T(d, desc, amt, direction="debit"):
    return Transaction(date.fromisoformat(d), desc, Decimal(amt), "INR", direction, None, "amex.me", f"r:{desc}")


OCT = [
    T("2026-10-18", "SWIGGY BANGALORE", "642.00"),
    T("2026-10-12", "AMAZON PAY INDIA", "1299.00"),
    T("2026-10-10", "AMAZON REFUND", "199.00", "credit"),
    T("2026-10-05", "PAYMENT RECEIVED - THANK YOU", "45210.55", "credit"),
    T("2026-10-19", "BLINKIT", "415.50"),
]


@pytest.fixture
def g(ctx):
    ctx.sites.upsert(Site(id="amex", name="American Express", login_url="https://www.americanexpress.com/", domains=["americanexpress.com"]))
    ctx.profiles.create("amex", "me", ProfileConfig(locale="en-IN"))
    ctx.transactions.add(OCT)
    fake = FakeGoogle({"Oct 2026": EXISTING, "Sept 2026": [["Date", "Item", "Amount"]]})
    ctx.sheets = SheetsClient(FakeAuth(), transport=httpx.MockTransport(fake.handler))
    return ctx, fake


async def test_append_matches_columns_skips_duplicates_and_verifies(g):
    ctx, fake = g
    v = await export_transactions(ctx, spreadsheet=f"https://docs.google.com/spreadsheets/d/{SID}/edit#gid=0",
                                  tab="Oct 2026", account="amex.me", start=START, end=END)
    rows = fake.tabs["Oct 2026"]
    assert rows[:3] == EXISTING  # existing rows untouched
    assert fake.appends == [[
        ["2026-10-18", "SWIGGY BANGALORE", "", "642.00", ""],
        ["2026-10-19", "BLINKIT", "", "415.50", ""],
    ]]  # Category and Notes left blank; credits left out; the Amazon row already there
    assert v["ok"] and v["appended"] == 2 and v["already_in_sheet"] == 1 and v["credits_left_out"] == 2
    assert (v["rows_before"], v["rows_after"]) == (2, 4)
    assert v["rows_in_range"] == 3 and v["total_in_range"] == "2356.50"
    assert set(v["columns_left_blank"]) == {"Category", "Notes"}
    assert ctx.audit.list(action_prefix="sheets.append")


async def test_rerun_appends_nothing(g):
    ctx, fake = g
    await export_transactions(ctx, spreadsheet=SID, tab="Oct 2026", account="amex.me", start=START, end=END)
    v = await export_transactions(ctx, spreadsheet=SID, tab="Oct 2026", account="amex.me", start=START, end=END)
    assert v["appended"] == 0 and v["already_in_sheet"] == 3 and len(fake.appends) == 1


async def test_tab_names_are_quoted(g):
    ctx, fake = g
    fake.tabs["Vignesh's card"] = [["Date", "Description", "Amount"]]
    v = await export_transactions(ctx, spreadsheet=SID, tab="Vignesh's card", account="amex.me", start=START, end=END)
    assert v["appended"] == 3 and v["ok"]


def test_website_text_never_becomes_a_formula():
    plan = plan_append([["Date", "Description", "Amount"]], [T("2026-10-03", '=IMPORTXML("http://evil", "//a")', "10.00")],
                       source_label="x", start=START, end=END)
    assert plan.rows[0][1].startswith("'=")


def test_debit_credit_columns_and_include_credits():
    header = [["Date", "Merchant", "Debit", "Credit", "Card"]]
    plan = plan_append(header, OCT, source_label="American Express", start=START, end=END, include_credits=True)
    refund = next(r for r in plan.rows if r[1] == "AMAZON REFUND")
    assert refund[2:] == ["", "199.00", "American Express"]
    assert plan.credits_left_out == 0 and len(plan.rows) == 5


def test_missing_columns_are_reported_not_guessed():
    with pytest.raises(SheetsError, match="Date, Description and Amount"):
        plan_append([["When", "Who"]], OCT, source_label="x", start=START, end=END)
    plan = plan_append([["When", "Spent on", "Rs"]], OCT[:1], source_label="x", start=START, end=END,
                       column_map={"When": "date", "Spent on": "description", "Rs": "amount"})
    assert plan.rows == [["2026-10-18", "SWIGGY BANGALORE", "642.00"]]


def test_validate_then():
    assert validate_then({"sheet": {"spreadsheet": SID, "tab": "Oct"}})["sheet"]["include_credits"] is False
    for bad in ({}, {"sheet": {"spreadsheet": "nope", "tab": "Oct"}}, {"sheet": {"spreadsheet": SID, "tab": ""}},
                {"sheet": {"spreadsheet": SID, "tab": "Oct", "column_map": {"A": "drop table"}}}):
        with pytest.raises(Exception):
            validate_then(bad)


async def test_fetch_then_sheet_reports_both_numbers(g):
    ctx, fake = g
    res = CapabilityResult("succeeded", "Activity 2026-10-01..2026-10-20: 5 transactions.", {}, [], [])
    params = {"start": "2026-10-01", "end": "2026-10-20", "then": validate_then({"sheet": {"spreadsheet": SID, "tab": "Oct 2026"}})}
    await ctx.jobs._then_sheet(res, "amex.me", params)
    assert res.status == "succeeded"
    assert "appended 2 row(s)" in res.summary and "4 data rows (was 2)" in res.summary and "totalling 2356.50" in res.summary


async def test_sheet_failure_marks_the_run(g):
    ctx, _ = g
    res = CapabilityResult("succeeded", "ok.", {}, [], [])
    params = {"start": "2026-10-01", "end": "2026-10-20", "then": validate_then({"sheet": {"spreadsheet": SID, "tab": "Sept 2026"}})}
    await ctx.jobs._then_sheet(res, "amex.me", params)  # "Item" maps to description, so this works...
    assert res.status == "succeeded"
    res2 = CapabilityResult("succeeded", "ok.", {}, [], [])
    ctx.sheets = SheetsClient(FakeAuth(), transport=httpx.MockTransport(lambda r: httpx.Response(404)))
    await ctx.jobs._then_sheet(res2, "amex.me", params)
    assert res2.status == "needs_attention" and "not updated" in res2.summary


async def test_client_lists_and_reads(g):
    ctx, _ = g
    assert (await ctx.sheets.list_spreadsheets("Exp"))[0]["id"] == SID
    assert [t["title"] for t in await ctx.sheets.tabs(SID)] == ["Oct 2026", "Sept 2026"]


# ---- OAuth -----------------------------------------------------------------------------------------


def _token_endpoint(scope, refresh="1//refresh-token-value"):
    def handle(request):
        form = dict(x.split("=", 1) for x in request.content.decode().split("&"))
        assert form["grant_type"] == "authorization_code" and form["code_verifier"]
        body = {"access_token": "ya29.x", "expires_in": 3600, "scope": scope}
        if refresh:
            body["refresh_token"] = refresh
        return httpx.Response(200, json=body)

    return httpx.MockTransport(handle)


async def test_oauth_stores_refresh_token_in_system_namespace(ctx):
    auth = GoogleAuth("client.apps.googleusercontent.com", "secret", ctx.vault)
    url = auth.start("sheets", "http://127.0.0.1:8765/oauth/google/callback")
    state = re.search(r"state=([^&]+)", url).group(1)
    assert "spreadsheets" in unquote(url) and "code_challenge_method=S256" in url
    async with httpx.AsyncClient(transport=_token_endpoint(" ".join(PURPOSES["sheets"]["scopes"]))) as http:
        assert await auth.finish(state, "code", http=http) == "sheets"
        with pytest.raises(ValueError, match="expired or was already used"):
            await auth.finish(state, "code", http=http)  # single use
    assert auth.connected("sheets") and not auth.connected("gmail")
    assert ctx.vault.exists("system/google/sheets-refresh-token")


async def test_oauth_refuses_partial_grant(ctx):
    auth = GoogleAuth("cid", "secret", ctx.vault)
    state = re.search(r"state=([^&]+)", auth.start("sheets", "http://x/cb")).group(1)
    async with httpx.AsyncClient(transport=_token_endpoint("https://www.googleapis.com/auth/spreadsheets")) as http:
        with pytest.raises(ValueError, match="did not grant"):
            await auth.finish(state, "code", http=http)
    assert not auth.connected("sheets")


def test_callback_needs_a_valid_state(ctx):
    from fastapi.testclient import TestClient

    from friday.web.app import create_app

    with TestClient(create_app(ctx), base_url="http://testserver") as c:
        r = c.get("/oauth/google/callback", params={"state": "forged", "code": "x"})
        assert r.status_code == 200 and "expired or was already used" in r.text
        assert c.get("/oauth/google/start", follow_redirects=False).headers["location"].startswith("/login")
