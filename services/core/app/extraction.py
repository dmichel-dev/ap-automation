"""Provider output → common invoice format. Deterministic: every interpretation of the
'as printed' values happens here, and anything uncertain becomes an Issue."""

from collections.abc import Callable
from datetime import date
from decimal import Decimal

from pydantic import ValidationError

from . import normalize as n
from .models import (
    CommonInvoice,
    DateInfo,
    ExtractionMeta,
    ExtractionResult,
    InvoiceLine,
    Issue,
    Normalized,
    Party,
    TaxLine,
)
from .providers.base import ProviderResult


class _Issues(list[Issue]):
    def add(self, code: str, field: str | None, message: str) -> None:
        self.append(Issue(code=code, field=field, message=message))


def _parsed[T](parse: Callable[[str | None], T], raw: str | None, field: str, issues: _Issues) -> T | None:
    """Runs a normalize parser; an unreadable value becomes an Issue and None."""
    try:
        return parse(raw)
    except n.NormalizationError as exc:
        issues.add(exc.code, field, str(exc))
        return None


def _amount(raw: str | None, currency: str | None, field: str, issues: _Issues) -> Decimal | None:
    return _parsed(lambda r: n.parse_amount(r, currency), raw, field, issues)


def _rate(raw: str | None, field: str, issues: _Issues) -> Decimal | None:
    return _parsed(n.parse_rate, raw, field, issues)


def _date(raw: str | None, field: str, issues: _Issues) -> n.ParsedDate | None:
    return _parsed(n.parse_date, raw, field, issues)


def _resolve_dates(
    inv: n.ParsedDate | None, due: n.ParsedDate | None, country: str | None, info: DateInfo, issues: _Issues
) -> tuple[date | None, date | None]:
    """A document uses one date convention: if one date proves the order (e.g. 23/11/2026),
    it resolves the other. If still ambiguous, flag it and suggest an order from the country."""
    proven = {p.order for p in (inv, due) if p and p.order}
    if len(proven) == 1:
        proven_order = proven.pop()
        inv, due = (n.ParsedDate(p.reading(proven_order)) if p and p.ambiguous else p for p in (inv, due))
    elif len(proven) == 2:
        issues.add("inconsistent_dates", "invoice_date", "the two dates use different day/month orders")

    ambiguous = [(name, p) for name, p in (("invoice_date", inv), ("due_date", due)) if p and p.ambiguous]
    if ambiguous:
        _suggest_order(inv, due, country, info)
        for name, p in ambiguous:
            issues.add("ambiguous_date", name, f"{p.dmy} (day first) or {p.mdy} (month first); {info.suggestion_basis}")
    return (inv.value if inv else None), (due.value if due else None)


def _suggest_order(inv: n.ParsedDate | None, due: n.ParsedDate | None, country: str | None, info: DateInfo) -> None:
    """Records the order suggested by the supplier country, for one-click confirmation."""
    info.ambiguous = True
    order, info.suggestion_basis = n.date_order_suggestion(country)
    if order:
        info.suggested_order = order
        info.suggested_invoice_date = inv.reading(order) if inv else None
        info.suggested_due_date = due.reading(order) if due else None


def build_common_invoice(result: ProviderResult, provider_name: str) -> CommonInvoice:
    issues = _Issues()
    meta = ExtractionMeta(
        provider=provider_name,
        model=result.model,
        pages=result.pages,
        ocr_confidence_avg=result.ocr_confidence_avg,
        ocr_confidence_min=result.ocr_confidence_min,
    )
    data = _annotation(result, issues)
    if data is None:
        return CommonInvoice(
            is_invoice=False,
            supplier=Party(),
            buyer=Party(),
            dates=DateInfo(),
            lines=[],
            taxes=[],
            ocr_text=result.ocr_text,
            normalized=Normalized(),
            extraction=meta,
            issues=issues,
        )

    is_invoice = data.document_kind != "not_invoice"
    if not is_invoice:
        issues.add("not_invoice", "document_kind", "the document is not an invoice or a credit note")
    country = n.resolve_country(data.supplier_country_code, data.supplier_tax_id)
    currency = _currency(data, country, result.ocr_text, issues)
    info = DateInfo(invoice_date_raw=n.clean_text(data.invoice_date), due_date_raw=n.clean_text(data.due_date))
    invoice_date, due_date = _resolve_dates(
        _date(data.invoice_date, "invoice_date", issues),
        _date(data.due_date, "due_date", issues),
        country,
        info,
        issues,
    )
    lines = _lines(data, currency, issues)
    taxes = _tax_lines(data, currency, issues)
    lines = _drop_taxes_repeated_as_adjustments(lines, taxes, len(data.line_items), issues)
    totals = [_amount(raw, currency, name, issues) for name, raw in _printed_totals(data)]
    document_type = data.document_kind if is_invoice else None
    if document_type == "credit_note":
        totals = _sign_credit_note(totals, lines, taxes)
    subtotal, tax_total, total = totals

    return CommonInvoice(
        is_invoice=is_invoice,
        document_type=document_type,
        supplier=Party(
            name=n.clean_text(data.supplier_name),
            tax_id=n.clean_text(data.supplier_tax_id),
            country=country,
            address=n.clean_text(data.supplier_address),
        ),
        buyer=Party(name=n.clean_text(data.buyer_name), tax_id=n.clean_text(data.buyer_tax_id)),
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


def _printed_totals(data: ExtractionResult) -> list[tuple[str, str | None]]:
    return [("subtotal", data.subtotal), ("tax_total", data.tax_total), ("total", data.total)]


def _annotation(result: ProviderResult, issues: _Issues) -> ExtractionResult | None:
    """The provider's structured output, if it matches the schema."""
    if result.annotation_error:
        issues.add("extraction_invalid", None, result.annotation_error)
        return None
    try:
        return ExtractionResult.model_validate(result.annotation)
    except ValidationError as exc:
        issues.add(
            "extraction_invalid", None, f"provider output does not match the schema: {exc.error_count()} error(s)"
        )
        return None


def _currency(data: ExtractionResult, country: str | None, ocr_text: str, issues: _Issues) -> str | None:
    currency, note = n.resolve_currency([data.currency, data.total], country)
    if currency is None:
        issues.add("ambiguous_currency" if note else "missing_currency", "currency", note or "no currency found")
    elif note:
        issues.add("currency_from_country", "currency", note)
    if currency and not n.currency_evidence(currency, ocr_text):
        issues.add(
            "currency_not_in_document",
            "currency",
            f"{currency} was extracted but neither the code nor a matching symbol is in the document text",
        )
    return currency


def _lines(data: ExtractionResult, currency: str | None, issues: _Issues) -> list[InvoiceLine]:
    """Table rows, then totals-section adjustments (so they count in the totals check and
    become separate bill lines in QuickBooks). A discount is always negative."""
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
    for i, adj in enumerate(data.adjustments, start=len(lines) + 1):
        lines.append(
            InvoiceLine(
                line_no=i,
                kind=adj.kind,
                tax_stage=adj.position,
                description=n.clean_text(adj.label),
                amount=_amount(adj.amount, currency, f"lines[{i}].amount", issues),
            )
        )
    for li in lines:
        # A discount reduces the amount, whether or not the PDF prints a minus sign
        if li.kind == "discount" and li.amount is not None:
            li.amount = -abs(li.amount)
            li.unit_price = -abs(li.unit_price) if li.unit_price is not None else None
    return lines


def _tax_lines(data: ExtractionResult, currency: str | None, issues: _Issues) -> list[TaxLine]:
    taxes = [
        TaxLine(
            tax_type=t.tax_type,
            label=n.clean_text(t.label),
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
    return taxes


def _drop_taxes_repeated_as_adjustments(
    lines: list[InvoiceLine], taxes: list[TaxLine], table_rows: int, issues: _Issues
) -> list[InvoiceLine]:
    """A tax (often a withholding) sometimes comes back twice: as a tax line and as an
    'other' totals adjustment. Counting both would break the totals check, so an 'other'
    adjustment with the same amount as a tax line is dropped, and the drop is recorded.
    Table rows are never dropped."""
    tax_amounts = {abs(t.amount) for t in taxes if t.amount is not None}
    kept = []
    for li in lines:
        if li.line_no > table_rows and li.kind == "other" and li.amount is not None and abs(li.amount) in tax_amounts:
            issues.add(
                "adjustment_repeats_tax",
                f"lines[{li.line_no}]",
                f"'{li.description}' ({li.amount}) repeats a tax line; counted once, as tax",
            )
            continue
        kept.append(li)
    return [li.model_copy(update={"line_no": i}) for i, li in enumerate(kept, start=1)]


def _flip(v: Decimal | None) -> Decimal | None:
    return -v if v is not None else None


def _sign_credit_note(
    totals: list[Decimal | None], lines: list[InvoiceLine], taxes: list[TaxLine]
) -> list[Decimal | None]:
    """Credit notes are stored signed (negative), whatever the PDF prints."""
    total = totals[-1]
    if total is None or total <= 0:
        return totals
    for li in lines:
        li.unit_price, li.amount = _flip(li.unit_price), _flip(li.amount)
    for t in taxes:
        t.taxable_base, t.amount = _flip(t.taxable_base), _flip(t.amount)
    return [_flip(v) for v in totals]
