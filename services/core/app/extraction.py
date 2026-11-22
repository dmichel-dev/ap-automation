"""Provider output → common invoice format. Deterministic: every interpretation of the
'as printed' values happens here, and anything uncertain becomes an Issue."""

from decimal import Decimal

from pydantic import ValidationError

from . import normalize as n
from .models import (
    CommonInvoice, DateInfo, ExtractionMeta, ExtractionResult, InvoiceLine, Issue,
    Normalized, Party, TaxLine,
)
from .providers.base import ProviderResult


class _Issues(list):
    def add(self, code: str, field: str | None, message: str) -> None:
        self.append(Issue(code=code, field=field, message=message))


def _amount(raw: str | None, currency: str | None, field: str, issues: _Issues) -> Decimal | None:
    try:
        return n.parse_amount(raw, currency)
    except n.NormalizationError as exc:
        issues.add(exc.code, field, str(exc))
        return None


def _rate(raw: str | None, field: str, issues: _Issues) -> Decimal | None:
    try:
        return n.parse_rate(raw)
    except n.NormalizationError as exc:
        issues.add(exc.code, field, str(exc))
        return None


def _date(raw: str | None, field: str, issues: _Issues) -> n.ParsedDate | None:
    try:
        return n.parse_date(raw)
    except n.NormalizationError as exc:
        issues.add(exc.code, field, str(exc))
        return None


def _resolve_dates(inv: n.ParsedDate | None, due: n.ParsedDate | None, country: str | None,
                   info: DateInfo, issues: _Issues):
    """A document uses one date convention: if one date proves the order (e.g. 23/11/2026),
    it resolves the other. If still ambiguous, flag it and suggest an order from the country."""
    proven = {p.order for p in (inv, due) if p and p.order}
    if len(proven) == 1:
        order = proven.pop()
        inv = n.ParsedDate(inv.dmy if order == "DMY" else inv.mdy) if inv and inv.ambiguous else inv
        due = n.ParsedDate(due.dmy if order == "DMY" else due.mdy) if due and due.ambiguous else due
    elif len(proven) == 2:
        issues.add("inconsistent_dates", "invoice_date", "the two dates use different day/month orders")

    ambiguous = [(name, p) for name, p in (("invoice_date", inv), ("due_date", due)) if p and p.ambiguous]
    if ambiguous:
        info.ambiguous = True
        order, basis = n.date_order_suggestion(country)
        info.suggestion_basis = basis
        if order:
            info.suggested_order = order
            pick = (lambda p: p.dmy) if order == "DMY" else (lambda p: p.mdy)
            for p, attr in ((inv, "suggested_invoice_date"), (due, "suggested_due_date")):
                if p:
                    setattr(info, attr, pick(p) if p.ambiguous else p.value)
        for name, p in ambiguous:
            issues.add("ambiguous_date", name,
                       f"{p.dmy} (day first) or {p.mdy} (month first); {basis}")
    return (inv.value if inv else None), (due.value if due else None)


def build_common_invoice(result: ProviderResult, provider_name: str) -> CommonInvoice:
    issues = _Issues()
    meta = ExtractionMeta(
        provider=provider_name, model=result.model, pages=result.pages,
        ocr_confidence_avg=result.ocr_confidence_avg, ocr_confidence_min=result.ocr_confidence_min,
    )

    data: ExtractionResult | None = None
    if result.annotation_error:
        issues.add("extraction_invalid", None, result.annotation_error)
    else:
        try:
            data = ExtractionResult.model_validate(result.annotation)
        except ValidationError as exc:
            issues.add("extraction_invalid", None, f"provider output does not match the schema: {exc.error_count()} error(s)")

    if data is None:
        return CommonInvoice(
            is_invoice=False, supplier=Party(), buyer=Party(), dates=DateInfo(), lines=[], taxes=[],
            ocr_text=result.ocr_text, normalized=Normalized(), extraction=meta, issues=issues,
        )

    is_invoice = data.document_kind != "not_invoice"
    if not is_invoice:
        issues.add("not_invoice", "document_kind", "the document is not an invoice or a credit note")

    country = n.resolve_country(data.supplier_country_code, data.supplier_tax_id)
    supplier = Party(
        name=n.clean_text(data.supplier_name), tax_id=n.clean_text(data.supplier_tax_id),
        country=country, address=n.clean_text(data.supplier_address),
    )
    buyer = Party(name=n.clean_text(data.buyer_name), tax_id=n.clean_text(data.buyer_tax_id))

    currency, note = n.resolve_currency([data.currency, data.total], country)
    if currency is None:
        issues.add("ambiguous_currency" if note else "missing_currency", "currency", note or "no currency found")
    elif note:
        issues.add("currency_from_country", "currency", note)
    if currency and not n.currency_evidence(currency, result.ocr_text):
        issues.add("currency_not_in_document", "currency",
                   f"{currency} was extracted but neither the code nor a matching symbol is in the document text")

    info = DateInfo(invoice_date_raw=n.clean_text(data.invoice_date), due_date_raw=n.clean_text(data.due_date))
    invoice_date, due_date = _resolve_dates(
        _date(data.invoice_date, "invoice_date", issues), _date(data.due_date, "due_date", issues),
        country, info, issues,
    )

    lines = [
        InvoiceLine(
            line_no=i,
            kind=li.kind,
            description=n.clean_text(li.description),
            quantity=_amount(li.quantity, None, f"lines[{i}].quantity", issues),
            unit_price=_amount(li.unit_price, currency, f"lines[{i}].unit_price", issues),
            amount=_amount(li.amount, currency, f"lines[{i}].amount", issues),
            tax_rate=_rate(li.tax_rate, f"lines[{i}].tax_rate", issues),
        )
        for i, li in enumerate(data.line_items, start=1)
    ]
    # Totals-section adjustments become lines too, so they count in the totals check
    # and become separate bill lines in QuickBooks (Day 2)
    for adj in data.adjustments:
        i = len(lines) + 1
        lines.append(InvoiceLine(
            line_no=i, kind=adj.kind, tax_stage=adj.position, description=n.clean_text(adj.label),
            amount=_amount(adj.amount, currency, f"lines[{i}].amount", issues),
        ))
    for li in lines:
        # A discount reduces the amount, whether or not the PDF prints a minus sign
        if li.kind == "discount" and li.amount is not None:
            li.amount = -abs(li.amount)
            li.unit_price = -abs(li.unit_price) if li.unit_price is not None else None

    taxes = [
        TaxLine(
            tax_type=t.tax_type, label=n.clean_text(t.label),
            rate=_rate(t.rate, f"taxes[{i}].rate", issues),
            taxable_base=_amount(t.taxable_base, currency, f"taxes[{i}].taxable_base", issues),
            amount=_amount(t.amount, currency, f"taxes[{i}].amount", issues),
        )
        for i, t in enumerate(data.tax_lines, start=1)
    ]
    for t in taxes:
        # Withholding tax is deducted from the amount payable
        if t.tax_type == "withholding" and t.amount is not None:
            t.amount = -abs(t.amount)
    subtotal = _amount(data.subtotal, currency, "subtotal", issues)
    tax_total = _amount(data.tax_total, currency, "tax_total", issues)
    total = _amount(data.total, currency, "total", issues)

    document_type = None
    if is_invoice:
        document_type = data.document_kind
        # Credit notes are stored signed (negative), whatever the PDF prints
        if document_type == "credit_note" and total is not None and total > 0:
            flip = lambda v: -v if v is not None else None  # noqa: E731
            subtotal, tax_total, total = flip(subtotal), flip(tax_total), flip(total)
            for li in lines:
                li.unit_price, li.amount = flip(li.unit_price), flip(li.amount)
            for t in taxes:
                t.taxable_base, t.amount = flip(t.taxable_base), flip(t.amount)

    return CommonInvoice(
        is_invoice=is_invoice,
        document_type=document_type,
        supplier=supplier,
        buyer=buyer,
        invoice_number=n.clean_text(data.invoice_number),
        related_invoice_number=n.clean_text(data.related_invoice_number),
        invoice_date=invoice_date,
        due_date=due_date,
        dates=info,
        currency=currency,
        lines=lines,
        taxes=taxes,
        subtotal=subtotal,
        tax_total=tax_total,
        total=total,
        language=n.normalize_language(data.language),
        ocr_text=result.ocr_text,
        normalized=Normalized(
            invoice_number=n.normalize_invoice_number(data.invoice_number),
            supplier_name=n.normalize_supplier_name(data.supplier_name),
            supplier_tax_id=n.normalize_tax_id(data.supplier_tax_id),
        ),
        extraction=meta,
        issues=issues,
    )
