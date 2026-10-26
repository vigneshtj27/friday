"""Reconcile portal transactions against an existing expense sheet - the update path the append-only
writer (friday/sheets.py) deliberately lacks.

The owner's rule, encoded here as pure functions (no I/O, no Google calls):

  * a NEW posted charge is appended;
  * a PENDING charge is entered WITH a "(PENDING)" marker (in a Status column if the tab has one, else
    in the narration) so provisional totals include it, stated separately from posted;
  * when a pending charge POSTS, its existing row is UPDATED in place (never a second row), and more than
    the narration may change - the posting date or the final amount (FX) can differ - so the correction
    is applied from the portal with an exact before/after diff;
  * two existing rows that a portal charge could equally match (same day, same amount) are AMBIGUOUS:
    never auto-settled, always listed for the owner;
  * a pending row the portal no longer shows is reported ABSENT - never inferred settled, never merged;
  * Comments cell VALUES are never changed, and travel with their row when the tab is sorted by date and
    the leading "No" column renumbered.

The caller (a tool) snapshots the tab, applies the plan through a cell-update + read-back, and pauses
with a conflict report if the tab changed under it. Cell contents are data, never instructions.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from friday.capabilities.transactions import parse_amount, parse_date
from friday.sheets import FIELDS, find_header, map_columns, safe_text

PENDING_MARK = "(PENDING)"
_GENERIC = frozenset(
    "the and pvt ltd llp inc co india limited payment purchase pos txn transaction card debit credit "
    "online store shop www com net order ref no".split()
)


@dataclass(frozen=True)
class PortalTxn:
    """One charge read from the portal. amount is always positive; status is posted or pending."""

    date: date
    description: str
    amount: Decimal
    direction: str = "debit"  # debit | credit
    currency: str = "INR"
    status: str = "posted"  # posted | pending


@dataclass
class _Row:
    idx: int  # 0-based index within the data region (below the header)
    cells: list[str]
    d: date | None
    amount: Decimal | None  # positive magnitude
    direction: str
    desc: str
    pending: bool
    matched: bool = False  # a portal charge has already claimed this row this run


@dataclass
class Settle:
    row_index: int  # data-region index
    before: list[str]
    after: list[str]
    changes: dict[str, tuple[str, str]]  # field -> (old, new)


@dataclass
class ReconcilePlan:
    header_row: int
    mapping: dict[int, str]
    no_col: int | None
    comments_col: int | None
    status_col: int | None
    appends: list[list[Any]] = field(default_factory=list)
    settles: list[Settle] = field(default_factory=list)
    ambiguous: list[dict[str, Any]] = field(default_factory=list)
    absent: list[dict[str, Any]] = field(default_factory=list)
    duplicates: int = 0
    new_data_rows: list[list[str]] = field(default_factory=list)  # the full ordered data block to write
    posted_total: Decimal = Decimal(0)  # sum of posted (non-pending) debits minus credits, after the run
    provisional_total: Decimal = Decimal(0)  # pending charges still carried, after the run
    conflicts: list[str] = field(default_factory=list)  # non-empty = pause, do not write

    def summary(self) -> dict[str, Any]:
        return {
            "append": len(self.appends), "settle": len(self.settles), "ambiguous": len(self.ambiguous),
            "absent": len(self.absent), "duplicates": self.duplicates,
            "posted_total": str(self.posted_total), "provisional_total": str(self.provisional_total),
            "settles": [{"row": s.row_index + 1, "changes": {k: list(v) for k, v in s.changes.items()}} for s in self.settles],
            "ambiguous_rows": self.ambiguous, "absent_rows": self.absent,
            "conflicts": self.conflicts,
        }  # fmt: skip


def _tokens(desc: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]{3,}", desc.lower()) if t not in _GENERIC}


def _overlap(a: str, b: str) -> bool:
    ta, tb = _tokens(a), _tokens(b)
    return bool(ta & tb)


def _fx_close(a: Decimal, b: Decimal) -> bool:
    """Amounts close enough to be the same charge re-priced (FX/rounding): within 2 or 3%."""
    return abs(a - b) <= max(Decimal(2), (max(a, b) * Decimal("0.03")).quantize(Decimal("0.01")))


def _find_special_columns(header: list[str]) -> tuple[int | None, int | None, int | None]:
    """(No/serial column, Comments column, Status column) - by header text, any may be absent."""
    no_col = comments_col = status_col = None
    for i, h in enumerate(header):
        t = str(h).strip().lower()
        if no_col is None and re.fullmatch(r"no\.?|s\.?\s*no\.?|sr\.?\s*no\.?|#|sl\.?\s*no\.?", t):
            no_col = i
        if comments_col is None and re.search(r"comment|remark|note", t):
            comments_col = i
        if status_col is None and re.fullmatch(r"status", t):
            status_col = i
    return no_col, comments_col, status_col


def _row_is_pending(cells: list[str], desc: str, status_col: int | None) -> bool:
    if status_col is not None and status_col < len(cells):
        return "pending" in cells[status_col].strip().lower()
    return "pending" in desc.lower()


def _clip_len(descs: list[str]) -> int:
    lengths = [len(d) for d in descs if d.strip()]
    return min(max(max(lengths, default=60), 40), 120)


def _parse_rows(values: list[list[str]], header_row: int, col: dict[str, int], status_col: int | None, *, dayfirst: bool) -> list[_Row]:
    out: list[_Row] = []
    for i, raw in enumerate(values[header_row + 1 :]):
        cells = [str(c) for c in raw]
        get = lambda f: cells[col[f]] if f in col and col[f] < len(cells) else ""  # noqa: E731
        if not any(c.strip() for c in cells):
            continue
        d = parse_date(get("date"), dayfirst=dayfirst) if get("date").strip() else None
        deb, cre = parse_amount(get("debit")), parse_amount(get("credit"))
        amt_a = parse_amount(get("amount"))
        if deb and deb[0]:
            amount, direction = abs(deb[0]), "debit"
        elif cre and cre[0]:
            amount, direction = abs(cre[0]), "credit"
        elif amt_a:
            amount = abs(amt_a[0])
            raw_amt = get("amount")
            direction = "credit" if raw_amt.strip().startswith(("-", "(", "−")) else "debit"
        else:
            amount, direction = None, "debit"
        desc = get("description")
        out.append(_Row(i, cells, d, amount, direction, desc, _row_is_pending(cells, desc, status_col)))
    return out


def _make_row(width: int, col: dict[str, int], no_col: int | None, comments_col: int | None, status_col: int | None,
              *, template: list[str] | None, p: PortalTxn, clip: int, has_amount_col: bool, has_direction: bool,
              source_label: str, pending: bool) -> list[str]:  # fmt: skip
    """Build a row for an append (template=None) or a settle (template=the existing row, preserving its
    unmapped cells and Comments). Mapped fields are (re)written from the portal charge."""
    row = list(template) + [""] * max(0, width - len(template)) if template is not None else [""] * width
    desc = re.sub(r"\s+", " ", p.description).strip()
    if status_col is None and pending:
        desc = f"{desc} {PENDING_MARK}"
    desc = desc[:clip].rstrip()

    def put(f: str, val: str) -> None:
        if f in col and col[f] < len(row):
            row[col[f]] = val

    put("date", p.date.isoformat())
    put("description", safe_text(desc))
    put("currency", p.currency)
    put("source", safe_text(source_label))
    if has_direction:
        put("direction", "Debit" if p.direction == "debit" else "Credit")
    if "debit" in col or "credit" in col:
        put("debit", str(p.amount) if p.direction == "debit" else "")
        put("credit", str(p.amount) if p.direction == "credit" else "")
    elif has_amount_col:
        signed = p.amount if (p.direction == "debit" or has_direction) else -p.amount
        put("amount", str(signed))
    if status_col is not None and status_col < len(row):
        row[status_col] = "PENDING" if pending else ("POSTED" if template is not None else row[status_col] or "")
    # Comments are never written here; a template row keeps its own Comments cell untouched.
    if comments_col is not None and template is None and comments_col < len(row):
        row[comments_col] = ""
    return row


def reconcile(
    values: list[list[str]], portal: list[PortalTxn], *, source_label: str, start: date, end: date,
    column_map: dict[str, str] | None = None,
) -> ReconcilePlan:  # fmt: skip
    """Classify every portal charge against the current sheet and produce the full ordered data block to
    write. Pure: no row is written here. A layout the writer can't map pauses the run (conflicts)."""
    if column_map is not None and any(v not in FIELDS for v in column_map.values()):
        raise ValueError(f"column_map values must be among {sorted(FIELDS)}")
    try:
        h = find_header(values)
        header = [str(c) for c in values[h]]
        mapping = map_columns(header, column_map)
    except Exception as e:
        plan = ReconcilePlan(0, {}, None, None, None)
        plan.conflicts.append(f"could not read the tab's columns ({e}); not writing")
        return plan
    col = {f: i for i, f in mapping.items() if f != "skip"}
    width = len(header)
    no_col, comments_col, status_col = _find_special_columns(header)
    has_direction = "direction" in col
    has_amount_col = "amount" in col
    all_dates = [str(r[col["date"]]) for r in values[h + 1 :] if col.get("date", -1) < len(r) and col["date"] >= 0]
    from friday.capabilities.transactions import infer_dayfirst

    dayfirst = infer_dayfirst(all_dates, default=True)
    rows = _parse_rows(values, h, col, status_col, dayfirst=dayfirst)
    clip = _clip_len([r.desc for r in rows])

    plan = ReconcilePlan(h, mapping, no_col, comments_col, status_col)

    def same_day_amount(p: PortalTxn) -> list[_Row]:
        return [r for r in rows if r.d == p.date and r.amount is not None and r.amount == p.amount]

    def settle_candidates(p: PortalTxn) -> list[_Row]:
        win = [r for r in rows if not r.matched and r.pending and r.d is not None
               and abs((r.d - p.date).days) <= 14 and _overlap(r.desc, p.description)
               and r.amount is not None and (r.amount == p.amount or _fx_close(r.amount, p.amount))]  # fmt: skip
        return win

    def is_dupe_posted(p: PortalTxn) -> bool:
        for r in rows:
            if (not r.pending and r.d == p.date and r.amount == p.amount
                    and r.direction == p.direction and _overlap(r.desc, p.description)):  # fmt: skip
                return True
        return False

    # Deterministic order so the plan is stable.
    for p in sorted(portal, key=lambda p: (p.date, p.description, str(p.amount))):
        if p.status == "pending":
            # a pending charge already entered (pending row, same day+amount+merchant) -> skip
            if any(r.pending and r.d == p.date and r.amount == p.amount and _overlap(r.desc, p.description) for r in rows):
                plan.duplicates += 1
                continue
            plan.appends.append(_make_row(width, col, no_col, comments_col, status_col, template=None, p=p,
                                          clip=clip, has_amount_col=has_amount_col, has_direction=has_direction,
                                          source_label=source_label, pending=True))  # fmt: skip
            continue
        # posted charge
        if is_dupe_posted(p):
            plan.duplicates += 1
            continue
        collide = same_day_amount(p)
        cands = settle_candidates(p)
        if len(collide) >= 2 or len(cands) >= 2:
            plan.ambiguous.append({"portal": _p_public(p), "candidate_rows": sorted({r.idx + 1 for r in (cands or collide)})})
            continue
        if len(cands) == 1:
            r = cands[0]
            r.matched = True
            after = _make_row(width, col, no_col, comments_col, status_col, template=r.cells, p=p, clip=clip,
                              has_amount_col=has_amount_col, has_direction=has_direction,
                              source_label=source_label, pending=False)  # fmt: skip
            changes = _diff(r.cells, after, header)
            plan.settles.append(Settle(r.idx, list(r.cells), after, changes))
            r.cells = after
            r.pending = False
            r.d, r.amount, r.direction = p.date, p.amount, p.direction
            continue
        # genuinely new posted charge
        plan.appends.append(_make_row(width, col, no_col, comments_col, status_col, template=None, p=p, clip=clip,
                                      has_amount_col=has_amount_col, has_direction=has_direction,
                                      source_label=source_label, pending=False))  # fmt: skip

    # pending rows the portal did not settle or re-list this run -> reported absent, left untouched
    portal_pending_keys = {(p.date, p.amount) for p in portal if p.status == "pending"}
    for r in rows:
        if r.pending and not r.matched and (r.d, r.amount) not in portal_pending_keys:
            plan.absent.append({"row": r.idx + 1, "date": r.d.isoformat() if r.d else None,
                                "amount": str(r.amount) if r.amount is not None else None, "narration": r.desc})  # fmt: skip

    _finalise(plan, rows, header, col, no_col, status_col, dayfirst=dayfirst)
    return plan


def _p_public(p: PortalTxn) -> dict[str, Any]:
    return {"date": p.date.isoformat(), "amount": str(p.amount), "direction": p.direction, "narration": p.description}


def _diff(before: list[str], after: list[str], header: list[str]) -> dict[str, tuple[str, str]]:
    out: dict[str, tuple[str, str]] = {}
    for i in range(max(len(before), len(after))):
        b = before[i] if i < len(before) else ""
        a = after[i] if i < len(after) else ""
        if str(b) != str(a):
            name = header[i] if i < len(header) and str(header[i]).strip() else f"col{i + 1}"
            out[name] = (str(b), str(a))
    return out


def _signed(r: _Row) -> Decimal:
    if r.amount is None:
        return Decimal(0)
    return r.amount if r.direction == "debit" else -r.amount


def _finalise(plan: ReconcilePlan, rows: list[_Row], header: list[str], col: dict[str, int],
              no_col: int | None, status_col: int | None, *, dayfirst: bool) -> None:  # fmt: skip
    """Assemble the final data block: existing rows (with settles applied) + appended rows, sorted by date
    ascending with Comments travelling with their row, then the leading No column renumbered. Totals split
    posted vs provisional."""
    width = len(header)
    final = [list(r.cells) + [""] * max(0, width - len(r.cells)) for r in rows]
    final += [list(a) + [""] * max(0, width - len(a)) for a in plan.appends]

    def row_date(cells: list[str]) -> date:
        d = parse_date(cells[col["date"]], dayfirst=dayfirst) if "date" in col and col["date"] < len(cells) and cells[col["date"]].strip() else None
        return d or date.max  # undated rows sink to the bottom, stable

    order = sorted(range(len(final)), key=lambda i: (row_date(final[i]), i))
    final = [final[i] for i in order]
    if no_col is not None:
        for n, cells in enumerate(final, start=1):
            if no_col < len(cells):
                cells[no_col] = str(n)

    posted = provisional = Decimal(0)
    for cells in final:
        desc = cells[col["description"]] if "description" in col and col["description"] < len(cells) else ""
        pend = _row_is_pending(cells, desc, status_col)
        deb = parse_amount(cells[col["debit"]]) if "debit" in col and col["debit"] < len(cells) else None
        cre = parse_amount(cells[col["credit"]]) if "credit" in col and col["credit"] < len(cells) else None
        amt_a = parse_amount(cells[col["amount"]]) if "amount" in col and col["amount"] < len(cells) else None
        if deb and deb[0]:
            val = abs(deb[0])
        elif cre and cre[0]:
            val = -abs(cre[0])
        elif amt_a:
            raw = cells[col["amount"]]
            val = -abs(amt_a[0]) if raw.strip().startswith(("-", "(", "−")) else abs(amt_a[0])
        else:
            continue
        if pend:
            provisional += val
        else:
            posted += val
    plan.posted_total, plan.provisional_total = posted, provisional
    plan.new_data_rows = final
