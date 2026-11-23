"""Deterministic checks and routing. No LLM involved: the provider only extracted data.

Routing principle:
  rejected      only when a human could do nothing with it: not an invoice, unreadable,
                exact duplicate file
  needs_review  anything a human can check or fix (missing field, ambiguous date, totals
                that don't add up, wrong sign, possible duplicate, low confidence...)
  ready         every check passed
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from .models import CommonInvoice, Issue
from .reference import minor_units

REQUIRED_FIELDS = ("supplier_name", "invoice_number", "invoice_date", "currency", "total")
MIN_READABLE_CHARS = 20

# Normalization issues that are information only (don't block auto-posting)
INFO_ISSUES = {"currency_from_country", "adjustment_repeats_tax"}


@dataclass
class Duplicate:
    invoice_id: str
    status: str


@dataclass
class Validation:
    status: str  # ready | needs_review | rejected
    reasons: list[Issue]
    checks: dict[str, Any]
    confidence: float | None
    confidence_ocr: float | None
    confidence_completeness: float
    tax_mode: str | None = None  # exclusive | inclusive (which totals formula matched)
    info: list[Issue] = field(default_factory=list)


def _issue(code: str, field_: str | None, message: str) -> Issue:
    return Issue(code=code, field=field_, message=message)


def _present(inv: CommonInvoice, name: str) -> bool:
    if name == "supplier_name":
        return bool(inv.supplier.name)
    if name == "invoice_date":
        # a printed but ambiguous date counts as extracted (it's reported as ambiguous_date)
        return inv.invoice_date is not None or bool(inv.dates.invoice_date_raw)
    return getattr(inv, name) is not None


def _close(a: Decimal, b: Decimal, tol: Decimal) -> bool:
    return abs(a - b) <= tol


def _sum(values: Iterable[Decimal | None]) -> Decimal:
    return sum((v for v in values if v is not None), Decimal(0))


@dataclass
class _Findings:
    """What the checks found: blocking reasons, information-only issues, and the check details."""

    reasons: list[Issue] = field(default_factory=list)
    info: list[Issue] = field(default_factory=list)
    checks: dict[str, Any] = field(default_factory=dict)

    def fail(self, code: str, field_: str | None, message: str) -> None:
        self.reasons.append(_issue(code, field_, message))


def totals_check(inv: CommonInvoice) -> tuple[list[Issue], dict[str, Any], str | None]:
    """Line items + adjustments (discount, shipping, fee, rounding) + tax = total.

    Formulas (each within a rounding tolerance of one minor unit per summed line):
      lines:    subtotal = item lines, or = all before-tax lines (discount/shipping included)
      tax:      tax_total = sum of tax lines (with or without withholding)
      total:    total = before-tax lines + tax + after-tax lines          (tax exclusive)
           or   total = before-tax lines + after-tax lines, tax included  (tax inclusive,
                common in Japan and on consumer receipts)
    """
    f = _Findings()
    if not inv.lines:
        f.fail("no_line_items", "lines", "no line items were extracted")
        return f.reasons, {"lines": "skipped: no line items"}, None
    if any(li.amount is None for li in inv.lines):
        return f.reasons, {"lines": "skipped: a line amount is unreadable"}, None

    unit = Decimal(1).scaleb(-minor_units(inv.currency))
    _subtotal_check(inv, unit, f)
    tax_sum = _tax_lines_check(inv, unit, f)
    tax_mode = _grand_total_check(inv, unit, tax_sum, f)
    return f.reasons, f.checks, tax_mode


def _before_tax(inv: CommonInvoice) -> Decimal:
    return _sum(li.amount for li in inv.lines if li.tax_stage == "before_tax")


def _after_tax(inv: CommonInvoice) -> Decimal:
    return _sum(li.amount for li in inv.lines if li.tax_stage == "after_tax")


def _subtotal_check(inv: CommonInvoice, unit: Decimal, f: _Findings) -> None:
    """Subtotal = all before-tax lines, or = item lines only."""
    if inv.subtotal is None:
        return
    before_sum = _before_tax(inv)
    items_sum = _sum(li.amount for li in inv.lines if li.tax_stage == "before_tax" and li.kind == "item")
    tol = unit * max(1, sum(1 for li in inv.lines if li.tax_stage == "before_tax"))
    ok = _close(inv.subtotal, before_sum, tol) or _close(inv.subtotal, items_sum, tol)
    f.checks["subtotal_vs_lines"] = {
        "passed": ok,
        "subtotal": str(inv.subtotal),
        "before_tax_lines": str(before_sum),
        "item_lines": str(items_sum),
    }
    if not ok:
        f.fail("lines_mismatch", "subtotal", f"subtotal {inv.subtotal} ≠ sum of lines {before_sum}")


def _tax_lines_check(inv: CommonInvoice, unit: Decimal, f: _Findings) -> Decimal | None:
    """Tax total = tax lines (with or without withholding). Returns the tax lines' sum when all are readable."""
    if not inv.taxes or any(t.amount is None for t in inv.taxes):
        return None
    tax_sum = _sum(t.amount for t in inv.taxes)
    if inv.tax_total is not None:
        tol = unit * max(1, len(inv.taxes))
        non_withholding = _sum(t.amount for t in inv.taxes if t.tax_type != "withholding")
        ok = _close(inv.tax_total, tax_sum, tol) or _close(inv.tax_total, non_withholding, tol)
        f.checks["tax_total_vs_tax_lines"] = {"passed": ok, "tax_total": str(inv.tax_total), "tax_lines": str(tax_sum)}
        if not ok:
            f.fail("tax_mismatch", "tax_total", f"tax total {inv.tax_total} ≠ sum of tax lines {tax_sum}")
    return tax_sum


def _grand_total_check(inv: CommonInvoice, unit: Decimal, tax_sum: Decimal | None, f: _Findings) -> str | None:
    """Total = lines + tax (exclusive) or = lines with tax included (inclusive). Returns the matching mode."""
    if inv.total is None:
        return None
    tax = tax_sum if tax_sum is not None else (inv.tax_total or Decimal(0))
    tol = unit * max(1, len(inv.lines) + len(inv.taxes))
    exclusive = _before_tax(inv) + tax + _after_tax(inv)
    inclusive = _before_tax(inv) + _after_tax(inv)
    tax_mode = None
    if _close(inv.total, exclusive, tol):
        tax_mode = "exclusive"
    elif tax != 0 and _close(inv.total, inclusive, tol):
        tax_mode = "inclusive"
    f.checks["total"] = {
        "passed": tax_mode is not None,
        "total": str(inv.total),
        "computed_tax_exclusive": str(exclusive),
        "computed_tax_inclusive": str(inclusive),
        "tax_mode": tax_mode,
    }
    if tax_mode is None:
        f.fail("total_mismatch", "total", f"total {inv.total} ≠ lines + adjustments + tax {exclusive}")
    return tax_mode


def validate(
    inv: CommonInvoice, *, confidence_threshold: float, duplicates: list[Duplicate] | None = None
) -> Validation:
    f = _Findings()
    # Confidence = OCR page-average confidence × required-field completeness (both stored)
    completeness = round(sum(_present(inv, name) for name in REQUIRED_FIELDS) / len(REQUIRED_FIELDS), 4)
    ocr = inv.extraction.ocr_confidence_avg
    confidence = round(ocr * completeness, 4) if ocr is not None else None

    def result(status: str, tax_mode: str | None = None) -> Validation:
        return Validation(status, f.reasons, f.checks, confidence, ocr, completeness, tax_mode, f.info)

    # Rejections (nothing a human could do with it) and early stops
    if stop := _readability_and_kind(inv, f):
        return result(stop)
    for i in inv.issues:  # normalization issues (ambiguous date, unreadable amount, currency...)
        (f.info if i.code in INFO_ISSUES else f.reasons).append(i)
    if any(i.code == "extraction_invalid" for i in inv.issues):
        return result("needs_review")  # readable text: a human can key it in

    _required_fields_check(inv, f)
    _sign_check(inv, f)
    _dates_check(inv, f)
    total_reasons, f.checks["totals"], tax_mode = totals_check(inv)
    f.reasons += total_reasons
    _duplicates_check(duplicates or [], f)
    _confidence_check(confidence, ocr, completeness, confidence_threshold, f)
    return result("needs_review" if f.reasons else "ready", tax_mode)


def _readability_and_kind(inv: CommonInvoice, f: _Findings) -> str | None:
    """'rejected' / 'needs_review' when the document can't go through the other checks."""
    readable = len(re.sub(r"\W", "", inv.ocr_text or ""))
    f.checks["readable"] = {"passed": readable >= MIN_READABLE_CHARS, "characters": readable}
    if readable < MIN_READABLE_CHARS:
        f.fail("unreadable", None, "no readable text in the document")
        return "rejected"
    not_invoice = next((i for i in inv.issues if i.code == "not_invoice"), None)
    if not_invoice is None:
        return None
    # Guard against a document wrongly classified (or a prompt injection saying 'not an invoice'):
    # if it carries an invoice number and a total, a human should look at it.
    if inv.invoice_number and inv.total is not None:
        f.fail(
            "classification_conflict",
            "document_kind",
            "classified as not an invoice, but has an invoice number and a total",
        )
        return "needs_review"
    f.reasons.append(not_invoice)
    return "rejected"


def _required_fields_check(inv: CommonInvoice, f: _Findings) -> None:
    missing = [name for name in REQUIRED_FIELDS if not _present(inv, name)]
    f.checks["required_fields"] = {"passed": not missing, "missing": missing}
    for name in missing:
        if not any(r.field == name for r in f.reasons):  # avoid double-reporting
            f.fail("missing_field", name, f"{name} not found")


def _sign_check(inv: CommonInvoice, f: _Findings) -> None:
    if inv.total is None:
        return
    wrong = (inv.document_type == "invoice" and inv.total < 0) or (inv.document_type == "credit_note" and inv.total > 0)
    f.checks["sign"] = {"passed": not wrong, "document_type": inv.document_type, "total": str(inv.total)}
    if wrong:
        f.fail("wrong_sign", "total", f"{inv.document_type} with total {inv.total}: check the document type")


def _dates_check(inv: CommonInvoice, f: _Findings) -> None:
    if not (inv.invoice_date and inv.due_date):
        return
    ok = inv.due_date >= inv.invoice_date
    f.checks["due_after_invoice_date"] = {"passed": ok}
    if not ok:
        f.fail(
            "due_before_invoice_date",
            "due_date",
            f"due date {inv.due_date} is before invoice date {inv.invoice_date}",
        )


def _duplicates_check(duplicates: list[Duplicate], f: _Findings) -> None:
    """Possible duplicate: same supplier + document type + number, different file."""
    f.checks["duplicate"] = {"passed": not duplicates, "matches": [d.invoice_id for d in duplicates]}
    for d in duplicates:
        f.fail(
            "possible_duplicate", "invoice_number", f"same supplier and number as invoice {d.invoice_id} ({d.status})"
        )


def _confidence_check(
    confidence: float | None, ocr: float | None, completeness: float, threshold: float, f: _Findings
) -> None:
    """The threshold is provisional, calibrated on the test set on Day 3."""
    f.checks["confidence"] = {
        "passed": confidence is not None and confidence >= threshold,
        "score": confidence,
        "ocr": ocr,
        "completeness": completeness,
        "threshold": threshold,
    }
    if confidence is None:
        f.fail("no_ocr_confidence", None, "the provider returned no OCR confidence")
    elif confidence < threshold:
        f.fail("low_confidence", None, f"confidence {confidence} below threshold {threshold}")
