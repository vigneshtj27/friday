"""Parse expense *evidence* out of bank/card/merchant emails - deterministically, in code.

This is evidence, not statement truth. A card charge can exist with no email; an emailed amount
can be a pending authorization that later reverses or settles differently (FX, tips, partial
captures); a merchant's name in an email rarely matches the statement descriptor. So everything
here is marked PENDING until a bank-confirmed fetch matches it (or the owner confirms it).

The email body is untrusted data. It is read only to pull the fields below with fixed per-sender
regex patterns; it is never passed to the model, never logged, and nothing in it triggers a tool
call, a plan step, a memory write or a schedule. A sender that matches no known pattern is skipped
(counted as unparsed), never guessed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Callable

from friday.capabilities.transactions import Transaction, parse_amount, parse_date

KINDS = ("otp", "alert", "order_confirmation")

# A starting set of senders that commonly carry expense evidence. The owner extends it per site
# (prefs key 'mail_evidence_senders:<site_id>') and via the site's otp_senders; the Gmail query is
# always restricted to senders, never a blanket inbox scan.
DEFAULT_EVIDENCE_SENDERS = [
    "AmericanExpress@welcome.americanexpress.com", "safekey@americanexpress.com",
    "alerts@hdfcbank.net", "alerts@icicibank.com", "cc.alerts@axisbank.com", "noreply@sbicard.com",
    "auto-confirm@amazon.in", "order-update@amazon.in", "noreply@flipkart.com",
    "noreply@swiggy.in", "order@zomato.com", "receipts@uber.com",
]

# Common currency words/symbols -> ISO code, so "Rs.1,234" and "INR 1,234" and "$5" normalise.
CURRENCY = {
    "inr": "INR", "rs": "INR", "rs.": "INR", "₹": "INR",
    "usd": "USD", "$": "USD", "us$": "USD",
    "gbp": "GBP", "£": "GBP", "eur": "EUR", "€": "EUR",
    "aed": "AED", "sgd": "SGD", "sg$": "SGD",
}  # fmt: skip
_CUR_ALT = "|".join(re.escape(c) for c in sorted(CURRENCY, key=len, reverse=True))
# An amount with its currency on either side: "INR 1,234.50", "Rs.1,234", "$5.00", "1,234.50 INR".
AMOUNT_RE = re.compile(rf"(?:(?P<pre>{_CUR_ALT})\s*)?(?P<num>\d[\d,]*(?:\.\d{{1,2}})?)(?:\s*(?P<post>{_CUR_ALT}))?", re.I)
DATE_RE = re.compile(
    r"\b(\d{1,2}[/-]\d{1,2}[/-]\d{2,4}"
    r"|\d{4}-\d{2}-\d{2}"
    r"|\d{1,2}(?:st|nd|rd|th)?[ -][A-Za-z]{3,9}[ -]\d{2,4}"
    r"|[A-Za-z]{3,9} \d{1,2},? \d{4})\b"
)


@dataclass(frozen=True)
class EvidenceItem:
    merchant: str
    amount: Decimal
    currency: str
    direction: str  # debit | credit
    date: date | None
    kind: str  # otp | alert | order_confirmation
    confidence_note: str
    message_id: str

    def description(self) -> str:
        return f"{self.merchant} (email {self.kind})"


def _norm_currency(tok: str | None, default: str) -> str:
    if not tok:
        return default
    return CURRENCY.get(tok.strip().lower(), default)


def _money_matches(text: str) -> list[tuple[int, Decimal, str, str | None]]:
    """Every currency-tagged amount in `text` as (position, value, currency_token, matched_text).
    A currency symbol/word is required, so a bare OTP code or a date number is never read as money."""
    out = []
    for m in AMOUNT_RE.finditer(text):
        cur_tok = m.group("pre") or m.group("post")
        if not cur_tok:
            continue
        parsed = parse_amount(m.group("num"))
        if parsed is None or parsed[0] <= 0:
            continue
        out.append((m.start(), parsed[0], cur_tok, m.group(0)))
    return out


def find_amount(text: str, *, default_currency: str, prefer_near: str | None = None) -> tuple[Decimal, str] | None:
    """A money amount from `text`: one near `prefer_near` if that keyword is present, else the first."""
    matches = _money_matches(text)
    if not matches:
        return None
    if prefer_near:
        kw = re.search(prefer_near, text, re.I)
        if kw:
            near = min(matches, key=lambda x: abs(x[0] - kw.start()))
            return near[1], _norm_currency(near[2], default_currency)
    pos, val, tok, _ = matches[0]
    return val, _norm_currency(tok, default_currency)


def find_date(text: str, *, dayfirst: bool = True) -> date | None:
    for m in DATE_RE.finditer(text):
        d = parse_date(re.sub(r"(st|nd|rd|th)", "", m.group(1)), dayfirst=dayfirst)
        if d:
            return d
    return None


def _clean_merchant(name: str) -> str:
    name = re.sub(r"\s+", " ", name).strip(" .,:;-\t")
    return name[:80]


# ---- per-sender registry ---------------------------------------------------
#
# Each pattern decides whether an email matches (by sender/subject) and, if so, extracts one
# EvidenceItem. Patterns are deterministic regex over the plain-text body. Add a merchant or bank
# by appending a Pattern; nothing else changes.


@dataclass(frozen=True)
class Pattern:
    name: str
    kind: str
    sender_re: re.Pattern[str]
    extract: Callable[["ParsedEmail"], EvidenceItem | None]


@dataclass(frozen=True)
class ParsedEmail:
    sender: str
    subject: str
    body: str
    message_id: str
    default_currency: str = "INR"


def _merchant_after(body: str, keyword: str) -> str | None:
    m = re.search(rf"{keyword}\s+([A-Z0-9][\w&.'\- ]{{1,60}}?)(?:\s+(?:on|for|dated|using|worth|ending)\b|[.,\n]|$)", body, re.I)
    return _clean_merchant(m.group(1)) if m else None


def _safekey(e: ParsedEmail) -> EvidenceItem | None:
    """Card 3-D Secure / SafeKey OTP email: a one-time code for a specific purchase + merchant."""
    body = e.body
    if not re.search(r"one[- ]time|verification code|safekey|secure", body + e.subject, re.I):
        return None
    amt = find_amount(body, default_currency=e.default_currency, prefer_near=r"purchase of|amount of|for")
    if amt is None:
        return None
    merchant = _merchant_after(body, r"\bat\b") or _merchant_after(body, r"with\b") or "(merchant from SafeKey email)"
    return EvidenceItem(merchant, amt[0], amt[1], "debit", find_date(body), "otp",
                        "3-D Secure/SafeKey code email: a purchase was attempted; it may not have completed", e.message_id)  # fmt: skip


def _bank_alert(e: ParsedEmail) -> EvidenceItem | None:
    """Bank transaction alert: 'Rs.X was spent/debited/credited ... at MERCHANT on DATE'."""
    body = e.body
    spent = re.search(r"\b(spent|debited|paid|withdrawn|charged)\b", body, re.I)
    got = re.search(r"\b(credited|received|refund(?:ed)?|deposited)\b", body, re.I)
    if not (spent or got):
        return None
    direction = "credit" if got and (not spent or got.start() < spent.start()) else "debit"
    amt = find_amount(body, default_currency=e.default_currency, prefer_near=r"(spent|debited|credited|charged|of|for)")
    if amt is None:
        return None
    merchant = _merchant_after(body, r"\bat\b") or _merchant_after(body, r"\bto\b") or _merchant_after(body, r"\bfrom\b") or "(payee from bank alert)"
    return EvidenceItem(merchant, amt[0], amt[1], direction, find_date(body), "alert",
                        "bank transaction-alert email: amount as notified, not from a statement", e.message_id)  # fmt: skip


def _order_confirmation(e: ParsedEmail) -> EvidenceItem | None:
    """Merchant order confirmation: an order total (and usually an order id)."""
    if not re.search(r"order|receipt|purchase|your (payment|invoice)", e.subject + " " + e.body[:200], re.I):
        return None
    amt = find_amount(e.body, default_currency=e.default_currency, prefer_near=r"order total|grand total|total (?:amount|paid|charged)|amount paid|you paid")
    if amt is None:
        return None
    domain = e.sender.split("@")[-1].strip(">").split(".")[-2] if "@" in e.sender else e.sender
    order = re.search(r"order\s*(?:#|id|no\.?|number)?\s*[:#]?\s*([A-Z0-9][A-Z0-9-]{3,})", e.body, re.I)
    note = "merchant order-confirmation email" + (f", order {order.group(1)}" if order else "") + ": a payment was placed, not statement-confirmed"
    return EvidenceItem(_clean_merchant(domain).title(), amt[0], amt[1], "debit", find_date(e.body), "order_confirmation",
                        note, e.message_id)  # fmt: skip


REGISTRY: list[Pattern] = [
    Pattern("amex_safekey", "otp", re.compile(r"safekey|americanexpress|amex", re.I), _safekey),
    Pattern("card_3ds", "otp", re.compile(r"\b(hdfc|icici|axis|sbi|visa|mastercard|secure)\b", re.I), _safekey),
    Pattern("bank_alert", "alert", re.compile(r"alert|@(hdfcbank|icicibank|axisbank|sbi|kotak)", re.I), _bank_alert),
    Pattern("order_confirmation", "order_confirmation", re.compile(r"amazon|flipkart|swiggy|zomato|uber|myntra|order|receipt|billing|no-?reply", re.I), _order_confirmation),
]


def parse_email(e: ParsedEmail) -> EvidenceItem | None:
    """First registry pattern whose sender matches and that extracts an item; else None (unparsed)."""
    for pat in REGISTRY:
        if pat.sender_re.search(e.sender) or pat.sender_re.search(e.subject):
            item = pat.extract(e)
            if item is not None:
                return item
    return None


def to_transaction(item: EvidenceItem, *, source: str, fallback_date: date) -> Transaction:
    """An email EvidenceItem as a Transaction for the store (source e.g. 'gmail:amex', raw_ref the id)."""
    return Transaction(
        date=item.date or fallback_date,
        description=item.description(),
        amount=item.amount,
        currency=item.currency,
        direction=item.direction,
        balance=None,
        source=source,
        raw_ref=f"gmail:{item.message_id}",
    )


def parse_messages(messages: list[ParsedEmail]) -> tuple[list[EvidenceItem], int]:
    """Parse a batch; return the evidence items and the count that matched no pattern."""
    items, unparsed = [], 0
    for e in messages:
        item = parse_email(e)
        if item is None:
            unparsed += 1
        else:
            items.append(item)
    return items, unparsed
