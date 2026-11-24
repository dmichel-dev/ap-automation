"""Matching rules (pure functions). Plaid sign: positive amount = money out of the account."""

from datetime import date
from decimal import Decimal as D

import pytest
from app.fx import payment_rate
from app.matching import (
    BankTx,
    FxRate,
    OpenBill,
    Rules,
    decide,
    evaluate,
    invoice_number_in,
    name_similarity,
)

RULES = Rules()
BILL = OpenBill("inv-1", "USD", D("641.24"), date(2026, 11, 18), date(2026, 12, 18), "quillmoor paper supply", "QM1042")


def tx(amount="641.24", day=date(2026, 11, 27), name="QUILLMOOR PAPER SUP ACH PPD", tid="t1", **kw):
    return BankTx(tid, D(amount), kw.pop("currency", "USD"), day, name, **kw)


# --- name ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "bank_text, expected",
    [
        ("QUILLMOOR PAPER SUP ACH PPD", 1.0),  # truncated bank descriptor
        ("Quillmoor Paper Supply LLC", 1.0),
        ("ACH PAYMENT QUILLMOR PAPER", 0.6667),  # typo + missing word
        ("AMAZON MKTPLACE PMTS", 0.0),
        ("CHECK 1042", 0.0),
    ],
)
def test_name_similarity(bank_text, expected):
    assert name_similarity("quillmoor paper supply", bank_text) == pytest.approx(expected, abs=1e-4)


def test_name_similarity_ignores_and_and_legal_forms():
    assert name_similarity("kranich and sohne feinmechanik", "KRANICH SOHNE FEINMECHANIK GMBH") == 1.0


@pytest.mark.parametrize(
    "text, found",
    [("PAYMENT QM-1042 THANK YOU", True), ("PAYMENT QM 1042", True), ("PAYMENT QM-1043", False), (None, False)],
)
def test_invoice_number_in(text, found):
    assert invoice_number_in("QM1042", text) is found


def test_short_invoice_numbers_are_not_evidence():
    assert not invoice_number_in("42", "STORE 42 PURCHASE")


# --- amount -------------------------------------------------------------------------
@pytest.mark.parametrize(
    "amount, ok",
    [
        ("641.24", True),
        ("641.64", True),
        ("640.84", True),  # ±0.40 within min($1.00, 0.5 % = $3.21)
        ("642.25", False),
        ("640.20", False),  # > $1.00
        ("641.2400001", True),  # Plaid sends more than 2 decimals
    ],
)
def test_amount_tolerance(amount, ok):
    assert evaluate(BILL, tx(amount), RULES).amount_ok is ok


def test_percentage_tolerance_applies_to_small_bills():
    small = OpenBill("inv-2", "USD", D("100.00"), date(2026, 11, 18), None, "quillmoor paper supply", None)
    assert evaluate(small, tx("100.50"), RULES).amount_ok  # 0.5 % of 100 = 0.50
    assert not evaluate(small, tx("100.60"), RULES).amount_ok


def test_difference_is_bank_minus_bill():
    assert evaluate(BILL, tx("641.64"), RULES).difference == D("0.40")
    assert evaluate(BILL, tx("640.84"), RULES).difference == D("-0.40")


# --- direction, status, currency ---------------------------------------------------
def test_incoming_money_is_never_a_payment():
    assert not evaluate(BILL, tx("-641.24"), RULES).amount_ok  # negative = money in


def test_pending_transactions_are_ignored():
    assert not evaluate(BILL, tx(pending=True), RULES).candidate


def test_other_currency_is_ignored():
    assert not evaluate(BILL, tx(currency="EUR"), RULES).candidate


# --- date ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "day, ok",
    [
        (date(2026, 11, 15), True),  # 3 days before the invoice
        (date(2026, 11, 14), False),  # 4 days before
        (date(2027, 1, 17), True),  # due date + 30
        (date(2027, 1, 18), False),
    ],
)
def test_date_window(day, ok):
    assert evaluate(BILL, tx(day=day), RULES).date_ok is ok


def test_without_due_date_default_terms_apply():
    bill = OpenBill("inv-3", "USD", D("641.24"), date(2026, 11, 18), None, "quillmoor paper supply", None)
    assert evaluate(bill, tx(day=date(2027, 1, 17)), RULES).date_ok  # +30 terms +30
    assert not evaluate(bill, tx(day=date(2027, 1, 18)), RULES).date_ok


def test_invoice_number_can_replace_the_name():
    ev = evaluate(BILL, tx(name="ACH PAYMENT REF QM-1042"), RULES)
    assert ev.name_similarity == 0 and ev.invoice_number_found and ev.candidate


# --- decision -----------------------------------------------------------------------
def test_unique_pair_is_accepted():
    d = decide([BILL], [tx(), tx("12.00", tid="t2", name="COFFEE")], RULES)
    assert [(b.invoice_id, t.transaction_id) for b, t, _ in d.accepted] == [("inv-1", "t1")]


def test_two_possible_transactions_are_ambiguous():
    d = decide([BILL], [tx(tid="t1"), tx(tid="t2", day=date(2026, 11, 28))], RULES)
    assert not d.accepted and len(d.ambiguous["inv-1"]) == 2


def test_one_transaction_for_two_bills_is_ambiguous_for_both():
    other = OpenBill("inv-2", "USD", D("641.24"), date(2026, 11, 19), None, "quillmoor paper supply", "QM1043")
    d = decide([BILL, other], [tx()], RULES)
    assert not d.accepted and set(d.ambiguous) == {"inv-1", "inv-2"}


def test_no_candidate_is_unmatched():
    assert decide([BILL], [tx("12.00", name="COFFEE")], RULES).unmatched == ["inv-1"]


# --- bills in another currency than the bank account ---------------------------------
EUR_BILL = OpenBill(
    "inv-eur", "EUR", D("1598.61"), date(2026, 11, 20), date(2026, 12, 20), "ateliers verlibreux", "AV20260147"
)
ECB_1123 = FxRate(D("1.0939"), date(2026, 11, 23))  # 1 EUR = 1.0939 USD


def eur_tx(amount, day=date(2026, 11, 24), tid="t-eur"):
    return tx(amount, day=day, name="ATELIERS VERLIBREUX SEPA", tid=tid)


def rates_from(*known: FxRate):
    """A RateLookup over EUR→USD rates: the latest on or before the day."""

    def lookup(base, quote, day):
        found = [r for r in known if base == "EUR" and quote == "USD" and r.rate_date <= day]
        return max(found, key=lambda r: r.rate_date) if found else None

    return lookup


def test_eur_bill_matches_usd_payment_within_fx_tolerance():
    # 1598.61 EUR × 1.0939 = 1748.72 USD expected; bank 1760.00 is +0.65 %
    ev = evaluate(EUR_BILL, eur_tx("1760.00"), RULES, ECB_1123)
    assert ev.candidate and ev.fx_status == "priced"
    assert ev.expected_bank_amount == D("1748.72") and ev.difference == D("11.28") and ev.bank_currency == "USD"
    decision = decide([EUR_BILL], [eur_tx("1760.00")], RULES, rates=rates_from(ECB_1123))
    assert [(b.invoice_id, t.transaction_id) for b, t, _ in decision.accepted] == [("inv-eur", "t-eur")]


@pytest.mark.parametrize("amount, ok", [("1801.18", True), ("1696.26", True), ("1801.19", False), ("1696.25", False)])
def test_fx_tolerance_is_three_percent_of_the_converted_bill(amount, ok):
    # 3 % of 1748.72 = 52.46: accepted from 1696.26 to 1801.18
    assert evaluate(EUR_BILL, eur_tx(amount), RULES, ECB_1123).amount_ok is ok


def test_eur_payment_outside_tolerance_goes_to_a_person_with_expected_and_actual():
    decision = decide([EUR_BILL], [eur_tx("1900.00")], RULES, rates=rates_from(ECB_1123))
    assert decision.accepted == [] and decision.unmatched == []
    code, found = decision.fx_review["inv-eur"]
    assert code == "fx_amount_difference" and [t.transaction_id for t, _ in found] == ["t-eur"]
    ev = found[0][1]
    assert (ev.bank_amount, ev.expected_bank_amount, ev.difference) == (D("1900.00"), D("1748.72"), D("151.28"))


def test_currency_without_a_rate_goes_to_a_person():
    aed = OpenBill("inv-aed", "AED", D("5000.00"), date(2026, 11, 20), None, "ateliers verlibreux", None)
    decision = decide([aed], [eur_tx("1361.47")], RULES, rates=rates_from(ECB_1123))
    code, found = decision.fx_review["inv-aed"]
    assert code == "fx_rate_unavailable" and found[0][1].fx_status == "no_rate" and found[0][1].difference is None


def test_rate_more_than_four_days_older_than_the_transaction_is_not_used():
    old = FxRate(D("1.0939"), date(2026, 11, 19))
    assert evaluate(EUR_BILL, eur_tx("1748.72", day=date(2026, 11, 23)), RULES, old).fx_status == "priced"
    stale = evaluate(EUR_BILL, eur_tx("1748.72", day=date(2026, 11, 24)), RULES, old)
    assert stale.fx_status == "stale_rate" and not stale.amount_ok
    assert decide([EUR_BILL], [eur_tx("1748.72")], RULES, rates=rates_from(old)).fx_review["inv-eur"][0] == (
        "fx_rate_unavailable"
    )


def test_foreign_bill_without_a_fitting_transaction_is_unmatched():
    other = tx("1760.00", name="AMAZON MKTPLACE PMTS", tid="t-amz")
    assert decide([EUR_BILL], [other], RULES, rates=rates_from(ECB_1123)).unmatched == ["inv-eur"]


def test_usd_payment_fitting_a_usd_and_an_eur_bill_is_ambiguous():
    usd = OpenBill("inv-usd", "USD", D("1760.00"), date(2026, 11, 20), None, "ateliers verlibreux", None)
    decision = decide([EUR_BILL, usd], [eur_tx("1760.00")], RULES, rates=rates_from(ECB_1123))
    assert decision.accepted == [] and set(decision.ambiguous) == {"inv-eur", "inv-usd"}


def test_a_transaction_another_bill_can_take_is_not_proposed_for_review():
    usd = OpenBill("inv-usd", "USD", D("1900.00"), date(2026, 11, 20), None, "ateliers verlibreux", None)
    decision = decide([EUR_BILL, usd], [eur_tx("1900.00")], RULES, rates=rates_from(ECB_1123))
    assert [b.invoice_id for b, _, _ in decision.accepted] == ["inv-usd"]
    assert decision.fx_review == {} and decision.unmatched == ["inv-eur"]


def test_same_currency_rules_ignore_rates():
    ev = evaluate(BILL, tx("641.24"), RULES, ECB_1123)
    assert ev.candidate and ev.fx_status == "same_currency" and ev.expected_bank_amount is None


# --- the rate a bill payment carries ------------------------------------------------
@pytest.mark.parametrize(
    "bill, bank, rate, booked",
    [
        ("100.00", "115.00", "1.1500000", "115.00"),
        ("1598.61", "1760.00", "1.1009565", "1760.00"),  # QuickBooks kept 1.1009565 and credited 1760.00 (live)
        ("18135", "114.40", "0.0063082", "114.40"),  # JPY: 0 decimals on the bill side
        ("99999.99", "110012.34", "1.1001235", "110012.34"),  # largest bill that is always exact
    ],
)
def test_payment_rate_books_the_exact_bank_amount(bill, bank, rate, booked):
    pr = payment_rate(D(bill), D(bank), "USD")
    assert (pr.rate, pr.booked, pr.gap) == (D(rate), D(booked), 0)


def test_payment_rate_reports_the_gap_quickbooks_precision_leaves():
    # 2,345,678 JPY: one rate step (1e-7) moves the booked amount by 0.23 USD, so some bank amounts
    # can't be booked exactly; the nearest rate leaves at most half a step
    assert payment_rate(D("2345678"), D("14801.23"), "USD").gap == 0
    pr = payment_rate(D("2345678"), D("14801.00"), "USD")
    assert (pr.rate, pr.booked, pr.gap) == (D("0.0063099"), D("14800.99"), D("-0.01"))
