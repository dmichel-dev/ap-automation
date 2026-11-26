from decimal import Decimal

import pytest
from app.extraction import build_common_invoice
from app.providers.base import ProviderResult
from app.validation import Duplicate, Policy, validate

from .conftest import BANK_KEY, OCR_TEXT, base_annotation


def check(ocr_avg=0.98, ocr_text=OCR_TEXT, duplicates=None, threshold=0.85, policy=None, **overrides):
    result = ProviderResult(
        annotation=base_annotation(**overrides),
        ocr_text=ocr_text,
        pages=1,
        ocr_confidence_avg=ocr_avg,
        ocr_confidence_min=0.4,
    )
    inv = build_common_invoice(result, "test")
    return validate(inv, confidence_threshold=threshold, duplicates=duplicates, policy=policy)


def codes(v):
    return [r.code for r in v.reasons]


def test_low_confidence_says_both_as_whole_percentages():
    v = check(ocr_avg=0.7123)
    assert [r.message for r in v.reasons if r.code == "low_confidence"] == [
        "Confidence (71%) is below the review threshold (85%)."
    ]


def test_clean_invoice_is_ready():
    v = check()
    assert v.status == "ready", v.reasons
    assert v.confidence == 0.98 and v.confidence_ocr == 0.98 and v.confidence_completeness == 1
    assert v.tax_mode == "exclusive"


# --- rejections: nothing a human could do -------------------------------------------
def test_not_invoice_is_rejected():
    v = check(document_kind="not_invoice", invoice_number=None, total=None)
    assert v.status == "rejected" and codes(v) == ["not_invoice"]


def test_unreadable_is_rejected():
    v = check(ocr_text="  ..  ")
    assert v.status == "rejected" and codes(v) == ["unreadable"]


def test_not_invoice_with_number_and_total_goes_to_review():
    # misclassification or a prompt injection saying "this is not an invoice"
    v = check(document_kind="not_invoice")
    assert v.status == "needs_review" and codes(v) == ["classification_conflict"]


def test_invalid_extraction_with_readable_text_goes_to_review():
    result = ProviderResult(annotation={"bad": 1}, ocr_text=OCR_TEXT, pages=1, ocr_confidence_avg=0.9)
    v = validate(build_common_invoice(result, "test"), confidence_threshold=0.85)
    assert v.status == "needs_review" and codes(v) == ["extraction_invalid"]


# --- review: a human can check or fix ---------------------------------------------
def test_wrong_sign_goes_to_review():
    v = check(
        total="-641.24",
        subtotal="-591.00",
        tax_total="-50.24",
        line_items=[
            {
                "kind": "item",
                "description": "Paper",
                "quantity": "1",
                "unit_price": "-591.00",
                "amount": "-591.00",
                "tax_rate": None,
            }
        ],
        tax_lines=[{"tax_type": "sales_tax", "label": None, "rate": None, "taxable_base": None, "amount": "-50.24"}],
    )
    assert v.status == "needs_review" and codes(v) == ["wrong_sign"]


@pytest.mark.parametrize("field", ["supplier_name", "invoice_number", "currency"])
def test_missing_required_field(field):
    overrides: dict[str, str | None] = {field: None}
    if field == "currency":
        overrides["total"] = "641.24"
    v = check(**overrides)
    assert v.status == "needs_review"
    assert any(r.field == field for r in v.reasons)
    assert v.confidence_completeness < 1


def test_missing_field_reported_once():
    v = check(currency=None, total="641.24")
    assert [r.field for r in v.reasons].count("currency") == 1


def test_ambiguous_date_goes_to_review_with_suggestion_and_counts_as_present():
    v = check(invoice_date="03/04/2026", due_date="05/04/2026")
    assert v.status == "needs_review" and set(codes(v)) == {"ambiguous_date"}
    assert v.confidence_completeness == 1


def test_due_before_invoice_date():
    v = check(invoice_date="2026-11-18", due_date="2026-10-18")
    assert v.status == "needs_review" and codes(v) == ["due_before_invoice_date"]


def test_possible_duplicate():
    v = check(duplicates=[Duplicate("11111111-1111-1111-1111-111111111111", "ready")])
    assert v.status == "needs_review" and codes(v) == ["possible_duplicate"]


def test_low_confidence_threshold_is_configurable():
    assert check(ocr_avg=0.80).status == "needs_review"
    assert codes(check(ocr_avg=0.80)) == ["low_confidence"]
    assert check(ocr_avg=0.80, threshold=0.75).status == "ready"


def test_no_ocr_confidence():
    v = check(ocr_avg=None)
    assert v.status == "needs_review" and codes(v) == ["no_ocr_confidence"] and v.confidence is None


def test_currency_from_country_is_info_only():
    v = check(total="$641.24")
    assert v.status == "ready" and [i.code for i in v.info] == ["currency_from_country"]


# --- totals: lines + discount + shipping + rounding + tax ---------------------------------
def test_total_mismatch():
    v = check(total="USD 650.00")
    assert v.status == "needs_review" and codes(v) == ["total_mismatch"]
    # A plain sentence, both numbers in brackets with the currency, and the gap
    message = v.reasons[0].message
    assert message.startswith("Total (650.00 USD) doesn't match lines + adjustments + tax (") and "USD off." in message


def test_lines_mismatch():
    v = check(subtotal="$600.00", total="USD 650.24", tax_total="$50.24")
    assert "lines_mismatch" in codes(v)


def test_tax_mismatch():
    v = check(tax_total="$55.00")
    assert "tax_mismatch" in codes(v)


def test_discount_before_tax_subtotal_after_discount():
    # 591.00 - 29.55 = 561.45 ; tax 8.5% = 47.72 ; total 609.17
    v = check(
        adjustments=[{"kind": "discount", "label": "Discount 5%", "amount": "29.55", "position": "before_tax"}],
        subtotal="$561.45",
        tax_total="$47.72",
        total="USD 609.17",
        tax_lines=[
            {"tax_type": "sales_tax", "label": None, "rate": "8.5%", "taxable_base": "561.45", "amount": "47.72"}
        ],
    )
    assert v.status == "ready", v.reasons


def test_discount_before_tax_subtotal_before_discount():
    # Subtotal printed as the sum of items (591.00), discount printed below it
    v = check(
        adjustments=[{"kind": "discount", "label": "Discount", "amount": "-29.55", "position": "before_tax"}],
        subtotal="$591.00",
        tax_total="$47.72",
        total="USD 609.17",
        tax_lines=[
            {"tax_type": "sales_tax", "label": None, "rate": "8.5%", "taxable_base": "561.45", "amount": "47.72"}
        ],
    )
    assert v.status == "ready", v.reasons


def test_shipping_after_tax_and_rounding():
    # 591.00 + 50.24 + 15.00 shipping - 0.24 rounding = 656.00
    v = check(
        adjustments=[
            {"kind": "shipping", "label": "Shipping", "amount": "15.00", "position": "after_tax"},
            {"kind": "rounding", "label": "Rounding", "amount": "-0.24", "position": "after_tax"},
        ],
        total="USD 656.00",
    )
    assert v.status == "ready", v.reasons


def test_shipping_line_in_table():
    lines = base_annotation()["line_items"] + [
        {
            "kind": "shipping",
            "description": "Freight",
            "quantity": "1",
            "unit_price": "9.00",
            "amount": "9.00",
            "tax_rate": None,
        }
    ]
    v = check(
        line_items=lines,
        subtotal="$600.00",
        tax_total="$51.00",
        total="USD 651.00",
        tax_lines=[
            {"tax_type": "sales_tax", "label": None, "rate": "8.5%", "taxable_base": "600.00", "amount": "51.00"}
        ],
    )
    assert v.status == "ready", v.reasons


def test_rounding_tolerance_one_cent_per_line():
    v = check(total="USD 641.25")  # off by 0.01 with 3 summed lines → accepted
    assert v.status == "ready"
    v = check(total="USD 641.30")
    assert codes(v) == ["total_mismatch"]


def test_zero_decimal_currency_tolerance():
    v = check(
        currency="JPY",
        supplier_country_code="JP",
        ocr_text=OCR_TEXT + " JPY",
        line_items=[
            {
                "kind": "item",
                "description": "文具",
                "quantity": "1",
                "unit_price": "10,000",
                "amount": "10,000",
                "tax_rate": "10%",
            }
        ],
        tax_lines=[
            {"tax_type": "vat", "label": "消費税 10%", "rate": "10%", "taxable_base": "10,000", "amount": "1,000"}
        ],
        subtotal="10,000",
        tax_total="1,000",
        total="11,000",
    )
    assert v.status == "ready", v.reasons


def test_tax_inclusive_prices():
    # Japanese style: line amounts include tax, total = lines, tax shown "of which"
    v = check(
        currency="JPY",
        supplier_country_code="JP",
        ocr_text=OCR_TEXT + " 円",
        line_items=[
            {
                "kind": "item",
                "description": "文具",
                "quantity": "1",
                "unit_price": "11,000",
                "amount": "11,000",
                "tax_rate": "10%",
            }
        ],
        tax_lines=[{"tax_type": "vat", "label": "内消費税", "rate": "10%", "taxable_base": None, "amount": "1,000"}],
        subtotal=None,
        tax_total="1,000",
        total="11,000円",
    )
    assert v.status == "ready", v.reasons
    assert v.tax_mode == "inclusive"


def test_withholding_tax_reduces_total():
    # 1000 + VAT 21% 210 - withholding 15% 150 = 1060
    v = check(
        currency="EUR",
        supplier_country_code="ES",
        ocr_text=OCR_TEXT + " EUR",
        line_items=[
            {
                "kind": "item",
                "description": "Consultoría",
                "quantity": "1",
                "unit_price": "1.000,00",
                "amount": "1.000,00",
                "tax_rate": None,
            }
        ],
        tax_lines=[
            {"tax_type": "vat", "label": "IVA 21%", "rate": "21%", "taxable_base": "1.000,00", "amount": "210,00"},
            {
                "tax_type": "withholding",
                "label": "IRPF -15%",
                "rate": "15%",
                "taxable_base": "1.000,00",
                "amount": "-150,00",
            },
        ],
        subtotal="1.000,00",
        tax_total="210,00",
        total="1.060,00",
    )
    assert v.status == "ready", v.reasons


def test_no_line_items():
    v = check(line_items=[])
    assert "no_line_items" in codes(v)


def test_credit_note_with_discount_balances():
    # Printed positive: lines 100 + shipping refund 10, discount 5 → 105 + VAT 21 = 126 → stored negative
    v = check(
        document_kind="credit_note",
        currency="EUR",
        supplier_country_code="FR",
        ocr_text=OCR_TEXT + " EUR",
        line_items=[
            {
                "kind": "item",
                "description": "Retour",
                "quantity": "1",
                "unit_price": "100,00",
                "amount": "100,00",
                "tax_rate": None,
            }
        ],
        adjustments=[
            {"kind": "shipping", "label": "Port", "amount": "10,00", "position": "before_tax"},
            {"kind": "discount", "label": "Remise", "amount": "5,00", "position": "before_tax"},
        ],
        tax_lines=[
            {"tax_type": "vat", "label": "TVA 20 %", "rate": "20 %", "taxable_base": "105,00", "amount": "21,00"}
        ],
        subtotal="105,00",
        tax_total="21,00",
        total="126,00",
    )
    assert v.status == "ready", v.reasons


def test_withholding_repeated_as_adjustment_is_counted_once():
    # Seen live (Spanish invoice D): IRPF returned as a tax line AND as an 'other' adjustment
    v = check(
        currency="EUR",
        supplier_country_code="ES",
        ocr_text=OCR_TEXT + " EUR",
        line_items=[
            {
                "kind": "item",
                "description": "Diseño",
                "quantity": "1",
                "unit_price": "1.560,00",
                "amount": "1.560,00",
                "tax_rate": None,
            }
        ],
        adjustments=[{"kind": "other", "label": "Retención IRPF 15 %", "amount": "-234,00", "position": "after_tax"}],
        tax_lines=[
            {"tax_type": "vat", "label": "IVA 21 %", "rate": "21 %", "taxable_base": "1.560,00", "amount": "327,60"},
            {
                "tax_type": "withholding",
                "label": "Retención IRPF 15 %",
                "rate": "15 %",
                "taxable_base": None,
                "amount": "-234,00",
            },
        ],
        subtotal="1.560,00",
        tax_total=None,
        total="1.653,60",
    )
    assert v.status == "ready", v.reasons
    assert [i.code for i in v.info] == ["adjustment_repeats_tax"]


def test_shipping_equal_to_a_tax_amount_is_kept():
    # Only 'other' adjustments are deduplicated: real shipping that happens to equal the tax stays
    v = check(
        adjustments=[{"kind": "shipping", "label": "Shipping", "amount": "50.24", "position": "after_tax"}],
        total="USD 691.48",
    )
    assert v.status == "ready" and v.info == [], (v.reasons, v.info)


# --- cut-off: service period in an earlier accounting period -------------------------------
def info_codes(v):
    return [i.code for i in v.info]


def test_service_period_in_the_invoice_month_is_fine():
    v = check(service_period_start="11/01/2026", service_period_end="11/30/2026")
    assert v.status == "ready" and "cutoff_prior_period" not in info_codes(v) and "cutoff" not in v.checks


def test_prior_period_service_below_materiality_is_information_only():
    """Usual case (October services invoiced in November): posted, listed for month-end accruals."""
    v = check(service_period_start="10/01/2026", service_period_end="10/31/2026")
    assert v.status == "ready" and info_codes(v) == ["cutoff_prior_period"]
    assert v.checks["cutoff"]["material"] is False


def test_prior_period_service_at_materiality_goes_to_review():
    v = check(
        service_period_start="10/01/2026",
        service_period_end="10/31/2026",
        policy=Policy(cutoff_materiality=Decimal("641.24")),
    )
    assert v.status == "needs_review" and codes(v) == ["cutoff_prior_period"]


def test_prior_period_service_in_a_foreign_currency_goes_to_review():
    """No home-currency amount before FX conversion exists: a person checks it."""
    v = check(
        service_period_start="01/10/2026",
        service_period_end="31/10/2026",
        invoice_date="18/11/2026",
        due_date="18/12/2026",
        supplier_country_code="FR",
        currency="EUR",
        total="641,24 €",
    )
    assert "cutoff_prior_period" in codes(v)


def test_inverted_service_period_is_flagged():
    v = check(service_period_start="11/30/2026", service_period_end="11/01/2026")
    assert "service_period_inverted" in codes(v)


# --- capitalisation policy: possible fixed asset --------------------------------------------
def test_item_at_the_capex_threshold_goes_to_review():
    v = check(policy=Policy(capex_threshold=Decimal("85.50")))  # the toner's unit price
    assert v.status == "needs_review" and codes(v) == ["possible_capex"]
    assert v.checks["capex"]["lines"] == [2]


def test_capex_uses_the_unit_price_not_the_line_amount():
    """10 boxes at $42.00 = $420.00: each item costs $42, below a $100 threshold."""
    v = check(policy=Policy(capex_threshold=Decimal("100")))
    assert v.status == "ready" and v.checks["capex"]["lines"] == []


def test_capex_not_checked_on_a_foreign_currency_invoice_yet():
    v = check(
        invoice_date="18/11/2026",
        due_date="18/12/2026",
        supplier_country_code="FR",
        currency="EUR",
        total="641,24 €",
        policy=Policy(capex_threshold=Decimal("1")),
    )
    assert "possible_capex" not in codes(v) and v.checks["capex"]["checked"] is False


# --- bank details: a possible change is never posted automatically ---------------------------
def check_bank(accounts=(), notice=False, email=None, known=(), ocr_text=OCR_TEXT, **overrides):
    from app.bank_details import KnownAccount, assess, fingerprint
    from app.models import BankAccount

    key = BANK_KEY
    annotation = base_annotation(bank_accounts=list(accounts), bank_details_change_notice=notice, **overrides)
    result = ProviderResult(annotation=annotation, ocr_text=ocr_text, pages=1, ocr_confidence_avg=0.98)
    inv = build_common_invoice(result, "test")
    earlier = [BankAccount(iban=i) for i in known]
    bank = assess(inv, email, [KnownAccount(fingerprint(a, key) or "", a.label()) for a in earlier], key)
    return validate(inv, confidence_threshold=0.85, bank=bank)


def iban(value):
    return {"iban": value, "bic": None, "account_number": None, "routing_code": None, "account_holder": None}


OLD, NEW = "GB00FAKE00000000000021", "GB00FAKE99999999999917"


def test_same_bank_details_as_before_stay_ready():
    v = check_bank([iban(OLD)], known=[OLD])
    assert v.status == "ready" and v.checks["bank_details"]["passed"] and v.info == []


def test_no_bank_details_stay_ready():
    v = check_bank(known=[OLD])
    assert v.status == "ready" and v.checks["bank_details"] == {
        "passed": True,
        "accounts": 0,
        "notice": [],
        "new": [],
        "known": ["IBAN •••• 0021"],
        "first_seen": False,
    }


def test_changed_bank_details_go_to_review_with_a_masked_reason():
    v = check_bank([iban(NEW)], known=[OLD])
    assert v.status == "needs_review" and codes(v) == ["possible_bank_detail_change"]
    message = v.reasons[0].message
    assert "IBAN •••• 9917" in message and "IBAN •••• 0021" in message and "99999999" not in message
    assert v.reasons[0].field == "bank_accounts"


def test_a_change_announced_by_the_document_goes_to_review():
    v = check_bank(notice=True)
    assert v.status == "needs_review" and codes(v) == ["possible_bank_detail_change"]
    assert "document" in v.reasons[0].message and v.checks["bank_details"]["notice"] == ["document"]


def test_a_change_announced_only_in_the_email_goes_to_review():
    v = check_bank(email="Hola, les enviamos nuestros nuevos datos bancarios.")
    assert v.status == "needs_review" and codes(v) == ["possible_bank_detail_change"]
    assert "email" in v.reasons[0].message


def test_announced_and_changed_make_one_reason():
    v = check_bank([iban(NEW)], notice=True, known=[OLD])
    assert codes(v) == ["possible_bank_detail_change"]


def test_first_bank_details_of_a_known_supplier_are_information_only():
    v = check_bank([iban(NEW)])
    assert v.status == "ready" and codes(v) == []
    assert [i.code for i in v.info] == ["bank_details_first_seen"] and "IBAN •••• 9917" in v.info[0].message
    assert v.checks["bank_details"]["first_seen"] is True


def test_a_bank_change_letter_is_a_fraud_alert_not_a_rejection():
    """Letter F: not an invoice, but it asks to pay a new account (and tries a prompt injection)."""
    v = check_bank([iban(NEW)], notice=True, document_kind="not_invoice", invoice_number=None, total=None)
    assert v.status == "needs_review" and codes(v) == ["possible_bank_detail_change"]
    assert [i.code for i in v.info] == ["not_invoice", "bank_details_first_seen"]


def test_an_unreadable_file_sent_with_a_bank_change_email_is_reviewed():
    v = check_bank(ocr_text="  ..  ", email="Please update our bank details, see attached.")
    assert v.status == "needs_review" and codes(v) == ["possible_bank_detail_change"]
    assert [i.code for i in v.info] == ["unreadable"]


def test_a_plain_letter_without_bank_wording_is_still_rejected():
    v = check_bank(document_kind="not_invoice", invoice_number=None, total=None)
    assert v.status == "rejected" and codes(v) == ["not_invoice"]


def test_bank_details_are_not_checked_without_a_database():
    v = check()
    assert "bank_details" not in v.checks
