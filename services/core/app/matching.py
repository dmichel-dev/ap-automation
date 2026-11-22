"""Deterministic bank matching: which outgoing bank transaction pays which open bill.

Pure functions (no I/O) so every rule is unit-tested; the database side is in matching_db.py.

A pair (bill, transaction) is a candidate when all three rules pass:
  amount  |bank − bill| ≤ min(absolute tolerance, percentage × bill)    (bank rounded to minor units)
  date    invoice date − N days ≤ transaction date ≤ due date (or invoice date + terms) + M days
  name    supplier-name similarity ≥ threshold, OR the invoice number appears in the bank text

A pair is accepted only when it is unique on BOTH sides: the bill has exactly one candidate
transaction and that transaction has exactly one candidate bill. Anything else is ambiguous and
left for a person. Nothing is guessed. Callers pass only same-currency, outgoing (amount > 0),
posted transactions; the checks below repeat that, so a wrong caller can't produce a match.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from difflib import SequenceMatcher

from . import normalize as n
from .reference import minor_units

RULE_VERSION = "m1"

# Words banks add to descriptions; they say nothing about the payee
BANK_NOISE = frozenset("""
ach ppd ccd ctx web tel pos debit credit payment pmt pymt paymt bill billpay online transfer xfer
wire check chk cheque purchase card visa mastercard des descr id indn entry co to from the for ref and
""".split())


@dataclass(frozen=True)
class Rules:
    abs_tolerance: Decimal = Decimal("1.00")
    pct_tolerance: Decimal = Decimal("0.005")
    days_before_invoice: int = 3
    days_after_due: int = 30
    default_terms_days: int = 30
    min_name_similarity: float = 0.6


@dataclass(frozen=True)
class OpenBill:
    invoice_id: str
    currency: str
    amount: Decimal                  # positive, document currency
    invoice_date: date
    due_date: date | None
    supplier_name_norm: str | None
    invoice_number_norm: str | None


@dataclass(frozen=True)
class BankTx:
    transaction_id: str
    amount: Decimal                  # Plaid sign: positive = money out
    currency: str | None
    date: date
    name: str | None
    merchant_name: str | None = None
    pending: bool = False


@dataclass(frozen=True)
class Evaluation:
    bank_amount: Decimal
    difference: Decimal
    days_from_invoice: int
    name_similarity: float
    invoice_number_found: bool
    amount_ok: bool
    date_ok: bool
    name_ok: bool

    @property
    def candidate(self) -> bool:
        return self.amount_ok and self.date_ok and self.name_ok


@dataclass
class Decision:
    accepted: list[tuple[OpenBill, BankTx, Evaluation]] = field(default_factory=list)
    ambiguous: dict[str, list[tuple[BankTx, Evaluation]]] = field(default_factory=dict)   # invoice_id → candidates
    unmatched: list[str] = field(default_factory=list)                                     # invoice_ids


def round_to_currency(amount: Decimal, currency: str) -> Decimal:
    return amount.quantize(Decimal(1).scaleb(-minor_units(currency)))


def _tokens(text: str | None) -> list[str]:
    norm = n.normalize_supplier_name(text) or ""
    return [t for t in norm.split() if t not in BANK_NOISE and not t.isdigit()]


def _token_match(a: str, b: str) -> bool:
    if a == b:
        return True
    if min(len(a), len(b)) >= 3 and (a.startswith(b) or b.startswith(a)):   # bank truncation: SUP → supply
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
    if not number_norm or len(number_norm) < 4:          # too short to be evidence
        return False
    haystack = "".join(re.sub(r"[^0-9A-Z]", "", (t or "").upper()) for t in bank_texts)
    return number_norm in haystack


def evaluate(bill: OpenBill, tx: BankTx, rules: Rules) -> Evaluation:
    bank_amount = round_to_currency(tx.amount, bill.currency)
    difference = bank_amount - bill.amount
    tolerance = min(rules.abs_tolerance, round_to_currency(bill.amount * rules.pct_tolerance, bill.currency))
    days = (tx.date - bill.invoice_date).days
    latest = (bill.due_date or bill.invoice_date + timedelta(days=rules.default_terms_days)) \
        + timedelta(days=rules.days_after_due)
    similarity = name_similarity(bill.supplier_name_norm, tx.name, tx.merchant_name)
    number_found = invoice_number_in(bill.invoice_number_norm, tx.name, tx.merchant_name)
    same_currency_outflow = tx.currency == bill.currency and tx.amount > 0 and not tx.pending
    return Evaluation(
        bank_amount=bank_amount, difference=difference, days_from_invoice=days,
        name_similarity=similarity, invoice_number_found=number_found,
        amount_ok=same_currency_outflow and abs(difference) <= tolerance,
        date_ok=-rules.days_before_invoice <= days and tx.date <= latest,
        name_ok=similarity >= rules.min_name_similarity or number_found,
    )


def decide(bills: list[OpenBill], txs: list[BankTx], rules: Rules) -> Decision:
    candidates: dict[str, list[tuple[BankTx, Evaluation]]] = {b.invoice_id: [] for b in bills}
    per_tx: dict[str, set[str]] = {}
    for bill in bills:
        for tx in txs:
            ev = evaluate(bill, tx, rules)
            if ev.candidate:
                candidates[bill.invoice_id].append((tx, ev))
                per_tx.setdefault(tx.transaction_id, set()).add(bill.invoice_id)

    decision = Decision()
    for bill in bills:
        found = candidates[bill.invoice_id]
        if not found:
            decision.unmatched.append(bill.invoice_id)
        elif len(found) == 1 and len(per_tx[found[0][0].transaction_id]) == 1:
            tx, ev = found[0]
            decision.accepted.append((bill, tx, ev))
        else:
            decision.ambiguous[bill.invoice_id] = found
    return decision
