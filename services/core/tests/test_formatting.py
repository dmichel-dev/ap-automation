"""What a person reads: money, scores, dates and reason messages (app/formatting.py, web/display.py)."""

from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from app import formatting
from app.web import display


@pytest.mark.parametrize(
    "value, currency, shown",
    [
        ("1598.6100", "EUR", "1,598.61 EUR"),  # the database's 4 decimals: 2 shown
        (Decimal("0.125"), "USD", "0.13 USD"),  # halves rounded up
        ("2345678.00", "JPY", "2,345,678 JPY"),  # a currency without decimals keeps its format
        ("12.3456", "KWD", "12.346 KWD"),  # and one with three
        ("-0.01", "USD", "-0.01 USD"),
        ("865.13", None, "865.13"),  # no currency known: 2 decimals, no code
        (None, "EUR", "–"),
    ],
)
def test_money_has_the_currency_s_usual_decimals(value, currency, shown):
    assert formatting.money(value, currency) == shown


@pytest.mark.parametrize(
    "score, shown",
    [
        (0.9775, "97%"),
        ("0.9775", "97%"),
        (0.973, "97%"),
        (0.9999, "99%"),
        ("0.8499", "84%"),
        (0.85, "85%"),
        (1, "100%"),
        (0, "0%"),
        (None, "–"),
    ],
)
def test_scores_are_whole_percentages_rounded_down(score, shown):
    assert formatting.percent(score) == shown


def test_dates_read_one_way_only():
    assert formatting.day(date(2026, 11, 26)) == "26 Nov 2026"
    assert formatting.day("2026-03-04") == "4 Mar 2026"  # never 03/04 or 04/03
    assert formatting.day(None) == "–" and formatting.day("not a date") == "not a date"
    paris = timezone(timedelta(hours=1))
    assert formatting.moment(datetime(2026, 11, 26, 15, 5, tzinfo=paris)) == "26 Nov 2026, 14:05"  # in UTC
    assert formatting.moment("2026-11-26T14:05:31.123456+00:00") == "26 Nov 2026, 14:05"
    assert formatting.moment(datetime(2026, 1, 2, 0, 0, tzinfo=UTC)) == "2 Jan 2026, 00:00"


def test_a_mismatch_shows_both_numbers_and_the_gap():
    assert (
        formatting.mismatch("total", Decimal("875.13"), Decimal("865.13"), "EUR")
        == "Total (875.13 EUR) doesn't match lines + adjustments + tax (865.13 EUR): 10.00 EUR off."
    )
    assert (
        formatting.mismatch("lines", Decimal("100"), Decimal("120.5"), None)
        == "Subtotal (100.00) doesn't match the sum of the lines (120.50): 20.50 off."
    )


@pytest.mark.parametrize(
    "stored, currency, shown",
    [
        (
            "total 875.13 ≠ lines + adjustments + tax 865.13",
            "EUR",
            "Total (875.13 EUR) doesn't match lines + adjustments + tax (865.13 EUR): 10.00 EUR off.",
        ),
        (
            "subtotal 1000.0000 ≠ sum of lines 990.00",
            None,
            "Subtotal (1,000.00) doesn't match the sum of the lines (990.00): 10.00 off.",
        ),
        (
            "tax total 19.00 ≠ sum of tax lines 18.05",
            "EUR",
            "Tax total (19.00 EUR) doesn't match the sum of the tax lines (18.05 EUR): 0.95 EUR off.",
        ),
        ("confidence 0.7123 below threshold 0.85", None, "Confidence (71%) is below the review threshold (85%)."),
        (
            "due date 2026-10-01 is before invoice date 2026-10-15",
            None,
            "due date 1 Oct 2026 is before invoice date 15 Oct 2026",
        ),
        ("first invoice from this supplier", None, "first invoice from this supplier"),
        (None, None, ""),
    ],
)
def test_messages_stored_in_the_old_wording_are_shown_in_the_new_one(stored, currency, shown):
    assert display.reason_detail(stored, currency) == shown


def test_each_status_has_one_label_and_one_colour():
    tones = {"ready", "review", "fraud", "closed"}
    assert all(s.tone in tones for s in display.STATUSES.values())
    assert display.STATUSES["needs_review"] == display.STATUSES["open"] == ("Needs review", "review")
    assert display.STATUSES["rejected"].tone == "closed" and display.STATUSES["fraud"].tone == "fraud"
    item = {"status": "open", "actions": ["verify", "reject"]}
    assert (display.status_label(item), display.tone(item)) == ("Possible fraud", "fraud")
