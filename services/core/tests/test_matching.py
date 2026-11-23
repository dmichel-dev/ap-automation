"""Matching rules (pure functions). Plaid sign: positive amount = money out of the account."""

from datetime import date
from decimal import Decimal as D

import pytest

from app.matching import BankTx, OpenBill, Rules, decide, evaluate, invoice_number_in, name_similarity

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
