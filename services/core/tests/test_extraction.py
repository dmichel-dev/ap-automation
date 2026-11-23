import json
from datetime import date
from decimal import Decimal

from app.extraction import build_common_invoice
from app.providers.base import ProviderResult
from app.providers.mistral import MistralOcrProvider

from .conftest import FIXTURES, base_annotation


def annotation(**overrides):
    return base_annotation(**overrides)


def build(ocr_text="Total due: USD 641.24 (all amounts in $)", **overrides):
    result = ProviderResult(
        annotation=annotation(**overrides),
        ocr_text=ocr_text,
        pages=1,
        model="test",
        ocr_confidence_avg=0.98,
        ocr_confidence_min=0.6,
    )
    return build_common_invoice(result, "test")


def codes(inv):
    return [i.code for i in inv.issues]


def test_clean_us_invoice():
    inv = build()
    assert inv.is_invoice and inv.document_type == "invoice"
    assert inv.currency == "USD" and inv.supplier.country == "US"
    assert inv.invoice_date == date(2026, 11, 18) and inv.due_date == date(2026, 12, 18)
    assert inv.total == Decimal("641.24") and inv.lines[1].amount == Decimal("171.00")
    assert inv.taxes[0].rate == Decimal("8.5")
    assert inv.normalized.invoice_number == "QM1042"
    assert inv.normalized.supplier_name == "quillmoor paper supply"
    assert codes(inv) == []  # explicit 'USD' in the total wins over '$'


def test_ambiguous_date_us_supplier_suggests_month_first():
    inv = build(invoice_date="03/04/2026", due_date="05/04/2026", total="$641.24")
    assert inv.invoice_date is None and inv.due_date is None
    assert inv.dates.ambiguous and inv.dates.suggested_order == "MDY"
    assert inv.dates.suggested_invoice_date == date(2026, 3, 4)
    assert inv.dates.suggested_due_date == date(2026, 5, 4)
    assert codes(inv).count("ambiguous_date") == 2


def test_ambiguous_date_french_supplier_suggests_day_first():
    inv = build(supplier_country_code="FR", currency="EUR", invoice_date="03/04/2026", due_date="2026-05-03")
    assert inv.invoice_date is None and inv.due_date == date(2026, 5, 3)
    assert inv.dates.suggested_order == "DMY"
    assert inv.dates.suggested_invoice_date == date(2026, 4, 3)
    assert inv.dates.suggested_due_date == date(2026, 5, 3)


def test_ambiguous_date_resolved_by_other_date_on_same_document():
    # 30/04 proves day-first, so 03/04 is 3 April: no review needed for the date
    inv = build(supplier_country_code="FR", currency="EUR", invoice_date="03/04/2026", due_date="30/04/2026")
    assert inv.invoice_date == date(2026, 4, 3) and not inv.dates.ambiguous
    assert "ambiguous_date" not in codes(inv)


def test_ambiguous_date_canada_has_no_suggestion():
    inv = build(supplier_country_code="CA", currency="CAD", invoice_date="03/04/2026", due_date=None)
    assert inv.dates.ambiguous and inv.dates.suggested_order is None
    assert inv.dates.suggested_invoice_date is None


def test_credit_note_amounts_are_negative():
    inv = build(document_kind="credit_note", related_invoice_number="QM-1001")
    assert inv.document_type == "credit_note"
    assert inv.total == Decimal("-641.24") and inv.subtotal == Decimal("-591.00")
    assert inv.lines[0].amount == Decimal("-420.00") and inv.taxes[0].amount == Decimal("-50.24")
    assert inv.lines[0].quantity == Decimal("10")
    assert inv.related_invoice_number == "QM-1001"


def test_credit_note_already_negative_is_kept():
    inv = build(document_kind="credit_note", total="-641.24", subtotal="-591.00", tax_total="-50.24")
    assert inv.total == Decimal("-641.24")


def test_not_invoice():
    inv = build(document_kind="not_invoice")
    assert not inv.is_invoice and inv.document_type is None
    assert "not_invoice" in codes(inv)


def test_ambiguous_currency_without_country():
    inv = build(supplier_country_code=None, supplier_tax_id=None, currency="$", total="$641.24")
    assert inv.currency is None and "ambiguous_currency" in codes(inv)


def test_currency_from_country_is_noted():
    inv = build(total="$641.24")
    assert inv.currency == "USD" and "currency_from_country" in codes(inv)


def test_currency_not_printed_on_document_is_flagged():
    # The provider says ILS but nothing in the text supports it (seen live with a glyph-less image)
    inv = build(supplier_country_code="FR", currency="ILS", total="120,00", ocr_text="Total TTC 120,00")
    assert inv.currency == "ILS" and "currency_not_in_document" in codes(inv)


def test_currency_supported_by_symbol_in_text():
    inv = build(supplier_country_code="FR", currency="€", total="120,00 €", ocr_text="Total TTC 120,00 €")
    assert inv.currency == "EUR" and "currency_not_in_document" not in codes(inv)


def test_unreadable_values_become_issues_not_errors():
    inv = build(total="see attached", invoice_date="soon")
    assert inv.total is None and inv.invoice_date is None
    assert {"unparseable_amount", "unparseable_date"} <= set(codes(inv))


def test_invalid_annotation():
    result = ProviderResult(annotation={"unexpected": True}, ocr_text="x", pages=1)
    inv = build_common_invoice(result, "test")
    assert not inv.is_invoice and codes(inv) == ["extraction_invalid"]


def test_missing_annotation():
    result = ProviderResult(annotation=None, ocr_text="x", pages=1, annotation_error="no document annotation returned")
    inv = build_common_invoice(result, "test")
    assert codes(inv) == ["extraction_invalid"]


def test_recorded_mistral_response_parses():
    """The probe response recorded from the live API (fictional invoice, partial schema)."""
    data = json.loads((FIXTURES / "mistral_us_invoice.json").read_text())
    provider = MistralOcrProvider("dummy-key")
    result = provider.parse_response(data)
    assert result.pages == 1 and "Quillmoor" in result.ocr_text
    assert result.ocr_confidence_avg is not None and 0.9 < result.ocr_confidence_avg <= 1
    assert result.annotation is not None and result.annotation["invoice_number"] == "QM-1042"
    assert "word_confidence_scores" not in json.dumps(result.raw)


def test_adjustments_become_lines_and_discount_is_negative():
    inv = build(
        adjustments=[
            {"kind": "discount", "label": "Discount 5%", "amount": "29.55", "position": "before_tax"},
            {"kind": "shipping", "label": "Shipping", "amount": "$15.00", "position": "after_tax"},
            {"kind": "rounding", "label": "Rounding", "amount": "-0.01", "position": "after_tax"},
        ]
    )
    kinds = [(li.line_no, li.kind, li.tax_stage, li.amount) for li in inv.lines[2:]]
    assert kinds == [
        (3, "discount", "before_tax", Decimal("-29.55")),
        (4, "shipping", "after_tax", Decimal("15.00")),
        (5, "rounding", "after_tax", Decimal("-0.01")),
    ]


def test_discount_row_in_table_is_negative():
    lines = annotation()["line_items"] + [
        {
            "kind": "discount",
            "description": "Loyalty discount",
            "quantity": None,
            "unit_price": None,
            "amount": "$20.00",
            "tax_rate": None,
        }
    ]
    inv = build(line_items=lines)
    assert inv.lines[2].kind == "discount" and inv.lines[2].amount == Decimal("-20.00")


def test_withholding_tax_is_negative():
    inv = build(
        tax_lines=[
            {
                "tax_type": "withholding",
                "label": "Retención 15%",
                "rate": "15%",
                "taxable_base": "591.00",
                "amount": "88.65",
            }
        ]
    )
    assert inv.taxes[0].amount == Decimal("-88.65")


def test_percentage_given_as_amount_is_flagged():
    inv = build(adjustments=[{"kind": "discount", "label": "Remise", "amount": "5 %", "position": "before_tax"}])
    assert inv.lines[2].amount is None and "unparseable_amount" in codes(inv)
