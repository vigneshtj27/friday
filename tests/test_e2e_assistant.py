"""Phase 4 acceptance, end to end: one chat request fetches a card's activity in a real browser and
populates the owner's expense sheet, verified by read-back; then the same for a second card named
at run time. Real Chrome, real profiles, the real job runner, sign-in engine, activity capability,
planner, tool registry and Sheets writer. Stand-ins: the mock bank (for the card sites), a fake
Google API (for the sheet), a heuristic browser model and a scripted planner (no API key here).

Nothing in the code path knows which card or site it is: the sites below are just records.

Run with: uv run pytest -m chrome tests/test_e2e_assistant.py
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from friday.config import Settings
from friday.context import build_context
from friday.mock_site import PASSWORD, USERNAME, card_activity
from friday.profiles.models import Site
from friday.vault.masking import Redactor

from .conftest import TEST_KEY
from .fake_google import SID, FakeGoogle, install
from .fakes import HeuristicLLM, MockBankServer
from .planner_fakes import PlanLLM, attach, chk, last_friday, plan, settle, step

pytestmark = [pytest.mark.chrome, pytest.mark.timeout(300)]

HEADER = ["Date", "Description", "Category", "Amount", "Card", "Notes"]


@pytest.fixture(scope="module")
def bank():
    with MockBankServer() as server:
        yield server


@pytest.fixture
def ectx(tmp_path):
    s = Settings(_env_file=None, data_dir=tmp_path / "data", volume_encrypted_assert=True, display_backend="auto",
                 action_delay_ms=(0, 0), site_min_login_interval_seconds=0, public_base_url="http://testserver", geoip_url=None,
                 default_timezone="Asia/Kolkata", default_locale="en-IN")  # fmt: skip
    ctx = build_context(s, master_key=TEST_KEY, redactor=Redactor(), llm_factory=lambda: HeuristicLLM(), chat_llm_factory=False)
    yield ctx
    ctx.db.close()


def add_card(ctx, bank, site_id: str, name: str) -> str:
    """What the owner's setup card does: register the site, create the profile, store credentials."""
    ctx.sites.upsert(Site(id=site_id, name=name, login_url=bank.url("/s/normal/login"), domains=["localhost"]))
    p = ctx.profiles.create(site_id, "me")
    ctx.vault.put(f"{site_id}/me/username", USERNAME)
    ctx.vault.put(f"{site_id}/me/password", PASSWORD)
    return p.id


def populate_plan(account: str, start: str, end: str, *, save_sheet: str | None = None) -> dict:
    steps = []
    if save_sheet:
        steps += [
            step("sheets_list", {"query": "Expenses 2026"}, checks=[chk("spreadsheets.0.id", "nonempty")], progress="Finding your sheet"),
            step("sheets_set_expense_sheet", {"spreadsheet": {"$ref": "s1.spreadsheets.0.id"}, "tab": "Oct", "owner_quote": save_sheet},
                 progress="Remembering the expense sheet"),
        ]
    n = len(steps)
    fetch, add = f"s{n + 1}", f"s{n + 2}"
    steps += [
        step("browser_run", {"account": account, "kind": "fetch_activity", "start": start, "end": end, "account_type": "card"},
             checks=[chk("status", "eq", "succeeded", says="the activity was read and passed checks"),
                     chk("counts.debits", "gt", 0, says="charges were found")],
             progress=f"Reading {account} activity"),
        step("sheets_add_transactions", {"account": account, "start": start, "end": end},
             checks=[chk("appended", "eq", {"$ref": f"{fetch}.counts.debits"}, says="every fetched charge was appended"),
                     chk("found_after", "eq", {"$ref": f"{add}.appended"}, says="all appended rows were read back"),
                     chk("total_in_range", "eq", {"$ref": f"{fetch}.totals.debits"}, says="the tab's total matches the fetched charges")],
             progress="Writing to your expense sheet"),
    ]
    return plan(*steps, goal=f"Add {account}'s expenses since {start} to the expense sheet")


async def test_fetch_and_populate_oct_card_expenses_then_a_second_card(ectx, bank):
    ctx = ectx
    fake = FakeGoogle(sheets={SID: {"Oct": [list(HEADER)], "Notes": [["anything"]]}}, titles={SID: "Expenses 2026"})
    install(ctx, fake)
    add_card(ctx, bank, "amex", "American Express")
    today = date.today()
    start, end = today.replace(day=1).isoformat(), today.isoformat()
    expected = [a for a in card_activity(today) if a["dir"] == "debit" and today.replace(day=1) <= a["date"] <= today]

    # 1. Nothing remembered yet: friday asks once.
    llm = attach(ctx, PlanLLM([plan(reply="Which spreadsheet and tab should your expenses go to?")]))
    await ctx.chat.owner_says("Fetch and populate my Oct Amex expenses into the sheet.")
    await settle(ctx)
    assert "Saved expense sheet: none" in llm.plan_inputs[0]
    assert last_friday(ctx).startswith("Which spreadsheet")

    # 2. The owner answers; friday remembers it and does the whole job.
    answer = "Use the Oct tab of Expenses 2026"
    llm = attach(ctx, PlanLLM([populate_plan("amex.me", start, end, save_sheet=answer)],
                              reports=["Added 4 American Express charges from this month to Expenses 2026 (Oct tab)."]))  # fmt: skip
    await ctx.chat.owner_says(answer)
    await settle(ctx)
    rows = fake.sheets[SID]["Oct"]
    assert rows[0] == HEADER  # existing content untouched
    assert [r[1] for r in rows[1:]] == [a["desc"] for a in sorted(expected, key=lambda a: a["date"])]
    assert all(r[4] == "American Express" and r[2] == "" and r[5] == "" for r in rows[1:])  # card filled; Category/Notes left alone
    assert fake.sheets[SID]["Notes"] == [["anything"]]
    msg = last_friday(ctx)
    for says in ("every fetched charge was appended", "all appended rows were read back", "the tab's total matches the fetched charges"):
        assert says in msg
    plan_row = ctx.db.query("SELECT state_json FROM plans ORDER BY created_at DESC LIMIT 1")[0]
    events = json.loads(plan_row["state_json"])["events"]
    assert len(events) >= 3, events  # live progress while it ran
    assert any("reading a table of transactions" in e["text"] for e in events)
    assert ctx.prefs.get("expense_sheet")["tab"] == "Oct" and "Expenses 2026" in ctx.memory.context("expense sheet")
    assert ctx.gate.history() == []  # a private sheet the owner named: no approval card needed

    # 3. Later: the same request uses memory, no question, and adds nothing twice.
    llm = attach(ctx, PlanLLM([plan(
        step("browser_run", {"account": "amex.me", "kind": "fetch_activity", "start": start, "end": end, "account_type": "card"},
             checks=[chk("status", "eq", "succeeded")]),
        step("sheets_add_transactions", {"account": "amex.me", "start": start, "end": end},
             checks=[chk("already_in_sheet", "eq", {"$ref": "s1.counts.debits"}, says="nothing was added twice")]),
    )], reports=["Nothing new since last time."]))  # fmt: skip
    await ctx.chat.owner_says("Fetch and populate my Oct Amex expenses into the sheet.")
    await settle(ctx)
    assert "Expenses 2026" in llm.plan_inputs[0] and "nothing was added twice" in last_friday(ctx)
    assert len(fake.sheets[SID]["Oct"]) == 1 + len(expected)

    # 4. A second card the owner names at run time: same code path, different records.
    add_card(ctx, bank, "hdfc", "HDFC Bank")
    attach(ctx, PlanLLM([populate_plan("hdfc.me", start, end)], reports=["Added 4 HDFC Bank charges too."]))
    await ctx.chat.owner_says("do the same for my HDFC card")
    await settle(ctx)
    hdfc_rows = [r for r in fake.sheets[SID]["Oct"] if r[4] == "HDFC Bank"]
    assert len(hdfc_rows) == len(expected) and "the tab's total matches the fetched charges" in last_friday(ctx)
    statuses = {r["profile_id"]: r["status"] for r in ctx.runs.list()}
    assert statuses == {"amex.me": "succeeded", "hdfc.me": "succeeded"}
