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
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from .matching import round_to_currency
from .reference import minor_units
from .tax_treatment import Treatment

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
    account: str  # 'expense' | 'tax_recoverable' | 'withholding'


@dataclass
class _Cost:
    """An expense line being built: its tax-allocation keys and the tax added to it."""

    description: str
    amount: Decimal
    tax_rate: Decimal | None
    base: bool  # takes part in tax allocation (before-tax line, not a rounding row)
    tax: Decimal = Decimal(0)


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
    treatment: Callable[[str], Treatment],
) -> list[Line]:
    """Lines whose sum equals the absolute invoice total, in the document currency.

    Each tax follows the company's tax treatment (tax_treatment.py):
      non-recoverable  part of the cost: spread over the expense lines it belongs to (by tax
                       rate when the lines carry one, otherwise in proportion to the amounts)
      recoverable      a separate line to the receivable account
      withholding      a separate (negative) line to the liability account
    Tax-inclusive invoices: line amounts already contain the tax, so only a recoverable tax is
    taken out of them; withholding is not added (it is outside the inclusive total).
    Credit notes are stored negative; the vendor credit is built from the positive amounts.
    A residual of at most one minor unit per line (the validation tolerance) becomes a rounding line.
    """
    sign = Decimal(-1) if is_credit_note else Decimal(1)
    costs = _costs(lines, sign, currency)
    separate: list[Line] = []
    for t in taxes:
        how = treatment(t["tax_type"])
        if tax_mode == "inclusive" and how != "recoverable":
            continue
        amount = _tax_amount(t, sign, currency)
        if how == "non_recoverable":
            _allocate(costs, amount, t.get("rate"), currency)
            continue
        if tax_mode == "inclusive":
            _allocate(costs, -amount, t.get("rate"), currency)  # take it out of the cost
        label = t.get("label") or TAX_LABELS[t["tax_type"]]
        separate.append(Line(label[:LINE_DESCRIPTION_MAX], amount, "tax_recoverable" if how == "recoverable" else how))
    out = [_cost_line(c) for c in costs if c.amount + c.tax] + [ln for ln in separate if ln.amount]
    out += _rounding_line(out, total, currency)
    if not out or _total(out) <= 0:
        raise PostingError("non_positive_total", "the document total must be positive once signed")
    return out


def _total(lines: list[Line]) -> Decimal:
    return sum((ln.amount for ln in lines), Decimal(0))


def _costs(lines: list[dict[str, Any]], sign: Decimal, currency: str) -> list[_Cost]:
    out = []
    for li in lines:
        if li["amount"] is None:
            raise PostingError("line_amount_missing", f"line {li['line_no']} has no amount")
        amount = round_to_currency(Decimal(li["amount"]) * sign, currency)
        label = li.get("description") or KIND_LABELS.get(li["kind"], "Item")
        base = li.get("tax_stage", "before_tax") == "before_tax" and li["kind"] != "rounding"
        rate = Decimal(li["tax_rate"]) if li.get("tax_rate") is not None else None
        out.append(_Cost(label, amount, rate, base))
    return out


def _tax_amount(t: dict[str, Any], sign: Decimal, currency: str) -> Decimal:
    if t["amount"] is None:
        raise PostingError("tax_amount_missing", "a tax line has no amount")
    return round_to_currency(Decimal(t["amount"]) * sign, currency)


def _allocate(costs: list[_Cost], amount: Decimal, rate: Any, currency: str) -> None:
    """Spreads a tax amount over the lines it applies to: the lines with the same rate when the
    rate is known on both sides, else every before-tax line, in proportion to their amounts.
    The rounding remainder goes to the largest line, so the total is exact."""
    if not amount:
        return
    base = [c for c in costs if c.base and c.amount > 0]
    same_rate = [c for c in base if rate is not None and c.tax_rate == Decimal(rate)]
    targets = same_rate or base
    weight = sum((c.amount for c in targets), Decimal(0))
    if weight <= 0:
        costs.append(_Cost("Tax (non-recoverable)", Decimal(0), None, False, amount))
        return
    shares = [round_to_currency(amount * c.amount / weight, currency) for c in targets]
    shares[max(range(len(targets)), key=lambda i: targets[i].amount)] += amount - sum(shares, Decimal(0))
    for c, share in zip(targets, shares, strict=True):
        c.tax += share


def _cost_line(c: _Cost) -> Line:
    description = f"{c.description} (incl. tax {c.tax})" if c.tax else c.description
    return Line(description[:LINE_DESCRIPTION_MAX], c.amount + c.tax, "expense")


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


@dataclass(frozen=True)
class BookClose:
    """The accounting system's closing date: nothing is posted on or before it."""

    known: bool  # False: the preferences could not be read, so nothing is posted this run
    closed_through: date | None = None  # None: no closing date set

    def closes(self, *days: date | None) -> bool:
        last = self.closed_through
        return last is not None and any(d is not None and d <= last for d in days)


def book_close(status: int, body: Any) -> BookClose:
    """From a QuickBooks GET /preferences response (AccountingInfoPrefs.BookCloseDate, absent when unset)."""
    prefs = body.get("Preferences") if status == 200 and isinstance(body, dict) else None
    if not isinstance(prefs, dict):
        return BookClose(known=False)
    raw = (prefs.get("AccountingInfoPrefs") or {}).get("BookCloseDate")
    try:
        return BookClose(known=True, closed_through=date.fromisoformat(raw[:10]) if raw else None)
    except (TypeError, ValueError):
        return BookClose(known=False)  # unreadable: treated like an unknown date


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
