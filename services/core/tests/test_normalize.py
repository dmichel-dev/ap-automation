from datetime import date
from decimal import Decimal

import pytest

from app import normalize as n


# --- amounts -------------------------------------------------------------------
@pytest.mark.parametrize(
    "raw, currency, expected",
    [
        ("$1,234.56", "USD", "1234.56"),
        ("1.234,56 €", "EUR", "1234.56"),
        ("1 234,56", "EUR", "1234.56"),
        ("1 234,56 €", "EUR", "1234.56"),  # narrow no-break space (French)
        ("CHF 1'234.50", "CHF", "1234.50"),  # Swiss apostrophe
        ("¥12,000", "JPY", "12000"),
        ("１２，０００円", "JPY", "12000"),  # fullwidth digits
        ("1,23,456.78", "INR", "123456.78"),  # Indian grouping
        ("١٬٢٣٤٫٥٠", "AED", "1234.50"),  # Arabic-Indic digits
        ("$1,200", "USD", "1200"),  # 3 digits after one separator = thousands
        ("1.500", "EUR", "1500"),
        ("1.500", "KWD", "1.500"),  # 3-decimal currency
        ("0,500", "EUR", "0.500"),  # integer part 0 → decimal
        ("12,5", "EUR", "12.5"),
        ("1.234.567", "EUR", "1234567"),
        ("(50.00)", "USD", "-50.00"),
        ("50.00-", "USD", "-50.00"),
        ("−50,00 €", "EUR", "-50.00"),  # unicode minus
        ("USD 641.24", "USD", "641.24"),
        ("42", None, "42"),
    ],
)
def test_parse_amount(raw, currency, expected):
    assert n.parse_amount(raw, currency) == Decimal(expected)


def test_parse_amount_empty_is_none():
    assert n.parse_amount(None) is None
    assert n.parse_amount("  ") is None


@pytest.mark.parametrize("raw", ["n/a", "12.00 + 3.00"])
def test_parse_amount_unreadable(raw):
    with pytest.raises(n.NormalizationError) as exc:
        n.parse_amount(raw)
    assert exc.value.code == "unparseable_amount"


@pytest.mark.parametrize("raw, expected", [("20 %", "20"), ("TVA 5,5 %", "5.5"), ("10％", "10"), ("8.875%", "8.875")])
def test_parse_rate(raw, expected):
    assert n.parse_rate(raw) == Decimal(expected)


def test_parse_rate_out_of_range():
    with pytest.raises(n.NormalizationError):
        n.parse_rate("150 %")


# --- dates ---------------------------------------------------------------------
@pytest.mark.parametrize(
    "raw, expected",
    [
        ("2026-11-18", date(2026, 11, 18)),
        ("2026/11/18", date(2026, 11, 18)),
        ("2026年11月18日", date(2026, 11, 18)),
        ("２０２６年１１月１８日", date(2026, 11, 18)),
        ("令和8年11月18日", date(2026, 11, 18)),
        ("2026년 11월 18일", date(2026, 11, 18)),
        ("18 November 2026", date(2026, 11, 18)),
        ("November 18, 2026", date(2026, 11, 18)),
        ("Nov 18th, 2026", date(2026, 11, 18)),
        ("18-Nov-2026", date(2026, 11, 18)),
        ("le 1er décembre 2026", date(2026, 12, 1)),
        ("18. März 2026", date(2026, 3, 18)),
        ("18 de noviembre de 2026", date(2026, 11, 18)),
        ("18 juillet 2026", date(2026, 7, 18)),
        ("3 juin 2026", date(2026, 6, 3)),
        ("23/11/2026", date(2026, 11, 23)),  # day > 12 proves day first
        ("11/23/2026", date(2026, 11, 23)),  # proves month first
        ("05.05.2026", date(2026, 5, 5)),  # same either way
        ("23/11/26", date(2026, 11, 23)),  # two-digit year
    ],
)
def test_parse_date_unambiguous(raw, expected):
    parsed = n.parse_date(raw)
    assert parsed.value == expected and not parsed.ambiguous


def test_parse_date_ambiguous_keeps_both_readings():
    p = n.parse_date("03/04/2026")
    assert p.value is None and p.ambiguous
    assert p.dmy == date(2026, 4, 3) and p.mdy == date(2026, 3, 4)


def test_parse_date_order_is_recorded():
    assert n.parse_date("23/11/2026").order == "DMY"
    assert n.parse_date("11/23/2026").order == "MDY"
    assert n.parse_date("2026-11-23").order is None


@pytest.mark.parametrize("raw", ["32/13/2026", "soon", "Q4 2026"])
def test_parse_date_unreadable(raw):
    with pytest.raises(n.NormalizationError):
        n.parse_date(raw)


@pytest.mark.parametrize(
    "country, order", [("US", "MDY"), ("PR", "MDY"), ("FR", "DMY"), ("JP", "DMY"), ("CA", None), (None, None)]
)
def test_date_order_suggestion(country, order):
    assert n.date_order_suggestion(country)[0] == order


# --- currency ------------------------------------------------------------------
@pytest.mark.parametrize(
    "candidates, country, code",
    [
        (["EUR"], None, "EUR"),
        (["€"], None, "EUR"),
        (["US$"], None, "USD"),
        (["円"], None, "JPY"),
        (["R$"], "BR", "BRL"),
        ([None, "USD 641.24"], None, "USD"),  # found in the printed total
        (["$", "CAD 100.00"], "US", "CAD"),  # explicit ISO code wins over the symbol
        (["zł"], None, "PLN"),
    ],
)
def test_resolve_currency_unambiguous(candidates, country, code):
    assert n.resolve_currency(candidates, country) == (code, None)


@pytest.mark.parametrize(
    "symbol, country, code",
    [
        ("$", "US", "USD"),
        ("$", "CA", "CAD"),
        ("¥", "CN", "CNY"),
        ("¥", "JP", "JPY"),
        ("kr", "SE", "SEK"),
        ("£", "FR", "GBP"),
    ],
)
def test_resolve_currency_by_country(symbol, country, code):
    got, note = n.resolve_currency([symbol], country)
    assert got == code and note


@pytest.mark.parametrize("symbol", ["$", "¥", "kr"])
def test_resolve_currency_ambiguous_without_country(symbol):
    got, note = n.resolve_currency([symbol], None)
    assert got is None and "ambiguous" in note


def test_resolve_currency_ignores_letters_inside_words():
    assert n.resolve_currency(["FIRM"], None) == (None, None)


@pytest.mark.parametrize(
    "code, text, expected",
    [
        ("EUR", "Total 120,00 €", True),
        ("EUR", "Total 120,00 EUR", True),
        ("USD", "Total $641.24", True),
        ("JPY", "合計 ¥12,000", True),
        ("JPY", "合計 12,000円", True),
        ("ILS", "Total TTC 120,00", False),
        ("EUR", "EUROPEAN supplies 120,00", False),
        ("MYR", "FIRM total 100", False),
        ("MYR", "Jumlah RM 100", True),
    ],
)
def test_currency_evidence(code, text, expected):
    assert n.currency_evidence(code, text) is expected


# --- country, keys, language -------------------------------------------------------
@pytest.mark.parametrize(
    "code, tax_id, expected",
    [
        ("FR", None, "FR"),
        ("fr", None, "FR"),
        ("XX", None, None),
        (None, "FR 12 345678901", "FR"),
        (None, "EL123456789", "GR"),
        (None, "12-3456789", None),
    ],
)
def test_resolve_country(code, tax_id, expected):
    assert n.resolve_country(code, tax_id) == expected


@pytest.mark.parametrize(
    "a, b",
    [
        ("Quillmoor Paper Supply, LLC", "QUILLMOOR PAPER SUPPLY LLC"),
        ("Société Fictive S.A.R.L.", "Societe Fictive SARL"),
        ("Kranich & Söhne GmbH", "Kranich and Söhne"),
        ("株式会社サクラ文具", "サクラ文具"),
    ],
)
def test_supplier_name_normalization_matches_variants(a, b):
    assert n.normalize_supplier_name(a) == n.normalize_supplier_name(b)


def test_supplier_name_keeps_japanese_voiced_marks():
    # NFKD would split が into か + ゛; accent stripping must not touch non-Latin text
    assert n.normalize_supplier_name("ガラス工房") == "ガラス工房"


@pytest.mark.parametrize("raw, expected", [("INV-2026/0042", "INV20260042"), ("n° 42", "N42"), ("ＡＢ－１", "AB1")])
def test_normalize_invoice_number(raw, expected):
    assert n.normalize_invoice_number(raw) == expected


def test_normalize_tax_id():
    assert n.normalize_tax_id("FR 12 345.678.901") == "FR12345678901"


@pytest.mark.parametrize("raw, expected", [("en", "en"), ("FR", "fr"), ("de-ch", "de-CH"), ("français", None)])
def test_normalize_language(raw, expected):
    assert n.normalize_language(raw) == expected
