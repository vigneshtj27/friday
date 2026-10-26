"""Part 1.1/1.3: the Amex (India) profile and READ-ONLY extraction - dashboard summary and the
posted/pending activity sections, parsed from Amex-shaped page text (no live site, no secrets)."""

from __future__ import annotations

from datetime import date

from friday.capabilities.amex import (
    AMEX_PROFILE,
    amex_site,
    parse_activity,
    parse_dashboard,
)

# A dashboard shaped like the verified one (the Oct 26 reference figures), labels on their own lines.
DASHBOARD = """
American Express Dashboard
Statement balance: INR 8,27,020.53
Pending charges: INR 730.85
Available credit: INR 0.00
Payment due date: 17 Nov 2026
Minimum amount due: INR 71,883.00
Membership Rewards points: 48,640
"""


def test_profile_is_read_only_and_names_no_secret_value():
    assert AMEX_PROFILE["remember_device"] is False  # never enable device memory
    assert "read-and-reconcile only" in AMEX_PROFILE["scope"]
    assert AMEX_PROFILE["otp"]["channel"] == "email"
    # credentials are referenced by vault item NAME only - no secret value anywhere in the profile
    blob = repr(AMEX_PROFILE)
    assert "amex.me/username" in blob and "amex.me/password" in blob
    assert "password" not in {k.lower() for k in ()} and "sk-" not in blob


def test_site_record_has_the_login_and_otp_sender():
    site = amex_site()
    assert site.id == "amex-in" and "americanexpress.com" in site.domains
    assert AMEX_PROFILE["otp"]["sender"] in site.otp_senders


def test_dashboard_summary_keeps_posted_and_pending_separate():
    s = parse_dashboard(DASHBOARD)
    assert s["posted_balance"] == "827020.53" and s["pending_auths"] == "730.85"  # separate fields
    assert s["available_credit"] == "0.00" and s["minimum_due"] == "71883.00"
    assert s["due_date"] == "2026-11-17" and s["mr_balance"] == "48640"


def test_activity_posted_and_pending_sections_become_distinct_statuses():
    posted = [["05 Oct 2026", "Blue Mart Groceries", "500.00"],
              ["09 Oct 2026", "Refund Kettle Co", "-300.00"]]
    pending = [["15 Oct 2026", "Cafe Mocha", "222.00"],
               ["16 Oct 2026", "Toll Plaza", "508.85"]]
    txns = parse_activity(posted, pending)
    by_status = {}
    for t in txns:
        by_status.setdefault(t.status, []).append(t)
    assert len(by_status["posted"]) == 2 and len(by_status["pending"]) == 2
    refund = next(t for t in by_status["posted"] if "Kettle" in t.description)
    assert refund.direction == "credit"  # the minus sign makes it a refund
    mocha = next(t for t in by_status["pending"] if "Mocha" in t.description)
    assert mocha.date == date(2026, 10, 15) and str(mocha.amount) == "222.00"


def test_register_amex_adds_the_site(ctx):
    from friday.capabilities.amex import register_amex

    register_amex(ctx)
    assert ctx.sites.get("amex-in").name == "American Express India"
