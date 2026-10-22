from __future__ import annotations

from datetime import date
from decimal import Decimal

from friday.capabilities.mail_evidence import ParsedEmail, parse_email, parse_messages


def pe(sender, subject, body, mid="m1", cur="INR"):
    return ParsedEmail(sender=sender, subject=subject, body=body, message_id=mid, default_currency=cur)


SAFEKEY = pe(
    "AmericanExpress@welcome.americanexpress.com",
    "Your American Express SafeKey one-time verification code",
    "Dear Cardmember, Your one-time verification code for your purchase of INR 4,599.00 at AMAZON on "
    "20 Oct 2026 is 582913. Do not share this code with anyone.",
    "sk1",
)
BANK_DEBIT = pe(
    "alerts@hdfcbank.net",
    "Transaction Alert on your HDFC Bank Card",
    "Rs.1,234.00 was spent on your HDFC Bank Credit Card ending 1234 at FLIPKART INTERNET on 19-10-2026. "
    "Not you? Call us.",
    "bd1",
)
BANK_CREDIT = pe(
    "alerts@hdfcbank.net",
    "Amount credited",
    "Rs.500.00 has been credited to your account at BRANCH on 18-10-2026 as a refund.",
    "bc1",
)
ORDER = pe(
    "auto-confirm@amazon.in",
    "Your Amazon.in order confirmation",
    "Thank you for your order. Order #403-1234567-7654321. Order Total: INR 2,100.00. Arriving soon.",
    "o1",
)


def test_safekey_otp():
    item = parse_email(SAFEKEY)
    assert item is not None
    assert item.kind == "otp" and item.direction == "debit"
    assert item.amount == Decimal("4599.00") and item.currency == "INR"
    assert item.merchant.upper().startswith("AMAZON") and item.date == date(2026, 10, 20)
    assert "582913" not in item.description() and "may not" in item.confidence_note.lower()


def test_bank_debit_alert():
    item = parse_email(BANK_DEBIT)
    assert item.kind == "alert" and item.direction == "debit"
    assert item.amount == Decimal("1234.00") and item.date == date(2026, 10, 19)
    assert "FLIPKART" in item.merchant.upper()


def test_bank_credit_alert():
    item = parse_email(BANK_CREDIT)
    assert item.kind == "alert" and item.direction == "credit"
    assert item.amount == Decimal("500.00")


def test_order_confirmation():
    item = parse_email(ORDER)
    assert item.kind == "order_confirmation" and item.direction == "debit"
    assert item.amount == Decimal("2100.00") and item.currency == "INR"
    assert "amazon" in item.merchant.lower() and "403-1234567-7654321" in item.confidence_note


def test_marketing_mail_is_not_evidence():
    spam = pe("deals@amazon.in", "Big Billion Days are here!", "Up to 80% off! Shop now. No amount here, just hype.")
    assert parse_email(spam) is None


def test_otp_without_amount_is_skipped():
    noamt = pe("AmericanExpress@welcome.americanexpress.com", "Your verification code",
               "Your one-time verification code is 123456. Do not share it.")
    assert parse_email(noamt) is None


def test_unknown_sender_is_skipped():
    other = pe("hello@randomnewsletter.com", "Weekly roundup", "You spent a lot of time reading us. Rs.999 of value!")
    assert parse_email(other) is None


def test_usd_currency_amount():
    o = pe("receipts@uber.com", "Your Tuesday evening trip receipt",
           "Thanks for riding. Total charged $23.45 to your card on Oct 20, 2026.", "u1", cur="USD")
    item = parse_email(o)
    assert item.amount == Decimal("23.45") and item.currency == "USD"


def test_parse_messages_counts_unparsed():
    items, unparsed = parse_messages([SAFEKEY, BANK_DEBIT, pe("x@y.com", "hi", "nothing here")])
    assert len(items) == 2 and unparsed == 1
