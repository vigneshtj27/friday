"""Capability: fetch recent / unbilled account activity for a date range, from the page itself.

For ranges with no statement file yet (a card's current cycle), the agent loop
opens the account's activity page and reads the transactions with
extract_table. Rows are then parsed in code, not taken from the model's
retelling:

  1. every table the agent extracted is parsed (header row by name, or columns
     inferred from their contents when there is no header);
  2. only if no table could be parsed, rows the agent reported in finish data
     are accepted, and each one must be grounded: its amount and the start of
     its description must appear in the text of a page the agent read.

Rows then go through the same Transaction / verify / dedupe path as a
statement. No site-specific code: where the activity page is and how to show a
full range are learned as site notes.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from friday.agent.loop import AgentLoop, AgentTask
from friday.capabilities.statement import CapabilityResult
from friday.capabilities.transactions import (
    Transaction,
    _map_header,
    parse_amount,
    parse_date,
    rows_to_transactions,
    verify,
)

log = logging.getLogger(__name__)

_YEARLESS = ["%d %b", "%b %d", "%d %B", "%B %d", "%d-%b", "%b-%d", "%d/%m", "%m/%d", "%d %b,", "%b %d,"]
_WEEKDAY = re.compile(r"^(mon|tue|wed|thu|fri|sat|sun)[a-z]*,?\s+", re.I)


def parse_activity_date(text: str, *, start: date, end: date, dayfirst: bool = True) -> date | None:
    """A full date, or a date without a year placed in the requested range (or the year before `end`)."""
    t = _WEEKDAY.sub("", text.strip()).strip(" ,")
    if not t:
        return None
    full = parse_date(t, dayfirst=dayfirst)
    if full and full.year > 1900:
        return full
    fmts = [f for f in _YEARLESS if not ("%m/%d" in f and dayfirst) and not ("%d/%m" in f and not dayfirst)]
    for f in fmts:
        try:  # parse against a leap year so 29 Feb works; the real year is chosen below
            md = datetime.strptime(f"{t.rstrip(',')} 2000", f"{f.rstrip(',')} %Y")
        except ValueError:
            continue
        for year in (end.year, end.year - 1):
            try:
                d = date(year, md.month, md.day)
            except ValueError:
                continue
            if d <= end:
                return d
    return None


def _is_amount(cell: str) -> bool:
    return bool(re.search(r"\d", cell)) and parse_amount(cell) is not None and not re.search(r"[A-Za-z]{4,}", cell)


def infer_columns(rows: list[list[str]], *, start: date, end: date) -> list[list[str]] | None:
    """For a table without a usable header: pick date/amount/description columns by content.

    Returns the rows with a synthetic header row (Date, Description, Amount) and the date cells
    rewritten to ISO, or None when the table doesn't look like transactions.
    """
    width = max((len(r) for r in rows), default=0)
    if width < 2 or len(rows) < 1:
        return None
    def share(col: int, test: Any) -> float:
        cells = [r[col] for r in rows if col < len(r) and r[col].strip()]
        return sum(1 for c in cells if test(c)) / max(len(rows), 1)
    date_col = max(range(width), key=lambda c: share(c, lambda x: parse_activity_date(x, start=start, end=end) is not None))
    if share(date_col, lambda x: parse_activity_date(x, start=start, end=end) is not None) < 0.6:
        return None
    amount_cols = [c for c in range(width) if c != date_col and share(c, _is_amount) >= 0.6]
    if not amount_cols:
        return None
    amount_col = amount_cols[-1]  # rightmost money column: the amount, not a reference number
    rest = [c for c in range(width) if c not in (date_col, amount_col)]
    # The description is the column with the most letters (not a reference number or a status chip).
    desc_col = max(rest, key=lambda c: sum(len(re.findall(r"[A-Za-z]", r[c])) for r in rows if c < len(r)), default=-1)
    out = [["Date", "Description", "Amount"]]
    for r in rows:
        get = lambda c: r[c] if 0 <= c < len(r) else ""  # noqa: E731
        d = parse_activity_date(get(date_col), start=start, end=end)
        out.append([d.isoformat() if d else "", get(desc_col), get(amount_col)])
    return out


def _with_iso_dates(rows: list[list[str]], *, start: date, end: date) -> list[list[str]]:
    """Rewrite the date column of a headed table to ISO, so year-less dates parse."""
    for i, row in enumerate(rows[:40]):
        cols = _map_header(row)
        if cols:
            dc = cols["date"]
            out = rows[: i + 1]
            for r in rows[i + 1 :]:
                r = list(r)
                if dc < len(r):
                    d = parse_activity_date(r[dc], start=start, end=end)
                    if d:
                        r[dc] = d.isoformat()
                out.append(r)
            return out
    return rows


def parse_tables(
    tables: list[dict[str, Any]], *, start: date, end: date, source: str, currency: str, card: bool, run_id: str, dayfirst: bool = True
) -> tuple[list[Transaction], list[dict[str, Any]]]:
    """Every extracted table -> transactions. Identical tables (re-extracted pages) count once."""
    txns: list[Transaction] = []
    report = []
    seen: set[str] = set()
    for n, t in enumerate(tables):
        rows = [[str(c) for c in r] for r in t.get("rows") or []]
        key = repr(rows)
        if not rows or key in seen:
            continue
        seen.add(key)
        ref = f"{run_id}:table{n}"
        try:
            got = rows_to_transactions(_with_iso_dates(rows, start=start, end=end), source=source, raw_ref=ref,
                                       currency=currency, dayfirst=dayfirst, card=card)  # fmt: skip
            how = "header"
        except ValueError:
            inferred = infer_columns(rows, start=start, end=end)
            if inferred is None:
                report.append({"table": n, "url": t.get("url"), "rows": len(rows), "used": False})
                continue
            got = rows_to_transactions(inferred, source=source, raw_ref=ref, currency=currency, dayfirst=dayfirst, card=card)
            how = "inferred columns"
        txns += got
        report.append({"table": n, "url": t.get("url"), "rows": len(rows), "transactions": len(got), "how": how})
    return txns, report


def _digits(s: str) -> str:
    return re.sub(r"[^\d]", "", s)


def grounded_rows(
    reported: list[dict[str, Any]], page_texts: list[str], *, start: date, end: date, source: str, currency: str, run_id: str
) -> tuple[list[Transaction], int]:
    """Rows the agent reported, kept only if the page it read shows them."""
    text = "\n".join(page_texts)
    flat = re.sub(r"\s+", " ", text).lower()
    digits_text = _digits(text)
    out, dropped = [], 0
    for n, r in enumerate(reported or []):
        try:
            desc = str(r.get("description") or "").strip()
            amt_txt = str(r.get("amount") or "")
            a = parse_amount(amt_txt)
            d = parse_activity_date(str(r.get("date") or ""), start=start, end=end)
            if not (desc and a and d):
                raise ValueError("incomplete row")
            amount = abs(a[0])
            cents = f"{amount:.2f}"
            # The amount as displayed (digits only, with or without paise) and the description's start must be on the page.
            amount_seen = _digits(cents) in digits_text or (cents.endswith(".00") and _digits(cents[:-3]) in digits_text)
            desc_seen = re.sub(r"\s+", " ", desc.lower())[:24] in flat
            if not (amount_seen and desc_seen):
                raise ValueError("not on the page")
        except (ValueError, TypeError, AttributeError):
            dropped += 1
            continue
        direction = "credit" if str(r.get("direction", "")).lower() == "credit" else "debit"
        out.append(Transaction(d, desc[:300], amount, currency, direction, None, source, f"{run_id}:reported{n}"))
    return out, dropped


class FetchActivity:
    name = "fetch_activity"

    def __init__(self, ctx: Any, llm: Any):
        self.ctx = ctx
        self.llm = llm

    @staticmethod
    def validate(params: dict[str, Any]) -> dict[str, Any]:
        start, end = date.fromisoformat(params["start"]), date.fromisoformat(params["end"])
        if end < start:
            raise ValueError("end is before start")
        if (end - start).days > 120:
            raise ValueError("activity pages cover recent months; use fetch_statement for older periods")
        account_type = params.get("account_type") or "card"
        if account_type not in ("card", "bank"):
            raise ValueError("account_type is card or bank")
        return {"start": start.isoformat(), "end": end.isoformat(), "account_type": account_type, "currency": params.get("currency")}

    async def run(self, op: Any, ex: Any, run_id: str, params: dict[str, Any]) -> CapabilityResult:
        profile, site = op.profile, op.site
        start, end = date.fromisoformat(params["start"]), date.fromisoformat(params["end"])
        currency = (params.get("currency") or self.ctx.settings.default_currency).upper()
        card = params.get("account_type", "card") == "card"
        task = AgentTask(
            goal=(
                f"You are signed in to account '{profile.account}'. Read every transaction dated {start.isoformat()} to "
                f"{end.isoformat()} (inclusive) from the site's own pages: recent activity, unbilled or pending "
                "transactions, the current statement period, or the account's transaction history. Open the page that "
                "lists them; choose a date range or period on the page if it offers one; expand 'show more' / 'load "
                "more' until the whole range is visible. Then call extract_table on the list of transactions (pass the "
                "ref of any row in it). If the range spans several pages or sections (for example pending and posted), "
                "extract each. Do not download a statement file for this and change nothing. Finish with status success "
                "once the tables are extracted. Only if the transactions are not in any table that extract_table can "
                "read, list them yourself in finish data as {\"transactions\": [{\"date\", \"description\", \"amount\", "
                "\"direction\": \"debit\"|\"credit\"}]}, copying date, description and amount exactly as the page shows "
                "them. Add a note saying where the activity page is and how to show the full range."
            ),
            run_id=run_id,
            profile_id=profile.id,
            site_name=site.name,
            allowed_domains=site.all_domains,
            secret_names=[],
            site_notes=self.ctx.sitenotes.render(site.id),
            max_steps=self.ctx.settings.max_steps,
            max_wall_seconds=self.ctx.settings.max_wall_seconds,
            max_cost_usd=self.ctx.settings.max_cost_usd,
        )
        outcome = await AgentLoop(self.llm, ex, self.ctx.runs).run(task)
        dayfirst = not profile.config.locale.lower().startswith("en-us")
        txns, tables = parse_tables(ex.tables, start=start, end=end, source=profile.id, currency=currency, card=card,
                                    run_id=run_id, dayfirst=dayfirst)  # fmt: skip
        dropped = 0
        how = "tables"
        if not txns:
            txns, dropped = grounded_rows((outcome.data or {}).get("transactions") or [], ex.page_texts, start=start,
                                          end=end, source=profile.id, currency=currency, run_id=run_id)  # fmt: skip
            how = "rows checked against the page text"
        in_range = [t for t in txns if start <= t.date <= end]
        base = {"agent_status": outcome.status, "agent_summary": outcome.summary, "tables": tables, "source": how,
                "ungrounded_rows_dropped": dropped, "outside_range": len(txns) - len(in_range)}  # fmt: skip
        if not in_range:
            return CapabilityResult(
                "failed" if outcome.status != "success" else "needs_attention",
                f"No transactions for {start}..{end} could be read from the page. {outcome.summary}".strip(),
                base, outcome.notes, outcome.contradicted_notes,
            )  # fmt: skip
        check = verify(in_range, start=start, end=end)
        result = {**base, "verification": check}
        if not check["ok"]:
            return CapabilityResult("needs_attention", "Read the activity, but it did not pass checks: " + "; ".join(check["problems"]),
                                    result, outcome.notes, outcome.contradicted_notes)  # fmt: skip
        new, dupes = self.ctx.transactions.add(in_range)
        result["stored"] = {"new": new, "duplicates": dupes}
        debits = sum((t.amount for t in in_range if t.direction == "debit"), Decimal(0))
        credits = sum((t.amount for t in in_range if t.direction == "credit"), Decimal(0))
        result["totals"] = {"debits": str(debits), "credits": str(credits), "currency": currency}
        summary = (
            f"Activity {start}..{end}: {len(in_range)} transactions ({new} new, {dupes} already stored); "
            f"charges {currency} {debits:,.2f}, credits {currency} {credits:,.2f}."
        )
        if dropped:
            summary += f" {dropped} reported row(s) were not on the page and were left out."
        return CapabilityResult("succeeded", summary, result, outcome.notes, outcome.contradicted_notes)
