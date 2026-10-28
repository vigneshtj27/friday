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
    """One charge read from the portal. amount is always positive; status is posted or pending.
    txn_id is the portal/bank transaction id when the portal gives one - the ONLY reliable identity for
    skipping a duplicate or transitioning a pending row to posted. Similarity never proves identity."""

    date: date
    description: str
    amount: Decimal
    direction: str = "debit"  # debit | credit
    currency: str = "INR"
    status: str = "posted"  # posted | pending
    txn_id: str | None = None


@dataclass
class _Row:
    idx: int  # 0-based index within the data region (below the header)
    cells: list[str]
    d: date | None
    amount: Decimal | None  # positive magnitude
    direction: str
    desc: str
    pending: bool
    txn_id: str | None = None  # from a Txn ID / Reference column, if the tab has one
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
    ambiguous: list[dict[str, Any]] = field(default_factory=list)  # NOT entered - the owner decides
    possible_duplicates: list[dict[str, Any]] = field(default_factory=list)  # entered AND flagged (two real charges can look alike)
    absent: list[dict[str, Any]] = field(default_factory=list)
    duplicates: int = 0  # skipped ONLY on reliable identity (a matching transaction id)
    out_of_window: int = 0  # portal charges outside [start, end], excluded and counted (never silent)
    new_data_rows: list[list[str]] = field(default_factory=list)  # the full ordered data block to write
    posted_total: Decimal = Decimal(0)  # sum of posted (non-pending) debits minus credits, after the run
    provisional_total: Decimal = Decimal(0)  # pending charges still carried, after the run
    conflicts: list[str] = field(default_factory=list)  # non-empty = pause, do not write

    def summary(self) -> dict[str, Any]:
        return {
            "append": len(self.appends), "settle": len(self.settles), "ambiguous": len(self.ambiguous),
            "possible_duplicates": len(self.possible_duplicates), "absent": len(self.absent),
            "duplicates": self.duplicates, "out_of_window": self.out_of_window,
            "posted_total": str(self.posted_total), "provisional_total": str(self.provisional_total),
            "settles": [{"row": s.row_index + 1, "changes": {k: list(v) for k, v in s.changes.items()}} for s in self.settles],
            "ambiguous_rows": self.ambiguous, "possible_duplicate_rows": self.possible_duplicates,
            "absent_rows": self.absent, "conflicts": self.conflicts,
        }  # fmt: skip


def _tokens(desc: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]{3,}", desc.lower()) if t not in _GENERIC}


def _overlap(a: str, b: str) -> bool:
    ta, tb = _tokens(a), _tokens(b)
    return bool(ta & tb)


def _fx_close(a: Decimal, b: Decimal) -> bool:
    """Amounts close enough to be the same charge re-priced (FX/rounding): within 2 or 3%."""
    return abs(a - b) <= max(Decimal(2), (max(a, b) * Decimal("0.03")).quantize(Decimal("0.01")))


def _related_amount(a: Decimal, b: Decimal) -> bool:
    """Beyond FX-close but still plausibly the same charge re-priced (a tip, a partial, a big FX move):
    within 25%. Used only to FLAG a pending/posted pair for the owner, never to auto-settle."""
    return abs(a - b) <= (max(a, b) * Decimal("0.25"))


def _find_txnid_column(header: list[str]) -> int | None:
    """A transaction-id / reference column, if the tab has one - the only reliable identity for a row."""
    for i, h in enumerate(header):
        if re.search(r"\b(txn|transaction|reference|ref|arn|auth\s*code)\b.*\b(id|no|number|code|ref)\b|^\s*(txn|transaction)\s*id\s*$|^\s*ref(erence)?\s*(no|number)?\s*$",
                     str(h).strip(), re.I):  # fmt: skip
            return i
    return None


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


def _parse_rows(values: list[list[str]], header_row: int, col: dict[str, int], status_col: int | None,
                txnid_col: int | None, *, dayfirst: bool) -> list[_Row]:  # fmt: skip
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
        txn_id = cells[txnid_col].strip() if txnid_col is not None and txnid_col < len(cells) else None
        out.append(_Row(i, cells, d, amount, direction, desc, _row_is_pending(cells, desc, status_col),
                        txn_id=txn_id or None))  # fmt: skip
    return out


def _make_row(width: int, col: dict[str, int], no_col: int | None, comments_col: int | None, status_col: int | None,
              txnid_col: int | None, *, template: list[str] | None, p: PortalTxn, clip: int, has_amount_col: bool,
              has_direction: bool, source_label: str, pending: bool) -> list[str]:  # fmt: skip
    """Build a row for an append (template=None) or a settle (template=the existing row, preserving its
    unmapped cells and Comments). Mapped fields are (re)written from the portal charge; the transaction id
    is stored when the tab has that column, so a later run recognises the row by reliable identity."""
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
    if txnid_col is not None and txnid_col < len(row) and p.txn_id:
        row[txnid_col] = safe_text(p.txn_id)  # store identity so a later run recognises this row
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
    txnid_col = _find_txnid_column(header)
    has_direction = "direction" in col
    has_amount_col = "amount" in col
    all_dates = [str(r[col["date"]]) for r in values[h + 1 :] if col.get("date", -1) < len(r) and col["date"] >= 0]
    from friday.capabilities.transactions import infer_dayfirst

    dayfirst = infer_dayfirst(all_dates, default=True)
    rows = _parse_rows(values, h, col, status_col, txnid_col, dayfirst=dayfirst)
    clip = _clip_len([r.desc for r in rows])

    plan = ReconcilePlan(h, mapping, no_col, comments_col, status_col)
    entered_ids: set[str] = set()  # transaction ids this run has already entered - reliable same-run identity

    def posted_id_dupe(p: PortalTxn) -> bool:
        # reliable "already present and posted": a matching transaction id on a POSTED row, or one this run
        # already entered. A matching id on a PENDING row is NOT this - it means settle, handled below.
        return bool(p.txn_id) and (p.txn_id in entered_ids
                                   or any(r.txn_id == p.txn_id and not r.pending for r in rows))  # fmt: skip

    def pending_id_rows(p: PortalTxn) -> list[_Row]:
        return [r for r in rows if not r.matched and r.pending and r.txn_id and p.txn_id and r.txn_id == p.txn_id]

    def heuristic_settle(p: PortalTxn) -> list[_Row]:
        return [r for r in rows if not r.matched and r.pending and r.d is not None
                and abs((r.d - p.date).days) <= 14 and _overlap(r.desc, p.description)
                and r.amount is not None and (r.amount == p.amount or _fx_close(r.amount, p.amount))]  # fmt: skip

    def same_day_amount(p: PortalTxn) -> list[_Row]:
        return [r for r in rows if not r.matched and r.pending and r.d == p.date and r.amount == p.amount]

    def near_pending(p: PortalTxn) -> list[_Row]:
        # 1.3(b): a posted charge that heuristic_settle missed but plausibly settles a pending row -
        # same amount re-narrated (no overlap), or same merchant with the amount moved beyond FX. FLAG,
        # never settle: identity is unclear.
        out = []
        for r in rows:
            if r.matched or not r.pending or r.d is None or abs((r.d - p.date).days) > 14 or r.amount is None:
                continue
            amt_same = r.amount == p.amount or _fx_close(r.amount, p.amount)
            if (amt_same and not _overlap(r.desc, p.description)) or (
                    _overlap(r.desc, p.description) and not amt_same and _related_amount(r.amount, p.amount)):
                out.append(r)
        return out

    def plausible_posted(p: PortalTxn) -> list[_Row]:
        # 1.3(a): a pending portal charge the sheet may already carry POSTED (entered earlier) - do not
        # double-enter; flag unless a reliable id already settled it.
        return [r for r in rows if not r.pending and r.d is not None and abs((r.d - p.date).days) <= 14
                and r.amount is not None and (r.amount == p.amount or _fx_close(r.amount, p.amount))
                and _overlap(r.desc, p.description)]  # fmt: skip

    def similar_posted(p: PortalTxn) -> list[_Row]:
        # 1.2: a posted charge that looks like an existing posted row but has no reliable id. Two real
        # same-day same-amount charges from one merchant exist, so ENTER it and flag the pair.
        return [r for r in rows if not r.pending and r.d == p.date and r.amount == p.amount
                and r.direction == p.direction and _overlap(r.desc, p.description)]  # fmt: skip

    def do_append(p: PortalTxn, *, pending: bool) -> None:
        plan.appends.append(_make_row(width, col, no_col, comments_col, status_col, txnid_col, template=None, p=p,
                                      clip=clip, has_amount_col=has_amount_col, has_direction=has_direction,
                                      source_label=source_label, pending=pending))  # fmt: skip
        if p.txn_id:
            entered_ids.add(p.txn_id)

    def do_settle(p: PortalTxn, r: _Row) -> None:
        r.matched = True
        after = _make_row(width, col, no_col, comments_col, status_col, txnid_col, template=r.cells, p=p, clip=clip,
                          has_amount_col=has_amount_col, has_direction=has_direction,
                          source_label=source_label, pending=False)  # fmt: skip
        plan.settles.append(Settle(r.idx, list(r.cells), after, _diff(r.cells, after, header)))
        r.cells, r.pending, r.d, r.amount, r.direction = after, False, p.date, p.amount, p.direction
        if p.txn_id:
            entered_ids.add(p.txn_id)

    # 1.1: only charges within [start, end] are reconciled; the rest are excluded and counted, not silent.
    in_window = [p for p in portal if start <= p.date <= end]
    plan.out_of_window = len(portal) - len(in_window)

    # Deterministic order so the plan is stable.
    for p in sorted(in_window, key=lambda p: (p.date, p.description, str(p.amount), p.txn_id or "")):
        if posted_id_dupe(p):  # already present and posted, by reliable id -> skip
            plan.duplicates += 1
            continue
        id_pending = pending_id_rows(p)
        if p.status == "pending":
            if id_pending:  # the same pending charge is already on the sheet (by id)
                plan.duplicates += 1
                continue
            post = plausible_posted(p)
            if post:  # 1.3(a): the sheet may already have this posted - don't double-enter, flag it
                plan.ambiguous.append({"portal": _p_public(p), "candidate_rows": sorted({r.idx + 1 for r in post}),
                                       "reason": "portal lists this as pending but a posted row may already be it"})  # fmt: skip
                continue
            if any(r.pending and r.d == p.date and r.amount == p.amount and _overlap(r.desc, p.description) for r in rows):
                plan.duplicates += 1  # already entered pending, same day+amount+merchant
                continue
            do_append(p, pending=True)
            continue
        # posted charge: a matching id on a pending row settles it, across any narration/amount change
        if len(id_pending) == 1:
            do_settle(p, id_pending[0])
            continue
        if len(id_pending) >= 2:
            plan.ambiguous.append({"portal": _p_public(p), "candidate_rows": sorted({r.idx + 1 for r in id_pending}),
                                   "reason": "more than one pending row shares this transaction id"})  # fmt: skip
            continue
        collide = same_day_amount(p)
        cands = heuristic_settle(p)
        if len(collide) >= 2 or len(cands) >= 2:
            plan.ambiguous.append({"portal": _p_public(p), "candidate_rows": sorted({r.idx + 1 for r in (cands or collide)}),
                                   "reason": "two pending rows share this day and amount"})  # fmt: skip
            continue
        if len(cands) == 1:
            do_settle(p, cands[0])
            continue
        near = near_pending(p)
        if near:  # 1.3(b): plausibly settles a pending row but identity unclear - enter nothing, flag
            plan.ambiguous.append({"portal": _p_public(p), "candidate_rows": sorted({r.idx + 1 for r in near}),
                                   "reason": "may settle a pending row (re-narrated or amount moved beyond FX) - unresolved"})  # fmt: skip
            continue
        sim = similar_posted(p)
        do_append(p, pending=False)  # 1.2: a genuinely new posted charge; enter it...
        if sim:  # ...but if it looks like an existing posted row with no reliable id, flag the pair
            plan.possible_duplicates.append({"portal": _p_public(p), "candidate_rows": sorted({r.idx + 1 for r in sim}),
                                             "reason": "looks like an already-entered charge, but no transaction id confirms it"})  # fmt: skip

    # pending rows the portal did not settle or re-list this run -> reported absent, left untouched
    portal_pending_keys = {(p.date, p.amount) for p in in_window if p.status == "pending"}
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
