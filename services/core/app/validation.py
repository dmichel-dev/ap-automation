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

from .bank_details import BankCheck
from .formatting import below_threshold, day, mismatch, money
from .models import CommonInvoice, Issue
from .reference import minor_units

REQUIRED_FIELDS = ("supplier_name", "invoice_number", "invoice_date", "currency", "total")
MIN_READABLE_CHARS = 20

# Normalization issues that are information only (don't block auto-posting)
INFO_ISSUES = {"currency_from_country", "adjustment_repeats_tax"}


@dataclass(frozen=True)
class Policy:
    """Company accounting policy settings (configurable, see docs/validation.md)."""

    home_currency: str = "USD"
    # Cut-off: a prior-period service goes to review only at or above this amount (home currency)
    cutoff_materiality: Decimal = Decimal("1000")
    # Capitalisation policy: an item at or above this amount may be an asset; a person decides
    capex_threshold: Decimal = Decimal("2500")


DUPLICATE_RULES = {
    "same_number": "same supplier and number as invoice {id} ({status})",
    "same_amount_near_date": "same supplier, total and currency as invoice {id} ({status}), "
    "dated within {days} days, with a different number",
}
DUPLICATE_DATE_WINDOW_DAYS = 7


@dataclass
class Duplicate:
    invoice_id: str
    status: str
    rule: str = "same_number"  # a key of DUPLICATE_RULES


@dataclass(frozen=True)
class SupplierStatus:
    """What the company already knows about the supplier (looked up in the database).

    known: a default account was set, the supplier is linked to a vendor in the accounting
    system, or exactly one active vendor there has the same normalized name. In the public demo:
    the supplier of one of its samples (demo_samples.supplier_status).
    """

    known: bool
    basis: str | None = None  # supplier_default | vendor_link | accounting_vendor_name | demo_sample_supplier
    default_account_id: str | None = None


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
        f.fail("lines_mismatch", "subtotal", mismatch("lines", inv.subtotal, before_sum, inv.currency))


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
            f.fail("tax_mismatch", "tax_total", mismatch("tax", inv.tax_total, tax_sum, inv.currency))
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
        f.fail("total_mismatch", "total", mismatch("total", inv.total, exclusive, inv.currency))
    return tax_mode


def validate(
    inv: CommonInvoice,
    *,
    confidence_threshold: float,
    duplicates: list[Duplicate] | None = None,
    policy: Policy | None = None,
    supplier: SupplierStatus | None = None,
    bank: BankCheck | None = None,
) -> Validation:
    """supplier=None and bank=None skip the checks that need the database (callers without one)."""
    f = _Findings()
    # Confidence = OCR page-average confidence × required-field completeness (both stored)
    completeness = round(sum(_present(inv, name) for name in REQUIRED_FIELDS) / len(REQUIRED_FIELDS), 4)
    ocr = inv.extraction.ocr_confidence_avg
    confidence = round(ocr * completeness, 4) if ocr is not None else None

    def result(status: str, tax_mode: str | None = None) -> Validation:
        return Validation(status, f.reasons, f.checks, confidence, ocr, completeness, tax_mode, f.info)

    # Rejections (nothing a human could do with it) and early stops
    if stop := _readability_and_kind(inv, f):
        if stop == "rejected" and bank and bank.notice_sources:
            # A bank-details change request is a fraud alert, never a plain rejection
            f.info, f.reasons, stop = f.info + f.reasons, [], "needs_review"
        if stop == "needs_review":
            _bank_details_check(bank, f)
        return result(stop)
    for i in inv.issues:  # normalization issues (ambiguous date, unreadable amount, currency...)
        (f.info if i.code in INFO_ISSUES else f.reasons).append(i)
    if any(i.code == "extraction_invalid" for i in inv.issues):
        _bank_details_check(bank, f)
        return result("needs_review")  # readable text: a human can key it in

    _required_fields_check(inv, f)
    _sign_check(inv, f)
    _dates_check(inv, f)
    _cutoff_check(inv, policy or Policy(), f)
    _capex_check(inv, policy or Policy(), f)
    total_reasons, f.checks["totals"], tax_mode = totals_check(inv)
    f.reasons += total_reasons
    _duplicates_check(duplicates or [], f)
    _supplier_check(inv, supplier, f)
    _bank_details_check(bank, f)
    _confidence_check(confidence, ocr, completeness, confidence_threshold, f)
    return result("needs_review" if f.reasons else "ready", tax_mode)


def posting_blockers(inv: CommonInvoice) -> tuple[list[Issue], str | None]:
    """After a person's review: what still prevents posting, and the totals formula that matched.

    Blocks: a fact the bill needs (a real date, not an ambiguous one), a wrong sign, inconsistent
    dates, totals that don't add up. Judgement reasons (possible duplicate, new supplier, capex,
    cut-off, low confidence) are settled by the reviewer's decision and don't block."""
    f = _Findings()
    present = {"supplier_name": inv.supplier.name, "document_type": inv.document_type}
    for name in (*REQUIRED_FIELDS, "document_type"):
        if present.get(name, getattr(inv, name, None)) in (None, ""):
            f.fail("missing_field", name, f"{name} is needed to post")
    _sign_check(inv, f)
    _dates_check(inv, f)
    start, end = inv.service_period_start, inv.service_period_end
    if start and end and end < start:
        f.fail(
            "service_period_inverted",
            "service_period_end",
            f"service period ends {day(end)}, before it starts on {day(start)}",
        )
    total_reasons, _, tax_mode = totals_check(inv)
    return f.reasons + total_reasons, tax_mode


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
        kind = (inv.document_type or "document").replace("_", " ")
        f.fail("wrong_sign", "total", f"{kind} with total {money(inv.total, inv.currency)}: check the document type")


def _dates_check(inv: CommonInvoice, f: _Findings) -> None:
    if not (inv.invoice_date and inv.due_date):
        return
    ok = inv.due_date >= inv.invoice_date
    f.checks["due_after_invoice_date"] = {"passed": ok}
    if not ok:
        f.fail(
            "due_before_invoice_date",
            "due_date",
            f"due date {day(inv.due_date)} is before invoice date {day(inv.invoice_date)}",
        )


def _cutoff_check(inv: CommonInvoice, policy: Policy, f: _Findings) -> None:
    """Service period ending before the accounting period (calendar month) of the invoice date.
    Usual for invoices received early in a month: listed for month-end accruals, and sent to review
    only when material. Closed periods (QuickBooks book close date) are checked at posting."""
    start, end = inv.service_period_start, inv.service_period_end
    if start and end and end < start:
        f.fail(
            "service_period_inverted",
            "service_period_end",
            f"service period ends {day(end)}, before it starts on {day(start)}",
        )
    if not (end and inv.invoice_date) or end >= inv.invoice_date.replace(day=1):
        return
    message = f"service period ends {day(end)}, before the accounting period of the invoice ({inv.invoice_date:%b %Y})"
    material, basis = _material(inv, policy)
    f.checks["cutoff"] = {"prior_period": True, "service_period_end": str(end), "material": material, "basis": basis}
    target = f.reasons if material else f.info
    target.append(_issue("cutoff_prior_period", "service_period_end", f"{message}; {basis}"))


def _material(inv: CommonInvoice, policy: Policy) -> tuple[bool, str]:
    if inv.currency != policy.home_currency or inv.total is None:
        # No home-currency amount before FX conversion exists: a person looks at it
        return True, "amount in the home currency not known yet"
    if abs(inv.total) >= policy.cutoff_materiality:
        return True, f"amount at or above the materiality of {money(policy.cutoff_materiality, policy.home_currency)}"
    return False, f"below the materiality of {money(policy.cutoff_materiality, policy.home_currency)}"


def _capex_check(inv: CommonInvoice, policy: Policy, f: _Findings) -> None:
    """Capitalisation policy: an item whose cost (unit price when printed, otherwise the line amount)
    reaches the threshold may be an asset rather than an expense. Never decided here."""
    if inv.document_type != "invoice":
        return
    if inv.currency != policy.home_currency:
        f.checks["capex"] = {"checked": False, "why": "foreign currency: checked once FX conversion exists"}
        return
    found = []
    for li in inv.lines:
        cost = li.unit_price if li.unit_price is not None and li.quantity else li.amount
        if li.kind == "item" and cost is not None and cost >= policy.capex_threshold:
            found.append(li.line_no)
            f.fail(
                "possible_capex",
                "lines",
                f"line {li.line_no}: item cost {money(cost, inv.currency)} reaches the capitalisation threshold "
                f"{money(policy.capex_threshold, policy.home_currency)}; decide expense or fixed asset",
            )
    f.checks["capex"] = {"checked": True, "threshold": str(policy.capex_threshold), "lines": found}


def _duplicates_check(duplicates: list[Duplicate], f: _Findings) -> None:
    """Possible duplicate: same supplier + document type + number, different file."""
    f.checks["duplicate"] = {
        "passed": not duplicates,
        "matches": [d.invoice_id for d in duplicates],
        "rules": [d.rule for d in duplicates],
    }
    for d in duplicates:
        message = DUPLICATE_RULES[d.rule].format(id=d.invoice_id, status=d.status, days=DUPLICATE_DATE_WINDOW_DAYS)
        f.fail("possible_duplicate", "invoice_number" if d.rule == "same_number" else "total", message)


def _supplier_check(inv: CommonInvoice, supplier: SupplierStatus | None, f: _Findings) -> None:
    """Supplier onboarding control and expense coding. A supplier's first invoice is always seen by
    a person (genuine supplier?); invoices are posted only to an account a person chose."""
    if supplier is None or not inv.supplier.name:
        return  # no supplier name: already a missing_field
    f.checks["supplier"] = {
        "known": supplier.known,
        "basis": supplier.basis,
        "default_account_id": supplier.default_account_id,
    }
    if not supplier.known:
        f.fail("new_vendor", "supplier_name", "first invoice from this supplier: check that it is a genuine supplier")
    if supplier.default_account_id is None:
        f.fail("expense_account_missing", "expense_account", "no default account for this supplier: choose one")


def _bank_details_check(bank: BankCheck | None, f: _Findings) -> None:
    """A possible change of bank details is never posted automatically: an announcement (document or
    email) or an account not on the supplier's earlier invoices sends it to review. Messages are masked."""
    if bank is None:
        return
    f.checks["bank_details"] = {
        "passed": not bank.notice_sources and not bank.new_accounts,
        "accounts": len(bank.accounts),
        "notice": bank.notice_sources,
        "new": bank.new_accounts,
        "known": bank.known_accounts,
        "first_seen": bank.first_seen,
    }
    messages = []
    if where := _notice_places(bank.notice_sources):
        messages.append(f"The {where} new or changed bank details.")
    if bank.new_accounts:
        messages.append(
            f"{', '.join(bank.new_accounts)} is not on this supplier's earlier invoices "
            f"({', '.join(bank.known_accounts)})."
        )
    if messages:
        f.fail("possible_bank_detail_change", "bank_accounts", " ".join(messages))
    if bank.first_seen:
        labels = ", ".join(a["label"] for a in bank.accounts)
        f.info.append(
            _issue(
                "bank_details_first_seen",
                "bank_accounts",
                f"First bank details seen for this supplier ({labels}): read from the document, not verified.",
            )
        )


def _notice_places(sources: list[str]) -> str | None:
    """'document announces', 'email announces', 'document and the email announce'."""
    places = list(dict.fromkeys(s.split("_")[0].split(":")[0] for s in sources))
    if not places:
        return None
    return f"{places[0]} announces" if len(places) == 1 else f"{places[0]} and the {places[1]} announce"


def _confidence_check(
    confidence: float | None, ocr: float | None, completeness: float, threshold: float, f: _Findings
) -> None:
    """The threshold is provisional until it is calibrated on the test set."""
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
        f.fail("low_confidence", None, below_threshold(confidence, threshold))
