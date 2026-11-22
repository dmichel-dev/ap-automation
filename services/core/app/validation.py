"""Deterministic checks and routing. No LLM involved: the provider only extracted data.

Routing principle:
  rejected      only when a human could do nothing with it: not an invoice, unreadable,
                exact duplicate file
  needs_review  anything a human can check or fix (missing field, ambiguous date, totals
                that don't add up, wrong sign, possible duplicate, low confidence...)
  ready         every check passed
"""

import re
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
    status: str                                   # ready | needs_review | rejected
    reasons: list[Issue]
    checks: dict[str, Any]
    confidence: float | None
    confidence_ocr: float | None
    confidence_completeness: float
    tax_mode: str | None = None                   # exclusive | inclusive (which totals formula matched)
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


def totals_check(inv: CommonInvoice) -> tuple[list[Issue], dict[str, Any], str | None]:
    """Line items + adjustments (discount, shipping, fee, rounding) + tax = total.

    Formulas (each within a rounding tolerance of one minor unit per summed line):
      lines:    subtotal = item lines, or = all before-tax lines (discount/shipping included)
      tax:      tax_total = sum of tax lines (with or without withholding)
      total:    total = before-tax lines + tax + after-tax lines          (tax exclusive)
           or   total = before-tax lines + after-tax lines, tax included  (tax inclusive,
                common in Japan and on consumer receipts)
    """
    reasons: list[Issue] = []
    checks: dict[str, Any] = {}
    unit = Decimal(1).scaleb(-minor_units(inv.currency))

    if not inv.lines:
        reasons.append(_issue("no_line_items", "lines", "no line items were extracted"))
        return reasons, {"lines": "skipped: no line items"}, None
    if any(li.amount is None for li in inv.lines):
        return reasons, {"lines": "skipped: a line amount is unreadable"}, None

    before = [li for li in inv.lines if li.tax_stage == "before_tax"]
    after = [li for li in inv.lines if li.tax_stage == "after_tax"]
    items_sum = sum((li.amount for li in before if li.kind == "item"), Decimal(0))
    before_sum = sum((li.amount for li in before), Decimal(0))
    after_sum = sum((li.amount for li in after), Decimal(0))

    # Subtotal vs lines
    if inv.subtotal is not None:
        tol = unit * max(1, len(before))
        ok = _close(inv.subtotal, before_sum, tol) or _close(inv.subtotal, items_sum, tol)
        checks["subtotal_vs_lines"] = {"passed": ok, "subtotal": str(inv.subtotal),
                                       "before_tax_lines": str(before_sum), "item_lines": str(items_sum)}
        if not ok:
            reasons.append(_issue("lines_mismatch", "subtotal",
                                  f"subtotal {inv.subtotal} ≠ sum of lines {before_sum}"))

    # Tax lines vs tax total
    tax_amounts = [t.amount for t in inv.taxes if t.amount is not None]
    tax_sum = sum(tax_amounts, Decimal(0)) if inv.taxes and len(tax_amounts) == len(inv.taxes) else None
    if tax_sum is not None and inv.tax_total is not None:
        tol = unit * max(1, len(inv.taxes))
        non_withholding = sum((t.amount for t in inv.taxes if t.tax_type != "withholding"), Decimal(0))
        ok = _close(inv.tax_total, tax_sum, tol) or _close(inv.tax_total, non_withholding, tol)
        checks["tax_total_vs_tax_lines"] = {"passed": ok, "tax_total": str(inv.tax_total), "tax_lines": str(tax_sum)}
        if not ok:
            reasons.append(_issue("tax_mismatch", "tax_total", f"tax total {inv.tax_total} ≠ sum of tax lines {tax_sum}"))

    # Grand total
    tax_mode = None
    if inv.total is not None:
        tax = tax_sum if tax_sum is not None else (inv.tax_total or Decimal(0))
        tol = unit * max(1, len(inv.lines) + len(inv.taxes))
        exclusive = before_sum + tax + after_sum
        inclusive = before_sum + after_sum
        if _close(inv.total, exclusive, tol):
            tax_mode = "exclusive"
        elif tax != 0 and _close(inv.total, inclusive, tol):
            tax_mode = "inclusive"
        checks["total"] = {"passed": tax_mode is not None, "total": str(inv.total),
                           "computed_tax_exclusive": str(exclusive), "computed_tax_inclusive": str(inclusive),
                           "tax_mode": tax_mode}
        if tax_mode is None:
            reasons.append(_issue("total_mismatch", "total",
                                  f"total {inv.total} ≠ lines + adjustments + tax {exclusive}"))
    return reasons, checks, tax_mode


def validate(inv: CommonInvoice, *, confidence_threshold: float,
             duplicates: list[Duplicate] | None = None) -> Validation:
    duplicates = duplicates or []
    reasons: list[Issue] = []
    info: list[Issue] = []
    checks: dict[str, Any] = {}

    # Confidence = OCR page-average confidence × required-field completeness (both stored)
    completeness = round(sum(_present(inv, f) for f in REQUIRED_FIELDS) / len(REQUIRED_FIELDS), 4)
    ocr = inv.extraction.ocr_confidence_avg
    confidence = round(ocr * completeness, 4) if ocr is not None else None

    def result(status: str, tax_mode: str | None = None) -> Validation:
        return Validation(status, reasons, checks, confidence, ocr, completeness, tax_mode, info)

    # --- Rejections: nothing a human could do with it
    readable = len(re.sub(r"\W", "", inv.ocr_text or ""))
    checks["readable"] = {"passed": readable >= MIN_READABLE_CHARS, "characters": readable}
    if readable < MIN_READABLE_CHARS:
        reasons.append(_issue("unreadable", None, "no readable text in the document"))
        return result("rejected")
    issue_codes = {i.code for i in inv.issues}
    if "not_invoice" in issue_codes:
        # Guard against a document wrongly classified (or a prompt injection saying 'not an invoice'):
        # if it carries an invoice number and a total, a human should look at it.
        if inv.invoice_number and inv.total is not None:
            reasons.append(_issue("classification_conflict", "document_kind",
                                  "classified as not an invoice, but has an invoice number and a total"))
            return result("needs_review")
        reasons.append(next(i for i in inv.issues if i.code == "not_invoice"))
        return result("rejected")

    # --- Normalization issues from extraction (ambiguous date, unreadable amount, currency...)
    for i in inv.issues:
        (info if i.code in INFO_ISSUES else reasons).append(i)
    if "extraction_invalid" in issue_codes:
        return result("needs_review")       # readable text: a human can key it in

    # --- Required fields
    missing = [f for f in REQUIRED_FIELDS if not _present(inv, f)]
    checks["required_fields"] = {"passed": not missing, "missing": missing}
    for f in missing:
        if not any(r.field == f for r in reasons):          # avoid double-reporting
            reasons.append(_issue("missing_field", f, f"{f} not found"))

    # --- Sign
    if inv.total is not None:
        wrong = (inv.document_type == "invoice" and inv.total < 0) or (inv.document_type == "credit_note" and inv.total > 0)
        checks["sign"] = {"passed": not wrong, "document_type": inv.document_type, "total": str(inv.total)}
        if wrong:
            reasons.append(_issue("wrong_sign", "total",
                                  f"{inv.document_type} with total {inv.total}: check the document type"))

    # --- Dates
    if inv.invoice_date and inv.due_date:
        ok = inv.due_date >= inv.invoice_date
        checks["due_after_invoice_date"] = {"passed": ok}
        if not ok:
            reasons.append(_issue("due_before_invoice_date", "due_date",
                                  f"due date {inv.due_date} is before invoice date {inv.invoice_date}"))

    # --- Totals (lines, discounts, shipping, rounding, tax)
    total_reasons, total_checks, tax_mode = totals_check(inv)
    reasons += total_reasons
    checks["totals"] = total_checks

    # --- Possible duplicate (same supplier + document type + number, different file)
    checks["duplicate"] = {"passed": not duplicates, "matches": [d.invoice_id for d in duplicates]}
    for d in duplicates:
        reasons.append(_issue("possible_duplicate", "invoice_number",
                              f"same supplier and number as invoice {d.invoice_id} ({d.status})"))

    # --- Confidence (threshold is provisional, calibrated on the test set on Day 3)
    checks["confidence"] = {"passed": confidence is not None and confidence >= confidence_threshold,
                            "score": confidence, "ocr": ocr, "completeness": completeness,
                            "threshold": confidence_threshold}
    if confidence is None:
        reasons.append(_issue("no_ocr_confidence", None, "the provider returned no OCR confidence"))
    elif confidence < confidence_threshold:
        reasons.append(_issue("low_confidence", None,
                              f"confidence {confidence} below threshold {confidence_threshold}"))

    return result("needs_review" if reasons else "ready", tax_mode)
