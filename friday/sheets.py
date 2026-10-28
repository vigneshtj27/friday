"""Google Sheets: list spreadsheets, read a tab, append transactions to it.

Appending is deliberately narrow:
  * rows go in with values.append + INSERT_ROWS, so new rows are inserted below
    the existing table and no existing cell is overwritten;
  * each new row follows the tab's own header: mapped columns are filled,
    note/comment/category columns and anything unrecognised are left blank;
  * rows already in the tab (same date, amount and description start) are skipped;
  * text from websites that starts like a formula (= + - @) is written as text.
After appending, the tab is read back and the appended rows are looked up.

Cell contents come from the owner's sheet and from bank pages: data, not instructions.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any
from urllib.parse import quote

import httpx

from friday.capabilities.activity import parse_activity_date
from friday.capabilities.transactions import infer_dayfirst, parse_amount, parse_date

SHEETS = "https://sheets.googleapis.com/v4/spreadsheets"
DRIVE = "https://www.googleapis.com/drive/v3/files"

# header text -> field. First match wins; order matters (debit/credit before amount).
FIELD_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("skip", re.compile(r"note|comment|remark|category|tag|split|reimburs|receipt|month|week|year|balance|running|status", re.I)),
    ("date", re.compile(r"^(txn |transaction |posting |posted |purchase )?date$|^date\b|^day$|^when$", re.I)),
    ("debit", re.compile(r"^debit|^withdrawal|^spent$|^charge", re.I)),
    ("credit", re.compile(r"^credit|^refund|^deposit", re.I)),
    ("amount", re.compile(r"amount|^amt|^value$|^cost$|^price$|^total$|^inr$|^₹$|^rs\.?$|^expense amount", re.I)),
    ("description", re.compile(r"desc|merchant|narration|particular|detail|payee|item|^expense$|^name$|^what$|^vendor|^store", re.I)),
    ("currency", re.compile(r"^currency$|^ccy$", re.I)),
    ("direction", re.compile(r"^type$|^dr/cr$|^cr/dr$|^debit/credit$|^direction$", re.I)),
    ("source", re.compile(r"^card$|^account$|^source$|^paid (with|by|via)|^payment (method|mode)$|^mode$|^bank$", re.I)),
]
FIELDS = {"date", "description", "amount", "debit", "credit", "currency", "direction", "source", "skip"}


class SheetsError(Exception):
    pass


def spreadsheet_id(ref: str) -> str:
    """Accept a spreadsheet id or its URL."""
    m = re.search(r"/spreadsheets/d/([A-Za-z0-9_-]{20,})", ref)
    if m:
        return m.group(1)
    if re.fullmatch(r"[A-Za-z0-9_-]{20,}", ref.strip()):
        return ref.strip()
    raise SheetsError(f"{ref!r} is not a spreadsheet id or URL")


def a1_tab(tab: str) -> str:
    return "'" + tab.replace("'", "''") + "'"


def safe_text(s: str) -> str:
    """Text from a web page must never become a formula."""
    s = s.replace("\n", " ").strip()
    return "'" + s if s[:1] in ("=", "+", "-", "@") else s


def _norm_cell(v: Any) -> tuple[str, Any]:
    """A cell's value, ignoring display format: full dates as dates, amounts as 2-dp numbers, text lowercased."""
    s = str(v).strip().lstrip("'")
    if not s:
        return ("empty", "")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}|\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}|\d{1,2} [A-Za-z]{3,9},? \d{4}|[A-Za-z]{3,9} \d{1,2},? \d{4}", s):
        d = parse_date(s, dayfirst=True)
        if d is not None:
            return ("date", d)
    if re.fullmatch(r"[-(]?[^\w\s]{0,2}\s?[\d,]+(\.\d+)?\)?", s):
        a = parse_amount(s)
        if a is not None:
            return ("number", f"{(-a[0] if s.startswith(('-', '(')) else a[0]):.2f}")
    return ("text", re.sub(r"\s+", " ", s.lower()))


def cells_match(written: list[Any], shown: list[Any]) -> bool:
    """Is `shown` (as Sheets displays it) the row friday wrote? Dates and numbers compare by value."""
    shown = list(shown) + [""] * max(0, len(written) - len(shown))
    return all(_norm_cell(w) == _norm_cell(s) for w, s in zip(written, shown) if str(w).strip())


def find_header(values: list[list[str]]) -> int:
    """Index of the header row: the first of the top 10 rows with at least two text cells."""
    for i, row in enumerate(values[:10]):
        texts = [c for c in row if str(c).strip() and parse_amount(str(c)) is None]
        if len(texts) >= 2:
            return i
    raise SheetsError("the tab has no header row (put column names like Date, Description, Amount in the first row)")


def map_columns(header: list[str], overrides: dict[str, str] | None = None) -> dict[int, str]:
    """Column index -> field. `overrides` maps header text to a field and wins over the patterns."""
    norm = {k.strip().lower(): v for k, v in (overrides or {}).items()}
    for v in norm.values():
        if v not in FIELDS:
            raise SheetsError(f"unknown field {v!r} in column map; use one of {sorted(FIELDS)}")
    out: dict[int, str] = {}
    used: set[str] = set()
    for i, h in enumerate(header):
        name = str(h).strip()
        if not name:
            continue
        if name.lower() in norm:
            out[i] = norm[name.lower()]
            continue
        for f, pat in FIELD_PATTERNS:
            if pat.search(name) and (f == "skip" or f not in used):
                out[i] = f
                used.add(f)
                break
    fields = set(out.values())
    if "date" not in fields or not ({"amount", "debit"} & fields) or "description" not in fields:
        raise SheetsError(
            f"could not find Date, Description and Amount columns in {header!r}; tell me which column is which"
        )
    return out


def _key(d: date | None, amount: Decimal | None, desc: str, source: str = "") -> tuple[Any, ...]:
    """Duplicate key. `source` (the Card/Account column, when the tab has one) keeps two cards' identical charges apart."""
    norm = lambda x: re.sub(r"[^a-z0-9]", "", x.lower())  # noqa: E731
    return (d, f"{amount:.2f}" if amount is not None else None, norm(desc)[:16], norm(source))


@dataclass
class Plan:
    header_row: int
    mapping: dict[int, str]
    rows: list[list[Any]] = field(default_factory=list)
    keys: list[tuple[Any, ...]] = field(default_factory=list)
    already_in_sheet: int = 0
    credits_left_out: int = 0
    blank_columns: list[str] = field(default_factory=list)
    total: Decimal = Decimal(0)
    source: str = ""  # the Card/Account value written, when the tab has such a column


def existing_keys(values: list[list[str]], header_row: int, mapping: dict[int, str], *, start: date, end: date) -> list[tuple[Any, ...]]:
    col = {f: i for i, f in mapping.items() if f != "skip"}
    data = values[header_row + 1 :]
    dates = [r[col["date"]] for r in data if col["date"] < len(r)]
    dayfirst = infer_dayfirst([str(d) for d in dates], default=True)
    out = []
    for r in data:
        get = lambda f: str(r[col[f]]) if f in col and col[f] < len(r) else ""  # noqa: E731
        d = parse_activity_date(get("date"), start=start, end=end, dayfirst=dayfirst) if get("date") else None
        a = parse_amount(get("amount") or get("debit") or get("credit"))
        out.append(_key(d, abs(a[0]) if a else None, get("description"), get("source")))
    return out


def plan_append(
    values: list[list[str]], txns: list[Any], *, source_label: str, start: date, end: date,
    include_credits: bool = False, column_map: dict[str, str] | None = None,
) -> Plan:  # fmt: skip
    """Work out the rows to append, without touching anything."""
    h = find_header(values)
    header = [str(c) for c in values[h]]
    mapping = map_columns(header, column_map)
    plan = Plan(h, mapping, source=source_label if "source" in set(mapping.values()) else "")
    plan.blank_columns = [header[i] for i, f in mapping.items() if f == "skip"] + [
        header[i] for i in range(len(header)) if header[i].strip() and i not in mapping
    ]
    have = set(existing_keys(values, h, mapping, start=start, end=end))
    fields = set(mapping.values())
    width = max(i for i in mapping) + 1
    for t in sorted(txns, key=lambda t: (t.date, t.raw_ref)):
        if t.direction == "credit" and not include_credits:
            plan.credits_left_out += 1
            continue
        k = _key(t.date, t.amount, t.description, source_label if "source" in fields else "")
        if k in have:
            plan.already_in_sheet += 1
            continue
        have.add(k)
        signed = t.amount if t.direction == "debit" else -t.amount
        row: list[Any] = [""] * width
        for i, f in mapping.items():
            if f == "date":
                row[i] = t.date.isoformat()
            elif f == "description":
                row[i] = safe_text(t.description)
            elif f == "amount":
                # One amount column: charges positive, refunds negative (unless a Type column says which).
                row[i] = str(t.amount if "direction" in fields else signed)
            elif f == "debit":
                row[i] = str(t.amount) if t.direction == "debit" else ""
            elif f == "credit":
                row[i] = str(t.amount) if t.direction == "credit" else ""
            elif f == "currency":
                row[i] = t.currency
            elif f == "direction":
                row[i] = "Debit" if t.direction == "debit" else "Credit"
            elif f == "source":
                row[i] = safe_text(source_label)
        plan.rows.append(row)
        plan.keys.append(k)
        plan.total += signed
    return plan


def check_after(values: list[list[str]], plan: Plan, *, start: date, end: date, rows_before: int) -> dict[str, Any]:
    """Read-back verification: every appended row is present; row count and the range's total."""
    present = existing_keys(values, plan.header_row, plan.mapping, start=start, end=end)
    found = sum(1 for k in plan.keys if k in set(present))
    col = {f: i for i, f in plan.mapping.items() if f != "skip"}
    data = values[plan.header_row + 1 :]
    total = Decimal(0)
    in_range = 0
    for r, k in zip(data, present):
        if plan.source and k[3] != _key(None, None, "", plan.source)[3]:
            continue  # another card's rows in a shared tab
        if k[0] is not None and start <= k[0] <= end and k[1] is not None:
            in_range += 1
            amt = Decimal(k[1])
            get = lambda f: str(r[col[f]]) if f in col and col[f] < len(r) else ""  # noqa: E731
            neg = (
                ("credit" in col and get("credit").strip() and not get("debit").strip())
                or get("direction").lower().startswith("c")
                or get("amount").strip().startswith(("-", "("))
            )
            total += -amt if neg else amt
    data_rows = sum(1 for r in data if any(str(c).strip() for c in r))
    return {
        "ok": found == len(plan.keys) and data_rows >= rows_before + len(plan.keys),
        "appended": len(plan.keys),
        "found_after": found,
        "rows_before": rows_before,
        "rows_after": data_rows,
        "rows_in_range": in_range,
        "total_in_range": str(total),
    }


class SheetsClient:
    def __init__(self, auth: Any, *, transport: httpx.AsyncBaseTransport | None = None):
        self.auth = auth
        self._transport = transport  # tests

    @property
    def configured(self) -> bool:
        return self.auth.can("sheets")

    async def _req(self, method: str, url: str, **kw: Any) -> Any:
        if not self.configured:
            raise SheetsError("Google Sheets is not connected; open the connect link first")
        async with httpx.AsyncClient(timeout=30, transport=self._transport) as http:
            token = await self.auth.token_for("drive.list" if url.startswith(DRIVE) else "sheets", http)
            r = await http.request(method, url, headers={"Authorization": f"Bearer {token}"}, **kw)
        if r.status_code in (403, 404):
            raise SheetsError(f"Google said {r.status_code}: no such spreadsheet or tab, or no access to it")
        r.raise_for_status()
        return r.json()

    async def list_spreadsheets(self, query: str | None = None) -> list[dict[str, Any]]:
        q = "mimeType='application/vnd.google-apps.spreadsheet' and trashed=false"
        if query:
            q += " and name contains '" + query.replace("\\", "").replace("'", "\\'") + "'"
        data = await self._req("GET", DRIVE, params={"q": q, "orderBy": "modifiedTime desc", "pageSize": 20,
                                                     "fields": "files(id,name,modifiedTime)"})  # fmt: skip
        return [{"id": f["id"], "name": f["name"], "modified": f.get("modifiedTime")} for f in data.get("files", [])]

    async def tabs(self, sid: str) -> list[dict[str, Any]]:
        data = await self._req("GET", f"{SHEETS}/{sid}", params={"fields": "properties.title,sheets.properties"})
        return [
            {"title": s["properties"]["title"], "sheet_id": s["properties"].get("sheetId"),
             "rows": s["properties"].get("gridProperties", {}).get("rowCount")}  # fmt: skip
            for s in data.get("sheets", [])
        ]

    async def title(self, sid: str) -> str:
        data = await self._req("GET", f"{SHEETS}/{sid}", params={"fields": "properties.title"})
        return str((data.get("properties") or {}).get("title") or "")

    async def add_tab(self, sid: str, title: str) -> dict[str, Any]:
        d = await self._req("POST", f"{SHEETS}/{sid}:batchUpdate", json={"requests": [{"addSheet": {"properties": {"title": title}}}]})
        props = ((d.get("replies") or [{}])[0].get("addSheet") or {}).get("properties") or {}
        return {"title": props.get("title", title), "sheet_id": props.get("sheetId")}

    async def delete_rows(self, sid: str, sheet_id: int, row_indexes: list[int]) -> None:
        """Delete whole rows by 0-based index, bottom-up so indexes stay valid."""
        reqs = [{"deleteDimension": {"range": {"sheetId": sheet_id, "dimension": "ROWS", "startIndex": i, "endIndex": i + 1}}}
                for i in sorted(set(row_indexes), reverse=True)]
        if reqs:
            await self._req("POST", f"{SHEETS}/{sid}:batchUpdate", json={"requests": reqs})

    async def read(self, sid: str, tab: str, *, render: str = "FORMATTED_VALUE") -> list[list[str]]:
        """Read a tab. render='FORMULA' returns the underlying formulas (a cell like '=SUM(..)') instead
        of their displayed values, so a writer can refuse to overwrite a formula cell."""
        data = await self._req("GET", f"{SHEETS}/{sid}/values/{quote(a1_tab(tab))}",
                               params={"valueRenderOption": render, "majorDimension": "ROWS"})  # fmt: skip
        return [[str(c) for c in row] for row in data.get("values", [])]

    async def update(self, sid: str, tab: str, a1: str, rows: list[list[Any]]) -> dict[str, Any]:
        """Overwrite cells starting at `a1` (e.g. 'A3') with `rows` (USER_ENTERED). Used by the
        reconciler to rewrite the data block in place; appends stay on append()."""
        if not rows:
            return {}
        rng = f"{a1_tab(tab)}!{a1}"
        return await self._req(
            "PUT", f"{SHEETS}/{sid}/values/{quote(rng)}",
            params={"valueInputOption": "USER_ENTERED"},
            json={"values": rows, "majorDimension": "ROWS"},
        )  # fmt: skip

    async def append(self, sid: str, tab: str, rows: list[list[Any]]) -> dict[str, Any]:
        if not rows:
            return {}
        return await self._req(
            "POST", f"{SHEETS}/{sid}/values/{quote(a1_tab(tab) + '!A1')}:append",
            params={"valueInputOption": "USER_ENTERED", "insertDataOption": "INSERT_ROWS"},
            json={"values": rows, "majorDimension": "ROWS"},
        )  # fmt: skip


def validate_then(then: dict[str, Any]) -> dict[str, Any]:
    """The follow-up step a fetch may carry: {"sheet": {"spreadsheet", "tab", "include_credits"?, "column_map"?}}."""
    sheet = (then or {}).get("sheet")
    if not isinstance(sheet, dict):
        raise ValueError("then must be {'sheet': {'spreadsheet': ..., 'tab': ...}}")
    tab = str(sheet.get("tab") or "").strip()
    if not tab or len(tab) > 100:
        raise ValueError("then.sheet needs the tab name")
    cmap = sheet.get("column_map") or None
    if cmap is not None and (not isinstance(cmap, dict) or any(v not in FIELDS for v in cmap.values())):
        raise ValueError(f"column_map values must be among {sorted(FIELDS)}")
    return {"sheet": {"spreadsheet": spreadsheet_id(str(sheet.get("spreadsheet") or "")), "tab": tab,
                      "include_credits": bool(sheet.get("include_credits")), "column_map": cmap}}  # fmt: skip


def _status_column(header: list[str]) -> int | None:
    for i, h in enumerate(header):
        if re.fullmatch(r"\s*status\s*", str(h), re.I):
            return i
    return None


async def export_transactions(
    ctx: Any, *, spreadsheet: str, tab: str, account: str, start: date, end: date,
    include_credits: bool = False, column_map: dict[str, str] | None = None,
    status: str | None = None, sources: list[str] | None = None, source_label: str | None = None, pending: bool = False,
) -> dict[str, Any]:  # fmt: skip
    """Append the stored transactions of one account and range to a tab, then verify by reading it back.

    `status`/`sources` select rows by status (e.g. 'email' evidence) and source; `pending` marks each
    appended row PENDING (a Status column if the tab has one, else a ' (PENDING)' suffix on the
    description) and `source_label` overrides the Card/Account value written.
    """
    sid = spreadsheet_id(spreadsheet)
    profile = ctx.profiles.get(account)
    site = ctx.sites.get(profile.site_id)
    from friday.capabilities.transactions import Transaction

    if sources is not None:
        rows = ctx.transactions.list(sources=sources, status=status, start=start.isoformat(), end=end.isoformat(), limit=20000)
    else:
        rows = ctx.transactions.list(source=profile.id, status=status, start=start.isoformat(), end=end.isoformat(), limit=20000)
    before = await ctx.sheets.read(sid, tab)
    status_col = _status_column([str(c) for c in before[find_header(before)]]) if pending else None
    txns = [
        Transaction(date.fromisoformat(r["date"]),
                    r["description"] + (" (PENDING)" if pending and status_col is None else ""),
                    Decimal(r["amount"]), r["currency"], r["direction"],
                    Decimal(r["balance"]) if r.get("balance") else None, r["source"], r["raw_ref"])  # fmt: skip
        for r in rows
    ]
    label = source_label if source_label is not None else (site.name if site else profile.id)
    plan = plan_append(before, txns, source_label=label, start=start, end=end,
                       include_credits=include_credits, column_map=column_map)  # fmt: skip
    if pending and status_col is not None:
        for row in plan.rows:
            if status_col < len(row):
                row[status_col] = "PENDING"
    rows_before = sum(1 for r in before[plan.header_row + 1 :] if any(str(c).strip() for c in r))
    await ctx.sheets.append(sid, tab, plan.rows)
    ctx.audit.record("sheets.append", actor="system", target=sid, tab=tab, rows=len(plan.rows), account=profile.id)
    after = await ctx.sheets.read(sid, tab)
    check = check_after(after, plan, start=start, end=end, rows_before=rows_before)
    return {
        **check,
        "stored_transactions": len(txns),
        "already_in_sheet": plan.already_in_sheet,
        "credits_left_out": plan.credits_left_out,
        "charges_considered": plan.already_in_sheet + len(plan.keys),
        "appended_total": str(plan.total),
        "columns_left_blank": plan.blank_columns,
        "spreadsheet": sid,
        "tab": tab,
    }
