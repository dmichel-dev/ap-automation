"""Builds what the n8n workflow sends to the accounting system (pure functions, no I/O).

The workflow does the API calls; this module decides the content, so the rules are tested:
  * bill / vendor credit lines whose sum is exactly the invoice total
  * document number within QuickBooks' 21-character limit, deterministic when shortened
  * vendor display names: one vendor per (supplier, currency), suffixed when not in the home currency
  * query strings with QuickBooks escaping
  * what to do with QuickBooks' answers: vendor found / create / review, existing document,
    and which failures are retried
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

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
    *,
    lines: list[dict[str, Any]],
    taxes: list[dict[str, Any]],
    tax_mode: str | None,
    total: Decimal,
    currency: str,
    is_credit_note: bool,
) -> list[Line]:
    """Lines whose sum equals the absolute invoice total, in the document currency.

    Tax-exclusive invoices: every line + every tax line (withholding is negative).
    Tax-inclusive invoices: line amounts already contain the tax, so tax lines are not added.
    Credit notes are stored negative; the vendor credit is built from the positive amounts.
    A residual of at most one minor unit per line (the validation tolerance) becomes a rounding line.
    """
    sign = Decimal(-1) if is_credit_note else Decimal(1)
    out = _expense_lines(lines, sign, currency)
    if tax_mode != "inclusive":
        out += _tax_lines(taxes, sign, currency)
    out += _rounding_line(out, total, currency)
    if not out or _total(out) <= 0:
        raise PostingError("non_positive_total", "the document total must be positive once signed")
    return out


def _total(lines: list[Line]) -> Decimal:
    return sum((ln.amount for ln in lines), Decimal(0))


def _expense_lines(lines: list[dict[str, Any]], sign: Decimal, currency: str) -> list[Line]:
    out = []
    for li in lines:
        if li["amount"] is None:
            raise PostingError("line_amount_missing", f"line {li['line_no']} has no amount")
        if amount := round_to_currency(Decimal(li["amount"]) * sign, currency):
            label = li.get("description") or KIND_LABELS.get(li["kind"], "Item")
            out.append(Line(label[:LINE_DESCRIPTION_MAX], amount, "expense"))
    return out


def _tax_lines(taxes: list[dict[str, Any]], sign: Decimal, currency: str) -> list[Line]:
    out = []
    for t in taxes:
        if t["amount"] is None:
            raise PostingError("tax_amount_missing", "a tax line has no amount")
        if amount := round_to_currency(Decimal(t["amount"]) * sign, currency):
            label = t.get("label") or TAX_LABELS[t["tax_type"]]
            out.append(Line(label[:LINE_DESCRIPTION_MAX], amount, "tax"))
    return out


def _rounding_line(lines: list[Line], total: Decimal, currency: str) -> list[Line]:
    """A residual within the validation tolerance becomes one rounding line; more is an error."""
    target = round_to_currency(abs(total), currency)
    residual = target - _total(lines)
    if not residual:
        return []
    unit = Decimal(1).scaleb(-minor_units(currency))
    if abs(residual) > unit * max(len(lines), 1):
        raise PostingError(
            "lines_do_not_sum_to_total", f"lines sum to {target - residual}, total is {target} ({currency})"
        )
    return [Line("Rounding difference", residual, "expense")]


# =====================================================================================
# Decisions on QuickBooks answers (the workflow only passes the responses through)
# =====================================================================================
@dataclass(frozen=True)
class Decision:
    """What the workflow does next: 'use' / 'create' / 'exists' go on; 'review' and 'error' are reported as failures."""

    decision: str
    external_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    http_status: int | None = None
    fault_code: str | None = None


def _query_error(status: int, body: Any) -> Decision | None:
    """A QuickBooks query that didn't answer normally."""
    if status == 200 and isinstance(body, dict) and "QueryResponse" in body:
        return None
    fault = qbo_fault(body)
    return Decision(
        "error",
        http_status=status,
        fault_code=fault.get("code"),
        error_message=(fault.get("Message") or "query failed")[:300],
    )


def vendor_decision(status: int, body: Any, currency: str) -> Decision:
    """Vendor search by display name (inactive vendors included). A vendor's currency is fixed in
    QuickBooks, so a same-name vendor in another currency needs a person, as does an inactive one."""
    if error := _query_error(status, body):
        return error
    found = body["QueryResponse"].get("Vendor") or []
    if not found:
        return Decision("create")
    vendor = found[0]
    vendor_currency = (vendor.get("CurrencyRef") or {}).get("value")
    if vendor_currency != currency:
        return Decision(
            "review",
            error_code="vendor_currency_conflict",
            error_message=f"vendor {vendor['Id']} with this name uses {vendor_currency}; the invoice is in {currency}",
        )
    if vendor.get("Active") is False:
        return Decision("review", error_code="vendor_inactive", error_message=f"vendor {vendor['Id']} is inactive")
    return Decision("use", external_id=str(vendor["Id"]))


def existing_document_decision(status: int, body: Any, entity: str, vendor_id: str) -> Decision:
    """Documents with the same number: QuickBooks accepts duplicates, so we look before creating.
    One for this vendor = an earlier run created it (reuse); several = a person must choose."""
    if error := _query_error(status, body):
        return error
    docs = [
        d for d in body["QueryResponse"].get(entity) or [] if str((d.get("VendorRef") or {}).get("value")) == vendor_id
    ]
    if len(docs) > 1:
        return Decision(
            "review",
            error_code="duplicate_in_accounting",
            error_message=f"{len(docs)} {entity} records with this number for vendor {vendor_id}",
        )
    return Decision("exists", external_id=str(docs[0]["Id"])) if docs else Decision("create")


def qbo_fault(body: Any) -> dict[str, Any]:
    """First error of a QuickBooks Fault, or {}."""
    if not isinstance(body, dict):
        return {}
    errors = (body.get("Fault") or {}).get("Error") or [{}]
    first: dict[str, Any] = errors[0]
    return first


# Failures named by the workflow (not HTTP errors) that are worth retrying
RETRYABLE_ERRORS = {"exchange_rate_unavailable", "file_fetch_failed", "attachment_failed"}


def classify_failure(
    error_code: str | None, http_status: int | None, fault_code: str | None, review_errors: Iterable[str]
) -> tuple[str, bool]:
    """(error code, retryable). Retryable: network errors (status 0), 401 (token refresh), 425 (request
    already sent: the next run finds the document), 429, 5xx, and the named transient failures.
    Validation errors (other 4xx) and anything needing a person are not: the same data would fail again."""
    if error_code:
        return error_code, error_code not in review_errors and error_code in RETRYABLE_ERRORS
    status = http_status or 0
    code = f"qbo_{fault_code}" if fault_code else f"http_{status}"
    return code, status in (0, 401, 425, 429) or status >= 500
