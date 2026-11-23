"""Accounting document content (pure functions)."""

from decimal import Decimal as D

import pytest
from app import postings as p
from app.postings import PostingError, doc_number, document_lines, qbo_quote, vendor_display_name
from app.tax_treatment import TaxRule, TaxTreatment


def line(no, amount, kind="item", description=None, rate=None):
    return {
        "line_no": no,
        "kind": kind,
        "description": description,
        "amount": D(amount) if amount else None,
        "tax_rate": D(rate) if rate else None,
    }


def tax(amount, tax_type="vat", label=None, rate=None):
    return {"tax_type": tax_type, "label": label, "amount": D(amount), "rate": D(rate) if rate else None}


# --- document number, vendor name, query escaping -----------------------------------
def test_doc_number_kept_when_it_fits():
    assert doc_number("AV-2026-0147") == "AV-2026-0147"


def test_long_doc_number_is_shortened_deterministically():
    long = "INV-2026-ABCDEFGHIJKLMNOP-0001"
    short = doc_number(long)
    assert len(short) == 21 and short == doc_number(long) and short.startswith("INV-2026-ABCDEFG")
    assert doc_number(long[:-1] + "2") != short


def test_vendor_display_name_suffix_only_for_foreign_currency():
    assert vendor_display_name("Quillmoor Paper Supply LLC", "USD", "USD") == "Quillmoor Paper Supply LLC"
    assert vendor_display_name("Ateliers Verlibreux SARL", "EUR", "USD") == "Ateliers Verlibreux SARL (EUR)"


def test_vendor_display_name_removes_forbidden_characters_and_fits():
    name = vendor_display_name("Acme: Tools\tand\nMore " + "x" * 200, "EUR", "USD")
    assert ":" not in name and "\t" not in name and "\n" not in name
    assert len(name) <= 100 and name.endswith(" (EUR)")


def test_qbo_quote():
    assert qbo_quote("O'Brien & Söhne") == "O\\'Brien & Söhne"
    assert qbo_quote("a\\b") == "a\\\\b"


# --- lines --------------------------------------------------------------------------
POLICY = TaxTreatment()  # default: every tax non-recoverable, withholding always separate
RECOVERABLE = TaxTreatment(default="recoverable")


def build(lines, taxes, total, currency="USD", tax_mode="exclusive", credit=False, policy=POLICY, country="US"):
    return document_lines(
        lines=lines,
        taxes=taxes,
        tax_mode=tax_mode,
        total=D(total),
        currency=currency,
        is_credit_note=credit,
        treatment=lambda tax_type: policy.treatment(tax_type, country),
    )


def total_of(lines):
    return sum(ln.amount for ln in lines)


def amounts(lines):
    return [(ln.amount, ln.account) for ln in lines]


def test_non_recoverable_tax_is_part_of_each_expense_line():
    """US sales tax: no separate tax line; spread over the lines in proportion to their amounts."""
    out = build([line(1, "420.00", description="Copy paper"), line(2, "171.00")], [tax("50.24", "sales_tax")], "641.24")
    assert amounts(out) == [(D("455.70"), "expense"), (D("185.54"), "expense")]
    assert out[0].description == "Copy paper (incl. tax 35.70)"


def test_tax_follows_the_lines_with_the_same_rate():
    """Japanese invoice, 10 % and reduced 8 %: each tax goes to the lines it was computed on."""
    out = build(
        [line(1, "6000", rate="10"), line(2, "7600", rate="10"), line(3, "2940", rate="8")],
        [tax("1360", rate="10"), tax("235", rate="8")],
        "18135",
        currency="JPY",
        country="JP",
    )
    assert [ln.amount for ln in out] == [D("6600"), D("8360"), D("3175")] and total_of(out) == D("18135")


def test_zero_decimal_currency_allocation_stays_in_whole_units():
    out = build([line(1, "6000"), line(2, "7600"), line(3, "2940")], [tax("1360"), tax("235")], "18135", "JPY")
    assert all(ln.amount == ln.amount.to_integral_value() for ln in out) and total_of(out) == D("18135")


def test_allocation_remainder_goes_to_the_largest_line():
    out = build([line(1, "10.00"), line(2, "10.00"), line(3, "10.00")], [tax("1.00")], "31.00")
    assert [ln.amount for ln in out] == [D("10.34"), D("10.33"), D("10.33")]


def test_withholding_is_always_a_separate_liability_line():
    """Spanish invoice: IVA non-recoverable (in the cost), IRPF withheld (owed to the tax office)."""
    out = build(
        [line(1, "1560.00"), line(2, "-78.00", "discount")],
        [tax("311.22", label="IVA 21 %"), tax("-222.30", "withholding", "IRPF 15 %")],
        "1570.92",
        currency="EUR",
        country="ES",
    )
    assert amounts(out) == [(D("1871.22"), "expense"), (D("-78.00"), "expense"), (D("-222.30"), "withholding")]


def test_recoverable_tax_is_a_separate_receivable_line():
    out = build([line(1, "420.00"), line(2, "171.00")], [tax("50.24")], "641.24", policy=RECOVERABLE)
    assert amounts(out) == [(D("420.00"), "expense"), (D("171.00"), "expense"), (D("50.24"), "tax_recoverable")]


def test_tax_inclusive_invoice_does_not_add_tax_again():
    out = build([line(1, "11000")], [tax("1000", label="内消費税")], "11000", "JPY", "inclusive", country="JP")
    assert amounts(out) == [(D("11000"), "expense")]


def test_recoverable_tax_is_taken_out_of_an_inclusive_invoice():
    out = build([line(1, "11000")], [tax("1000")], "11000", "JPY", "inclusive", policy=RECOVERABLE, country="JP")
    assert amounts(out) == [(D("10000"), "expense"), (D("1000"), "tax_recoverable")]


def test_credit_note_lines_are_positive():
    out = build([line(1, "-69.00"), line(2, "-58.00")], [tax("-25.40")], "-152.40", "GBP", credit=True, country="GB")
    assert [ln.amount for ln in out] == [D("82.80"), D("69.60")]


def test_rounding_residual_within_tolerance_becomes_a_line():
    out = build([line(1, "10.00"), line(2, "10.00")], [tax("4.00")], "24.01", "EUR")
    assert out[-1].description == "Rounding difference" and out[-1].amount == D("0.01")
    assert total_of(out) == D("24.01")


def test_large_residual_is_refused():
    with pytest.raises(PostingError) as e:
        build([line(1, "10.00")], [], "12.00", "EUR")
    assert e.value.code == "lines_do_not_sum_to_total"


def test_missing_amount_is_refused():
    with pytest.raises(PostingError):
        build([line(1, None)], [], "10", "EUR")


# --- tax treatment policy ----------------------------------------------------------------
def test_most_specific_rule_wins_and_withholding_is_fixed():
    policy = TaxTreatment(
        default="non_recoverable",
        rules=(
            TaxRule(tax_type="vat", treatment="non_recoverable"),
            TaxRule(tax_type="vat", country="FR", treatment="recoverable"),
            TaxRule(country="DE", treatment="recoverable"),
        ),
    )
    assert policy.treatment("vat", "FR") == "recoverable"
    assert policy.treatment("vat", "ES") == "non_recoverable"
    assert policy.treatment("vat", "DE") == "non_recoverable"  # tax type + country beats country alone
    assert policy.treatment("gst", "DE") == "recoverable"
    assert policy.treatment("withholding", "FR") == "withholding"


def test_committed_policy_is_valid_and_non_recoverable_for_the_us_company():
    policy = TaxTreatment.load()
    assert {policy.treatment(t, c) for t in ("vat", "gst", "sales_tax", "other") for c in ("US", "FR", "JP")} == {
        "non_recoverable"
    }


def test_unknown_key_in_the_policy_file_is_refused():
    with pytest.raises(ValueError):
        TaxTreatment.model_validate({"default": "non_recoverable", "rules": [{"treatment": "maybe"}]})


# --- decisions on QuickBooks answers ----------------------------------------------------
def vendors(*found):
    return {"QueryResponse": {"Vendor": list(found)} if found else {}}


def test_vendor_decision():
    v = {"Id": "58", "CurrencyRef": {"value": "EUR"}, "Active": True}
    assert p.vendor_decision(200, vendors(), "EUR").decision == "create"
    assert p.vendor_decision(200, vendors(v), "EUR") == p.Decision("use", external_id="58")
    conflict = p.vendor_decision(200, vendors(v), "USD")
    assert (conflict.decision, conflict.error_code) == ("review", "vendor_currency_conflict")
    inactive = p.vendor_decision(200, vendors({**v, "Active": False}), "EUR")
    assert (inactive.decision, inactive.error_code) == ("review", "vendor_inactive")


def test_vendor_decision_on_query_error():
    fault = {"Fault": {"Error": [{"code": "4001", "Message": "Invalid query"}]}}
    d = p.vendor_decision(400, fault, "EUR")
    assert (d.decision, d.http_status, d.fault_code, d.error_code) == ("error", 400, "4001", None)
    assert p.vendor_decision(0, None, "EUR").decision == "error"  # network error: no response


def test_existing_document_decision():
    doc = lambda i, vendor: {"Id": i, "VendorRef": {"value": vendor}}  # noqa: E731
    answer = lambda *docs: {"QueryResponse": {"Bill": list(docs)}}  # noqa: E731
    assert p.existing_document_decision(200, answer(), "Bill", "7").decision == "create"
    assert p.existing_document_decision(200, answer(doc("150", "9")), "Bill", "7").decision == "create"  # other vendor
    assert p.existing_document_decision(200, answer(doc("150", "7")), "Bill", "7") == p.Decision("exists", "150")
    dup = p.existing_document_decision(200, answer(doc("150", "7"), doc("151", "7")), "Bill", "7")
    assert (dup.decision, dup.error_code) == ("review", "duplicate_in_accounting")


@pytest.mark.parametrize(
    ("error_code", "status", "fault", "expected"),
    [
        (None, 0, None, ("http_0", True)),  # network error
        (None, 401, None, ("http_401", True)),  # token refresh
        (None, 425, None, ("http_425", True)),  # request already sent: the next run finds it
        (None, 429, None, ("http_429", True)),
        (None, 503, None, ("http_503", True)),
        (None, 400, "6240", ("qbo_6240", False)),  # validation error: same data fails again
        ("exchange_rate_unavailable", None, None, ("exchange_rate_unavailable", True)),
        ("attachment_failed", 400, "2010", ("attachment_failed", True)),
        ("vendor_currency_conflict", 503, None, ("vendor_currency_conflict", False)),  # a person decides
        ("something_new", None, None, ("something_new", False)),
    ],
)
def test_classify_failure(error_code, status, fault, expected):
    assert p.classify_failure(error_code, status, fault, {"vendor_currency_conflict"}) == expected
