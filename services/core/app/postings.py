"""Builds what the n8n workflow sends to the accounting system (pure functions, no I/O).

The workflow does the API calls; this module decides the content, so the rules are tested:
  * bill / vendor credit lines whose sum is exactly the invoice total
  * document number within QuickBooks' 21-character limit, deterministic when shortened
  * vendor display names: one vendor per (supplier, currency), suffixed when not in the home currency
  * query strings with QuickBooks escaping
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from decimal import Decimal

from .matching import round_to_currency
from .reference import minor_units

DOC_NUMBER_MAX = 21
DISPLAY_NAME_MAX = 100
LINE_DESCRIPTION_MAX = 4000
KIND_LABELS = {
    "item": "Item",
    "discount": "Discount",
    "shipping": "Shipping",
    "fee": "Fee",
    "rounding": "Rounding",
    "other": "Adjustment",
}
TAX_LABELS = {
    "vat": "VAT",
    "gst": "GST",
    "sales_tax": "Sales tax",
    "withholding": "Withholding tax",
    "other": "Tax",
    "none": "Tax",
}


class PostingError(Exception):
    """The invoice can't be turned into a correct accounting document; a person must look at it."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Line:
    description: str
    amount: Decimal
    account: str  # 'expense' | 'tax'


def doc_number(invoice_number: str) -> str:
    """The invoice number if it fits; otherwise a deterministic short form (prefix + hash)."""
    number = re.sub(r"\s+", " ", invoice_number).strip()
    if len(number) <= DOC_NUMBER_MAX:
        return number
    digest = hashlib.sha256(number.encode()).hexdigest()[:4].upper()
    return f"{number[: DOC_NUMBER_MAX - 5]}~{digest}"


def vendor_display_name(supplier_name: str, currency: str, home_currency: str) -> str:
    """QuickBooks: unique across all vendors whatever the currency; no ':' tab or newline."""
    base = re.sub(r"[:\t\r\n]+", " ", supplier_name)
    base = re.sub(r"\s+", " ", base).strip()
    suffix = "" if currency == home_currency else f" ({currency})"
    return base[: DISPLAY_NAME_MAX - len(suffix)].rstrip() + suffix


def qbo_quote(value: str) -> str:
    """Value for a QuickBooks query string literal: 'O\\'Brien'."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


def document_lines(
    *, lines: list[dict], taxes: list[dict], tax_mode: str | None, total: Decimal, currency: str, is_credit_note: bool
) -> list[Line]:
    """Lines whose sum equals the absolute invoice total, in the document currency.

    Tax-exclusive invoices: every line + every tax line (withholding is negative).
    Tax-inclusive invoices: line amounts already contain the tax, so tax lines are not added.
    Credit notes are stored negative; the vendor credit is built from the positive amounts.
    A residual of at most one minor unit per line (the validation tolerance) becomes a rounding line.
    """
    sign = Decimal(-1) if is_credit_note else Decimal(1)
    out: list[Line] = []
    for li in lines:
        if li["amount"] is None:
            raise PostingError("line_amount_missing", f"line {li['line_no']} has no amount")
        amount = round_to_currency(Decimal(li["amount"]) * sign, currency)
        if amount:
            label = (li.get("description") or KIND_LABELS.get(li["kind"], "Item"))[:LINE_DESCRIPTION_MAX]
            out.append(Line(label, amount, "expense"))
    if tax_mode != "inclusive":
        for t in taxes:
            if t["amount"] is None:
                raise PostingError("tax_amount_missing", "a tax line has no amount")
            amount = round_to_currency(Decimal(t["amount"]) * sign, currency)
            if amount:
                out.append(Line((t.get("label") or TAX_LABELS[t["tax_type"]])[:LINE_DESCRIPTION_MAX], amount, "tax"))

    target = round_to_currency(abs(total), currency)
    residual = target - sum((l.amount for l in out), Decimal(0))
    unit = Decimal(1).scaleb(-minor_units(currency))
    if residual:
        if abs(residual) > unit * max(len(out), 1):
            raise PostingError(
                "lines_do_not_sum_to_total", f"lines sum to {target - residual}, total is {target} ({currency})"
            )
        out.append(Line("Rounding difference", residual, "expense"))
    if not out or sum((l.amount for l in out), Decimal(0)) <= 0:
        raise PostingError("non_positive_total", "the document total must be positive once signed")
    return out
