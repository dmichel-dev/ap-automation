"""Accounting document content (pure functions)."""

from decimal import Decimal as D

import pytest
from app import postings as p
from app.postings import PostingError, doc_number, document_lines, qbo_quote, vendor_display_name


def line(no, amount, kind="item", description=None):
    return {"line_no": no, "kind": kind, "description": description, "amount": D(amount) if amount else None}


def tax(amount, tax_type="vat", label=None):
    return {"tax_type": tax_type, "label": label, "amount": D(amount)}


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
def total_of(lines):
    return sum(ln.amount for ln in lines)


def test_tax_exclusive_invoice_lines_sum_to_total():
    out = document_lines(
        lines=[line(1, "420.00"), line(2, "171.00")],
        taxes=[tax("50.24", "sales_tax", "Sales tax")],
        tax_mode="exclusive",
        total=D("641.24"),
        currency="USD",
        is_credit_note=False,
    )
    assert total_of(out) == D("641.24") and [ln.account for ln in out] == ["expense", "expense", "tax"]


def test_discount_and_withholding_are_negative_lines():
    out = document_lines(
        lines=[line(1, "1560.00"), line(2, "-78.00", "discount")],
        taxes=[tax("311.22", label="IVA 21 %"), tax("-222.30", "withholding", "IRPF 15 %")],
        tax_mode="exclusive",
        total=D("1570.92"),
        currency="EUR",
        is_credit_note=False,
    )
    assert [ln.amount for ln in out] == [D("1560.00"), D("-78.00"), D("311.22"), D("-222.30")]
    assert total_of(out) == D("1570.92")


def test_tax_inclusive_invoice_does_not_add_tax_again():
    out = document_lines(
        lines=[line(1, "11000")],
        taxes=[tax("1000", label="内消費税")],
        tax_mode="inclusive",
        total=D("11000"),
        currency="JPY",
        is_credit_note=False,
    )
    assert [(ln.amount, ln.account) for ln in out] == [(D("11000"), "expense")]


def test_credit_note_lines_are_positive():
    out = document_lines(
        lines=[line(1, "-69.00"), line(2, "-58.00")],
        taxes=[tax("-25.40")],
        tax_mode="exclusive",
        total=D("-152.40"),
        currency="GBP",
        is_credit_note=True,
    )
    assert total_of(out) == D("152.40") and all(ln.amount > 0 for ln in out)


def test_rounding_residual_within_tolerance_becomes_a_line():
    out = document_lines(
        lines=[line(1, "10.00"), line(2, "10.00")],
        taxes=[tax("4.00")],
        tax_mode="exclusive",
        total=D("24.01"),
        currency="EUR",
        is_credit_note=False,
    )
    assert out[-1].description == "Rounding difference" and out[-1].amount == D("0.01")
    assert total_of(out) == D("24.01")


def test_large_residual_is_refused():
    with pytest.raises(PostingError) as e:
        document_lines(
            lines=[line(1, "10.00")],
            taxes=[],
            tax_mode="exclusive",
            total=D("12.00"),
            currency="EUR",
            is_credit_note=False,
        )
    assert e.value.code == "lines_do_not_sum_to_total"


def test_missing_amount_is_refused():
    with pytest.raises(PostingError):
        document_lines(
            lines=[line(1, None)], taxes=[], tax_mode="exclusive", total=D("10"), currency="EUR", is_credit_note=False
        )


def test_zero_decimal_currency_amounts_are_integers():
    out = document_lines(
        lines=[line(1, "6000"), line(2, "7600"), line(3, "2940")],
        taxes=[tax("1360"), tax("235")],
        tax_mode="exclusive",
        total=D("18135"),
        currency="JPY",
        is_credit_note=False,
    )
    assert all(ln.amount == ln.amount.to_integral_value() for ln in out) and total_of(out) == D("18135")


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
