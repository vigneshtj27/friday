"""Part 1.2/1.3: the reconciliation engine (friday/reconcile_sheet.py) - pure, against the owner's
A-I expense layout: No / Date / Narration / (blank) / (blank) / Debit / Credit / Comments / Bank."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from friday.reconcile_sheet import PENDING_MARK, PortalTxn, reconcile

HEADER = ["No", "Date", "Narration", "", "", "Debit", "Credit", "Comments", "Bank"]
START, END = date(2026, 10, 1), date(2026, 10, 31)


def _sheet(*rows: list[str]) -> list[list[str]]:
    return [list(HEADER), *[list(r) for r in rows]]


def _row(no, d, narr, debit="", credit="", comment="", bank="Amex"):
    return [str(no), d, narr, "", "", str(debit) if debit else "", str(credit) if credit else "", comment, bank]


def _p(d, narr, amount, direction="debit", status="posted", txn_id=None):
    return PortalTxn(date=d, description=narr, amount=Decimal(str(amount)), direction=direction,
                     status=status, txn_id=txn_id)  # fmt: skip


# a layout with a Txn ID column (J), for reliable-identity tests
HEADER_TID = ["No", "Date", "Narration", "", "", "Debit", "Credit", "Comments", "Bank", "Txn ID"]


def _sheet_tid(*rows):
    return [list(HEADER_TID), *[list(r) for r in rows]]


def _row_tid(no, d, narr, debit="", credit="", comment="", bank="Amex", txnid=""):
    return [str(no), d, narr, "", "", str(debit) if debit else "", str(credit) if credit else "", comment, bank, txnid]


def _run(values, portal):
    return reconcile(values, portal, source_label="Amex", start=START, end=END)


# ---- append / dupe / pending ---------------------------------------------------------------------


def test_new_posted_charge_is_appended():
    plan = _run(_sheet(_row(1, "05/10/2026", "Blue Mart groceries", debit="500")),
                [_p(date(2026, 10, 9), "Kettle Co", 300)])  # fmt: skip
    assert len(plan.appends) == 1 and not plan.settles and plan.duplicates == 0
    assert len(plan.new_data_rows) == 2  # one existing + one appended


def test_duplicate_skipped_only_on_a_matching_transaction_id():
    # reliable identity: the sheet row carries the same txn id -> a true duplicate, skipped
    plan = _run(_sheet_tid(_row_tid(1, "05/10/2026", "Blue Mart groceries", debit="500", txnid="TXN1")),
                [_p(date(2026, 10, 5), "Blue Mart groceries", 500, txn_id="TXN1")])  # fmt: skip
    assert plan.duplicates == 1 and not plan.appends and not plan.settles


def test_lookalike_without_an_id_is_entered_and_flagged_not_silently_skipped():
    # 1.2: same day, amount and merchant but NO reliable id -> two real charges can look alike, so enter
    # the charge and flag the pair; never a silent skip on similarity.
    plan = _run(_sheet(_row(1, "05/10/2026", "Blue Mart groceries", debit="500")),
                [_p(date(2026, 10, 5), "Blue Mart groceries", 500)])  # fmt: skip
    assert plan.duplicates == 0 and len(plan.appends) == 1
    assert len(plan.possible_duplicates) == 1 and plan.possible_duplicates[0]["candidate_rows"] == [1]


def test_two_portal_charges_sharing_an_id_enter_only_once():
    # rows from the same fetch batch must not double-enter one charge, but must not collapse two real ones
    plan = _run(_sheet(_row(1, "01/10/2026", "Seed", debit="10")),
                [_p(date(2026, 10, 9), "Kettle Co", 300, txn_id="K9"),
                 _p(date(2026, 10, 9), "Kettle Co", 300, txn_id="K9")])  # fmt: skip
    assert len(plan.appends) == 1 and plan.duplicates == 1


def test_two_real_same_day_same_amount_charges_both_enter():
    plan = _run(_sheet(_row(1, "01/10/2026", "Seed", debit="10")),
                [_p(date(2026, 10, 9), "Kettle Co", 300, txn_id="A1"),
                 _p(date(2026, 10, 9), "Kettle Co", 300, txn_id="A2")])  # fmt: skip
    assert len(plan.appends) == 2 and plan.duplicates == 0  # distinct ids -> two charges


# ---- 1.1 date window -----------------------------------------------------------------------------


def test_charges_outside_the_window_are_excluded_and_counted():
    plan = _run(_sheet(_row(1, "05/10/2026", "Seed", debit="10")),
                [_p(date(2026, 9, 28), "Before window", 100),      # out
                 _p(date(2026, 11, 2), "After window", 200),       # out
                 _p(date(2026, 10, 15), "In window", 300)])        # in
    assert plan.out_of_window == 2 and len(plan.appends) == 1
    assert "In window" in plan.appends[0][2]


# ---- 1.3 pending/posted double-counting, both directions ----------------------------------------


def test_portal_pending_matching_an_already_posted_row_is_not_double_entered():
    # 1.3(a): the sheet already has the posted row; the portal still lists it pending -> flag, don't add
    plan = _run(
        _sheet(_row(1, "10/10/2026", "Hotel Zurich", debit="730.85")),  # already posted earlier
        [_p(date(2026, 10, 10), "Hotel Zurich", "730.85", status="pending")],
    )
    assert not plan.appends and plan.ambiguous
    assert "posted row may already be it" in plan.ambiguous[0]["reason"]


def test_posted_charge_that_renarrates_a_pending_row_is_flagged_not_doubled():
    # 1.3(b): a pending row, same amount, but the bank renarrated it so no token overlaps -> unresolved,
    # enter nothing, and the stale pending row is left intact (not deleted, not auto-merged)
    plan = _run(
        _sheet(_row(1, "10/10/2026", f"POS 4471 AUTH {PENDING_MARK}", debit="500")),
        [_p(date(2026, 10, 12), "Blue Mart Groceries", 500)],
    )
    assert not plan.appends and not plan.settles and plan.ambiguous
    assert "unresolved" in plan.ambiguous[0]["reason"]


def test_posted_charge_amount_beyond_fx_but_related_to_a_pending_row_is_flagged():
    # same merchant, amount moved far beyond the 3% FX tolerance (a tip / partial) -> flag, enter nothing
    plan = _run(
        _sheet(_row(1, "10/10/2026", f"Cafe Mocha {PENDING_MARK}", debit="500")),
        [_p(date(2026, 10, 11), "Cafe Mocha", 560)],  # +12%, beyond FX-close, within related band
    )
    assert not plan.appends and not plan.settles and plan.ambiguous
    assert "beyond FX" in plan.ambiguous[0]["reason"]


def test_pending_posts_on_a_matching_id_even_when_narration_changed():
    # reliable identity settles across a narration change the heuristic would miss
    plan = reconcile(
        _sheet_tid(_row_tid(1, "10/10/2026", f"POS 4471 {PENDING_MARK}", debit="500", txnid="T7")),
        [_p(date(2026, 10, 12), "Blue Mart Groceries", 500, txn_id="T7")],
        source_label="Amex", start=START, end=END,
    )
    assert len(plan.settles) == 1 and not plan.appends


def test_pending_charge_entered_with_marker_and_amount():
    plan = _run(_sheet(_row(1, "05/10/2026", "Blue Mart", debit="500")),
                [_p(date(2026, 10, 10), "Cafe Mocha", 222, status="pending")])  # fmt: skip
    assert len(plan.appends) == 1
    appended = plan.appends[0]
    assert PENDING_MARK in appended[2] and appended[5] == "222"  # marker in narration, amount in Debit
    assert plan.provisional_total == Decimal(222)  # counted provisionally, separate from posted
    assert plan.posted_total == Decimal(500)


# ---- settle: update in place, never a second row -------------------------------------------------


def test_settle_narration_only_change_updates_the_row_no_double_count():
    plan = _run(
        _sheet(_row(1, "05/10/2026", "Blue Mart", debit="500"),
               _row(2, "10/10/2026", f"Cafe Mocha {PENDING_MARK}", debit="222", comment="lunch")),
        [_p(date(2026, 10, 10), "Cafe Mocha", 222)],
    )
    assert len(plan.settles) == 1 and not plan.appends  # updated in place, no new row
    s = plan.settles[0]
    assert PENDING_MARK in s.before[2] and PENDING_MARK not in s.after[2]
    assert s.after[7] == "lunch"  # the Comment travelled untouched
    assert len(plan.new_data_rows) == 2  # no double count
    assert plan.provisional_total == Decimal(0) and plan.posted_total == Decimal(722)


def test_settle_with_date_and_fx_amount_correction_shows_the_diff():
    plan = _run(
        _sheet(_row(1, "10/10/2026", f"Hotel Zurich {PENDING_MARK}", debit="730.85")),
        [_p(date(2026, 10, 12), "Hotel Zurich", "745.20")],  # posted later, FX-adjusted total
    )
    assert len(plan.settles) == 1
    changes = plan.settles[0].changes
    assert "Date" in changes and changes["Date"][0] != changes["Date"][1]
    assert "Debit" in changes and changes["Debit"] == ("730.85", "745.20")
    assert plan.provisional_total == Decimal(0) and plan.posted_total == Decimal("745.20")


# ---- ambiguity is never auto-settled ------------------------------------------------------------


def test_two_same_day_same_amount_rows_are_ambiguous_not_settled():
    plan = _run(
        _sheet(_row(1, "10/10/2026", f"Store A {PENDING_MARK}", debit="199"),
               _row(2, "10/10/2026", f"Store B {PENDING_MARK}", debit="199")),
        [_p(date(2026, 10, 10), "Store A", 199)],
    )
    assert not plan.settles and plan.ambiguous and sorted(plan.ambiguous[0]["candidate_rows"]) == [1, 2]


# ---- credit / refund ----------------------------------------------------------------------------


def test_refund_is_entered_in_the_credit_column():
    plan = _run(_sheet(_row(1, "05/10/2026", "Blue Mart", debit="500")),
                [_p(date(2026, 10, 8), "Blue Mart refund", 500, direction="credit")])  # fmt: skip
    assert len(plan.appends) == 1 and plan.appends[0][6] == "500" and plan.appends[0][5] == ""


# ---- sort, Comments preserved, renumber ---------------------------------------------------------


def test_sorted_by_date_with_comments_preserved_and_no_renumbered():
    plan = _run(
        _sheet(_row(1, "20/10/2026", "Later charge", debit="100", comment="keep-later"),
               _row(2, "05/10/2026", "Earlier charge", debit="200", comment="keep-earlier")),
        [_p(date(2026, 10, 10), "Mid charge", 300)],
    )
    rows = plan.new_data_rows
    assert [r[2] for r in rows] == ["Earlier charge", "Mid charge", "Later charge"]  # date ascending
    assert [r[0] for r in rows] == ["1", "2", "3"]  # No renumbered after sort
    # Comments stayed with their own rows
    assert rows[0][7] == "keep-earlier" and rows[2][7] == "keep-later" and rows[1][7] == ""


# ---- narration clipping -------------------------------------------------------------------------


def test_long_narration_is_clipped_to_the_sheet_convention():
    plan = _run(_sheet(_row(1, "05/10/2026", "Short", debit="500")),
                [_p(date(2026, 10, 9), "X" * 300, 300)])  # fmt: skip
    assert len(plan.appends[0][2]) <= 120


# ---- absent: never inferred settled -------------------------------------------------------------


def test_pending_row_the_portal_no_longer_shows_is_reported_absent():
    plan = _run(
        _sheet(_row(1, "08/10/2026", f"Ghost charge {PENDING_MARK}", debit="150")),
        [_p(date(2026, 10, 9), "Something else", 300)],  # portal does not show the pending charge at all
    )
    assert plan.absent and plan.absent[0]["narration"].startswith("Ghost charge")
    assert not plan.settles  # never settled on absence
    assert plan.provisional_total == Decimal(150)  # still carried as pending, not dropped


# ---- layout we can't map pauses the run ---------------------------------------------------------


def test_unmappable_layout_pauses_with_a_conflict():
    plan = reconcile([["foo", "bar"], ["1", "2"]], [_p(date(2026, 10, 9), "X", 10)],
                     source_label="Amex", start=START, end=END)  # fmt: skip
    assert plan.conflicts and not plan.new_data_rows and not plan.appends
