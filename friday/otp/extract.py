"""Pick the one-time code out of an email or SMS body.

Scores every 4-8 digit (or short alphanumeric) token by its surroundings and
rejects things that look like amounts, years, dates, phone/account fragments.
"""

from __future__ import annotations

import html
import re

KEYWORDS = re.compile(
    r"(otp|one[- ]?time|passcode|pass code|verification|verify|security code|auth(entication)? code|login code|"
    r"sign[- ]?in code|\bcode\b|\bpin\b|\btan\b)",
    re.IGNORECASE,
)
NEGATIVE_BEFORE = re.compile(
    r"(rs\.?|inr|usd|eur|gbp|₹|\$|€|£|amount|amt|balance|bal|a/c|acct|account|card|ending|ends with|xx+|\*+|no\.?|number|"
    r"ref(erence)?|txn|transaction|order|invoice|ticket|id|tel|phone|call|mobile|\+)\s*[:#.]?\s*$",
    re.IGNORECASE,
)
CANDIDATE = re.compile(r"(?<![\w.,/:-])(\d{4,8}|[A-Z0-9]{3}-?[A-Z0-9]{3}|G-\d{6})(?![\w.,/:]\d|\w)")


def html_to_text(body: str) -> str:
    body = re.sub(r"(?is)<(script|style|head)[^>]*>.*?</\1>", " ", body)
    body = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>|</td>", "\n", body)
    body = re.sub(r"<[^>]+>", " ", body)
    return html.unescape(body)


def extract_otp(text: str, *, min_len: int = 4, max_len: int = 8) -> str | None:
    if "<" in text and ">" in text:
        text = html_to_text(text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    best: tuple[int, str] | None = None
    for m in CANDIDATE.finditer(text):
        token = m.group(1)
        code = token[2:] if token.startswith("G-") else token
        digits_only = code.isdigit()
        if digits_only and not (min_len <= len(code) <= max_len):
            continue
        if not digits_only and not any(c.isdigit() for c in code):
            continue  # all-letter words like "ABC-DEF"
        before = text[max(0, m.start() - 60) : m.start()]
        after = text[m.end() : m.end() + 40]
        score = 0
        if KEYWORDS.search(before[-45:]):
            score += 5
        if re.match(r"\s*(is|as) (your|the)\b", after, re.I) and KEYWORDS.search(after):
            score += 5
        if NEGATIVE_BEFORE.search(before):
            score -= 6
        if digits_only and len(code) == 4 and re.fullmatch(r"(19|20)\d\d", code):
            score -= 4
        if re.match(r"\s*(/|-)\s*\d", after) or re.search(r"\d\s*(/|-)\s*$", before):
            score -= 5  # dates, ranges
        if re.match(r"\s*(am|pm|hrs|mins?|minutes|seconds|secs|days)\b", after, re.I):
            score -= 4
        if digits_only and len(code) == 6:
            score += 1
        if not digits_only:
            score -= 1
        if score > 0 and (best is None or score > best[0]):
            best = (score, code.replace("-", "") if not digits_only and "-" in code and len(code) == 7 else code)
    return best[1] if best else None
