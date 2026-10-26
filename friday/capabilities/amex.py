"""American Express (India) portal profile and READ-ONLY extraction, recorded from the verified flow.

This is configuration and parsing only - no secrets, no payment. Credentials come from the vault item
`amex.me/username` + `/password`, the one-time code through the email OTP broker; nothing here types a
secret or clicks a payment/dispute/settings control. The dashboard summary and the posted/pending
activity sections are parsed into values the reconciler (friday/reconcile_sheet.py) can use; the
statement cycle is derived from the dashboard, never hardcoded.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from friday.capabilities.transactions import parse_amount, parse_date
from friday.reconcile_sheet import PortalTxn

# Site profile (no secrets). Routes are re-inspected live; they are hints, not guarantees.
AMEX_PROFILE: dict[str, Any] = {
    "site_id": "amex-in",
    "name": "American Express India",
    "login_url": "https://www.americanexpress.com/en-in/account/login",
    "domains": ["americanexpress.com", "global.americanexpress.com"],
    "otp": {
        "channel": "email",  # choose EMAIL for the One-Time Verification Code (not SMS)
        "sender": "AmericanExpress@welcome.americanexpress.com",
        "subject": "Your American Express temporary security code",
    },
    "remember_device": False,  # on "Remember this device" choose Continue WITHOUT enabling memory
    "credentials": {"username": "amex.me/username", "password": "amex.me/password"},  # vault item names only
    "routes": {  # observed post-login; re-inspect, do not hardcode the statement period
        "dashboard": "/dashboard",
        "recent_activity": "/activity/recent",
        "statement": "/activity/statement?end=YYYY-MM-DD",
    },
    "scope": "read-and-reconcile only; never Pay Now, autopay, dispute, card controls or limits",
    "notes": (
        "Dashboard carries statement/total balance, available credit, due date, minimum due and MR balance. "
        "Recent activity lists Pending charges separately from the posted total. Derive each account's live "
        "statement cycle from the dashboard. First try the supported secure flow (vault fill + email OTP "
        "broker); use the manual-login handoff only on an actual blocker."
    ),
}


def amex_site() -> Any:
    """A Site record for the Amex profile (read-only usage). Registered via register_amex(ctx)."""
    from friday.profiles.models import Site

    return Site(
        id=AMEX_PROFILE["site_id"], name=AMEX_PROFILE["name"], login_url=AMEX_PROFILE["login_url"],
        domains=AMEX_PROFILE["domains"], otp_senders=[AMEX_PROFILE["otp"]["sender"]],
    )  # fmt: skip


def register_amex(ctx: Any) -> Any:
    site = amex_site()
    ctx.sites.upsert(site)
    return site


# ---- dashboard summary -----------------------------------------------------

_DASH_FIELDS: list[tuple[str, re.Pattern[str]]] = [
    ("minimum_due", re.compile(r"minimum\s*(?:amount\s*)?due", re.I)),
    ("due_date", re.compile(r"(?:payment\s*)?due\s*date", re.I)),
    ("available_credit", re.compile(r"available\s*credit", re.I)),
    ("pending_auths", re.compile(r"pending(?:\s*(?:charges|authorisations|authorizations|auths))?", re.I)),
    ("posted_balance", re.compile(r"(?:statement|closing|total|current|posted)\s*balance", re.I)),
    ("mr_balance", re.compile(r"membership\s*reward|mr\s*(?:points|balance)|reward\s*points", re.I)),
]
_AMOUNT = re.compile(r"(?:₹|rs\.?|inr)?\s*([\d,]+(?:\.\d{1,2})?)", re.I)
_DATE = re.compile(r"(\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4}|[A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4}|\d{4}-\d{2}-\d{2}|\d{1,2}[/.]\d{1,2}[/.]\d{2,4})")


def parse_dashboard(text: str) -> dict[str, Any]:
    """Pull the summary fields from the dashboard text. Each field is read from its own labelled line, so
    posted and pending stay separate. Amounts are strings (no arithmetic); the due date is ISO."""
    out: dict[str, Any] = {}
    for line in text.splitlines():
        low = line.strip()
        if not low:
            continue
        for key, pat in _DASH_FIELDS:
            if key in out or not pat.search(low):
                continue
            if key == "due_date":
                m = _DATE.search(low)
                d = parse_date(m.group(1), dayfirst=True) if m else None
                if d:
                    out[key] = d.isoformat()
            else:
                m = _AMOUNT.search(low[pat.search(low).end():])  # the amount after the label
                a = parse_amount(m.group(1)) if m else None
                if a is not None:
                    out[key] = str(a[0])
            break
    return out


# ---- recent activity (posted + pending, kept separate) ---------------------


def _rows_to_portal(rows: list[list[str]], status: str, *, currency: str) -> list[PortalTxn]:
    out: list[PortalTxn] = []
    for r in rows:
        cells = [str(c).strip() for c in r]
        d = next((parse_date(c, dayfirst=True) for c in cells if _DATE.fullmatch(c)), None)
        amt = next((parse_amount(c) for c in reversed(cells) if parse_amount(c) is not None), None)
        desc = max((c for c in cells if not _DATE.fullmatch(c) and parse_amount(c) is None),
                   key=len, default="")  # fmt: skip
        if d is None or amt is None or not desc:
            continue
        raw = next((c for c in reversed(cells) if parse_amount(c) is not None), "")
        direction = "credit" if raw.strip().startswith(("-", "(", "−")) or amt[1] == "credit" else "debit"
        out.append(PortalTxn(date=d, description=desc, amount=abs(amt[0]), direction=direction,
                             currency=currency, status=status))  # fmt: skip
    return out


def parse_activity(posted_rows: list[list[str]], pending_rows: list[list[str]], *, currency: str = "INR") -> list[PortalTxn]:
    """Turn the recent-activity tables into portal charges: the posted section as status 'posted' and the
    separate pending section as 'pending'. Row order within a section does not matter."""
    return _rows_to_portal(posted_rows, "posted", currency=currency) + _rows_to_portal(pending_rows, "pending", currency=currency)


def statement_cycle(dashboard_text: str) -> tuple[date | None, date | None]:
    """Best-effort statement period derived from the dashboard (never hardcoded). Returns (start, end)
    or (None, None) if the dashboard doesn't state it; the caller then asks rather than guessing."""
    m = re.search(r"statement period[:\s]*" + _DATE.pattern + r"\s*(?:to|-|–)\s*" + _DATE.pattern, dashboard_text, re.I)
    if not m:
        return (None, None)
    return (parse_date(m.group(1), dayfirst=True), parse_date(m.group(2), dayfirst=True))
