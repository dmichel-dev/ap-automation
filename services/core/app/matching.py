"""Deterministic bank matching: which outgoing bank transaction pays which open bill.

Pure functions (no I/O) so every rule is unit-tested; the database side is in matching_db.py.

A pair (bill, transaction) is a candidate when all three rules pass:
  amount  |bank − bill| ≤ min(absolute tolerance, percentage × bill)    (bank rounded to minor units)
  date    invoice date − N days ≤ transaction date ≤ due date (or invoice date + terms) + M days
  name    supplier-name similarity ≥ threshold, OR the invoice number appears in the bank text

A bill in another currency than the bank account is compared through a reference rate of the
transaction date (1 bill currency = rate bank currency, the latest published on or before that date):
  amount  |bank − bill × rate| ≤ FX percentage × (bill × rate)    (no absolute cap: rates move)
  rate    no rate, or a rate more than N days older than the transaction → the amount can't be checked
Date and name rules are the same. A foreign bill with no candidate but a transaction that fits the
date and name goes to a person (amount too far from the converted bill, or no usable rate): the
deterministic rules never accept it on their own.

A pair is accepted only when it is unique on BOTH sides: the bill has exactly one candidate
transaction and that transaction has exactly one candidate bill. Anything else is ambiguous and
left for a person. Nothing is guessed. Callers pass only outgoing (amount > 0), posted transactions
of the bank account; the checks below repeat that, so a wrong caller can't produce a match.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from difflib import SequenceMatcher
from typing import Literal

from . import normalize as n
from .reference import minor_units

RULE_VERSION = "m2"  # m2: bills in another currency than the bank account

# Words banks add to descriptions; they say nothing about the payee
BANK_NOISE = frozenset(
    """
ach ppd ccd ctx web tel pos debit credit payment pmt pymt paymt bill billpay online transfer xfer
wire check chk cheque purchase card visa mastercard des descr id indn entry co to from the for ref and
""".split()
)


@dataclass(frozen=True)
class Rules:
    abs_tolerance: Decimal = Decimal("1.00")
    pct_tolerance: Decimal = Decimal("0.005")
    days_before_invoice: int = 3
    days_after_due: int = 30
    default_terms_days: int = 30
    min_name_similarity: float = 0.6
    fx_pct_tolerance: Decimal = Decimal("0.03")
    fx_max_rate_age_days: int = 4


@dataclass(frozen=True)
class OpenBill:
    invoice_id: str
    currency: str
    amount: Decimal  # positive, document currency
    invoice_date: date
    due_date: date | None
    supplier_name_norm: str | None
    invoice_number_norm: str | None


@dataclass(frozen=True)
class BankTx:
    transaction_id: str
    amount: Decimal  # Plaid sign: positive = money out
    currency: str | None
    date: date
    name: str | None
    merchant_name: str | None = None
    pending: bool = False


@dataclass(frozen=True)
class FxRate:
    rate: Decimal  # 1 unit of the bill currency in the bank currency
    rate_date: date  # the publication date (business days only)


RateLookup = Callable[[str, str, date], FxRate | None]  # (bill currency, bank currency, day) → latest on/before
FxStatus = Literal["same_currency", "priced", "no_rate", "stale_rate"]
FX_REVIEW_CODES = ("fx_amount_difference", "fx_rate_unavailable")
# Review codes where a person picks one of the stored candidate transactions (or none)
CANDIDATE_REVIEW_CODES = ("ambiguous_match", *FX_REVIEW_CODES)


@dataclass(frozen=True)
class Amounts:
    bank_amount: Decimal  # rounded to the bank currency's minor unit
    bank_currency: str
    expected: Decimal | None  # FX: the bill converted at the rate (None when no usable rate)
    difference: Decimal | None  # bank − bill (same currency) or bank − expected (FX)
    ok: bool
    fx_status: FxStatus
    rate: FxRate | None = None  # only when priced


@dataclass(frozen=True)
class Evaluation:
    bank_amount: Decimal
    difference: Decimal | None
    days_from_invoice: int
    name_similarity: float
    invoice_number_found: bool
    amount_ok: bool
    date_ok: bool
    name_ok: bool
    bank_currency: str = ""
    expected_bank_amount: Decimal | None = None
    fx_status: FxStatus = "same_currency"
    fx_rate: FxRate | None = None

    @property
    def candidate(self) -> bool:
        return self.amount_ok and self.date_ok and self.name_ok

    @property
    def needs_person(self) -> bool:
        """Foreign bill: date and name fit, but the amount can't be accepted by rule (too far, or no rate)."""
        return self.fx_status != "same_currency" and self.date_ok and self.name_ok and not self.amount_ok


@dataclass
class Decision:
    accepted: list[tuple[OpenBill, BankTx, Evaluation]] = field(default_factory=list)
    ambiguous: dict[str, list[tuple[BankTx, Evaluation]]] = field(default_factory=dict)  # invoice_id → candidates
    unmatched: list[str] = field(default_factory=list)  # invoice_ids
    # Foreign bills a person must check: invoice_id → (review code, transactions that fit date and name)
    fx_review: dict[str, tuple[str, list[tuple[BankTx, Evaluation]]]] = field(default_factory=dict)


def round_to_currency(amount: Decimal, currency: str) -> Decimal:
    return amount.quantize(Decimal(1).scaleb(-minor_units(currency)))


def _tokens(text: str | None) -> list[str]:
    norm = n.normalize_supplier_name(text) or ""
    return [t for t in norm.split() if t not in BANK_NOISE and not t.isdigit()]


def _token_match(a: str, b: str) -> bool:
    if a == b:
        return True
    if min(len(a), len(b)) >= 3 and (a.startswith(b) or b.startswith(a)):  # bank truncation: SUP → supply
        return True
    return SequenceMatcher(None, a, b).ratio() >= 0.85


def name_similarity(supplier_name_norm: str | None, *bank_texts: str | None) -> float:
    """Share of the supplier's name tokens found in the bank text (0..1)."""
    supplier = [t for t in (supplier_name_norm or "").split() if t not in BANK_NOISE]
    bank = [t for text in bank_texts for t in _tokens(text)]
    if not supplier or not bank:
        return 0.0
    found = sum(1 for s in supplier if any(_token_match(s, b) for b in bank))
    return round(found / len(supplier), 4)


def invoice_number_in(number_norm: str | None, *bank_texts: str | None) -> bool:
    if not number_norm or len(number_norm) < 4:  # too short to be evidence
        return False
    haystack = "".join(re.sub(r"[^0-9A-Z]", "", (t or "").upper()) for t in bank_texts)
    return number_norm in haystack


def is_outflow(tx: BankTx) -> bool:
    return tx.amount > 0 and not tx.pending


def is_foreign(bill: OpenBill, tx: BankTx) -> bool:
    return tx.currency is not None and tx.currency != bill.currency


def _same_currency_amounts(bill: OpenBill, tx: BankTx, rules: Rules) -> Amounts:
    bank_amount = round_to_currency(tx.amount, bill.currency)
    difference = bank_amount - bill.amount
    tolerance = min(rules.abs_tolerance, round_to_currency(bill.amount * rules.pct_tolerance, bill.currency))
    ok = tx.currency == bill.currency and is_outflow(tx) and abs(difference) <= tolerance
    return Amounts(bank_amount, bill.currency, None, difference, ok, "same_currency")


def _fx_amounts(bill: OpenBill, tx: BankTx, rules: Rules, rate: FxRate | None) -> Amounts:
    bank_ccy = tx.currency or ""
    bank_amount = round_to_currency(tx.amount, bank_ccy)
    if rate is None:
        return Amounts(bank_amount, bank_ccy, None, None, False, "no_rate")
    if (tx.date - rate.rate_date).days > rules.fx_max_rate_age_days or rate.rate_date > tx.date:
        return Amounts(bank_amount, bank_ccy, None, None, False, "stale_rate")
    expected = round_to_currency(bill.amount * rate.rate, bank_ccy)
    difference = bank_amount - expected
    tolerance = round_to_currency(expected * rules.fx_pct_tolerance, bank_ccy)
    ok = is_outflow(tx) and expected > 0 and abs(difference) <= tolerance
    return Amounts(bank_amount, bank_ccy, expected, difference, ok, "priced", rate)


def evaluate(bill: OpenBill, tx: BankTx, rules: Rules, rate: FxRate | None = None) -> Evaluation:
    """rate: only for a transaction in another currency than the bill (see RateLookup)."""
    amounts = _fx_amounts(bill, tx, rules, rate) if is_foreign(bill, tx) else _same_currency_amounts(bill, tx, rules)
    days = (tx.date - bill.invoice_date).days
    latest = (bill.due_date or bill.invoice_date + timedelta(days=rules.default_terms_days)) + timedelta(
        days=rules.days_after_due
    )
    similarity = name_similarity(bill.supplier_name_norm, tx.name, tx.merchant_name)
    number_found = invoice_number_in(bill.invoice_number_norm, tx.name, tx.merchant_name)
    return Evaluation(
        bank_amount=amounts.bank_amount,
        difference=amounts.difference,
        days_from_invoice=days,
        name_similarity=similarity,
        invoice_number_found=number_found,
        amount_ok=amounts.ok,
        date_ok=-rules.days_before_invoice <= days and tx.date <= latest,
        name_ok=similarity >= rules.min_name_similarity or number_found,
        bank_currency=amounts.bank_currency,
        expected_bank_amount=amounts.expected,
        fx_status=amounts.fx_status,
        fx_rate=amounts.rate,
    )


Pairs = dict[str, list[tuple[BankTx, Evaluation]]]  # invoice_id → (transaction, evaluation)


def _evaluate_all(
    bills: list[OpenBill],
    txs: list[BankTx],
    rules: Rules,
    excluded: frozenset[tuple[str, str]],
    rates: RateLookup | None,
) -> tuple[Pairs, Pairs]:
    """(candidates, foreign pairs a person must check), per bill."""
    candidates: Pairs = {b.invoice_id: [] for b in bills}
    for_person: Pairs = {b.invoice_id: [] for b in bills}
    for bill in bills:
        for tx in txs:
            if (bill.invoice_id, tx.transaction_id) in excluded:
                continue
            rate = (
                rates(bill.currency, tx.currency, tx.date) if rates and tx.currency and is_foreign(bill, tx) else None
            )
            ev = evaluate(bill, tx, rules, rate)
            if ev.candidate:
                candidates[bill.invoice_id].append((tx, ev))
            elif ev.needs_person and is_outflow(tx):
                for_person[bill.invoice_id].append((tx, ev))
    return candidates, for_person


def _fx_review(found: list[tuple[BankTx, Evaluation]], taken: set[str]) -> tuple[str, list[tuple[BankTx, Evaluation]]]:
    """A transaction that is some bill's candidate is never proposed for a foreign bill's review."""
    free = [(tx, ev) for tx, ev in found if tx.transaction_id not in taken]
    no_rate = any(ev.fx_status in ("no_rate", "stale_rate") for _, ev in free)
    return ("fx_rate_unavailable" if no_rate else "fx_amount_difference"), free


def decide(
    bills: list[OpenBill],
    txs: list[BankTx],
    rules: Rules,
    excluded: frozenset[tuple[str, str]] = frozenset(),
    rates: RateLookup | None = None,
) -> Decision:
    """excluded: (invoice ID, transaction ID) pairs a person dismissed: never proposed again.
    rates: reference rates for bills in another currency than the bank account (None: none known)."""
    candidates, for_person = _evaluate_all(bills, txs, rules, excluded, rates)
    per_tx: dict[str, set[str]] = {}
    for invoice_id, found in candidates.items():
        for tx, _ in found:
            per_tx.setdefault(tx.transaction_id, set()).add(invoice_id)

    decision = Decision()
    for bill in bills:
        found = candidates[bill.invoice_id]
        if len(found) == 1 and len(per_tx[found[0][0].transaction_id]) == 1:
            tx, ev = found[0]
            decision.accepted.append((bill, tx, ev))
        elif found:
            decision.ambiguous[bill.invoice_id] = found
        else:
            code, free = _fx_review(for_person[bill.invoice_id], set(per_tx))
            if free:
                decision.fx_review[bill.invoice_id] = (code, free)
            else:
                decision.unmatched.append(bill.invoice_id)
    return decision
