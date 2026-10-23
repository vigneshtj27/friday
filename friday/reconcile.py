"""One-to-one reconciliation of email-evidence rows against imported bank rows.

Email evidence (a "you paid X at Y" mail) is a hint, never statement truth. Reconciliation confirms a
hint only when a real imported bank row matches it: same amount, the date within a small window, and
the merchant overlapping. The match is strictly one-to-one - two same-day same-amount hints never
both collapse onto a single bank row, and a bank row is used at most once - so a genuine pair of
identical charges stays two rows, not one.

Pure functions over plain dicts (date iso string, amount string, description, hash), so they are
driven by synthetic fixtures in tests and by the transaction store in production.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from friday.capabilities.transactions import _desc_overlap


def _d(s: str) -> date:
    return date.fromisoformat(s[:10])


def _amount(s: Any) -> str:
    # compare on the exact string the store holds (it is already normalised to 2 dp there)
    return str(s).strip()


def match_one_to_one(email: list[dict[str, Any]], bank: list[dict[str, Any]], *, window_days: int = 3) -> dict[str, Any]:
    """Greedy one-to-one match. Returns matched pairs and the rows left unmatched on each side.

    A bank row is consumed by at most one email row; the nearest-dated bank row wins when several
    qualify, so same-day same-amount pairs line up rather than collapse.
    """
    used: set[int] = set()
    matched: list[dict[str, Any]] = []
    unmatched_email: list[dict[str, Any]] = []
    for e in email:
        best: tuple[int, int] | None = None  # (date distance, bank index)
        for i, b in enumerate(bank):
            if i in used:
                continue
            if _amount(b["amount"]) != _amount(e["amount"]):
                continue
            dist = abs((_d(b["date"]) - _d(e["date"])).days)
            if dist > window_days:
                continue
            if not _desc_overlap(str(b["description"]), str(e["description"])):
                continue
            if best is None or dist < best[0]:
                best = (dist, i)
        if best is None:
            unmatched_email.append(e)
        else:
            used.add(best[1])
            matched.append({"email": e, "bank": bank[best[1]]})
    unmatched_bank = [b for i, b in enumerate(bank) if i not in used]
    return {"matched": matched, "unmatched_email": unmatched_email, "unmatched_bank": unmatched_bank}
