"""Deterministic normalization of values extracted 'as printed'.

Nothing here guesses: when a value cannot be read without assumptions, the function
returns None (or flags ambiguity) and the caller records an Issue.
"""

import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Literal

from . import reference as ref

# Arabic-Indic and Persian digits, Arabic decimal/thousands separators
_DIGITS = str.maketrans(
    "٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹٫٬",
    "01234567890123456789.,",
)
_MINUS = str.maketrans({"−": "-", "‒": "-", "–": "-", "—": "-", "﹣": "-"})


class NormalizationError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def clean_text(value: str | None) -> str | None:
    """NFKC (fullwidth → ASCII, NBSP → space), Eastern digits → ASCII, trimmed. Empty → None."""
    if value is None:
        return None
    s = unicodedata.normalize("NFKC", value).translate(_DIGITS).translate(_MINUS)
    s = re.sub(r"\s+", " ", s).strip()
    return s or None


# ---------------------------------------------------------------------------
# Amounts
# ---------------------------------------------------------------------------
_NUMBER_RUN = re.compile(r"\d(?:[\d.,' ]*\d)?")


def parse_amount(raw: str | None, currency: str | None = None) -> Decimal | None:
    """Parse a printed amount: '1.234,56 €', '1 234,56', '$1,234.56', '¥12,000', '(50.00)', '50.00-'.

    Separator rules:
      * both '.' and ',' present → the last one is the decimal separator
      * one separator used several times → thousands separator
      * one separator used once, followed by exactly 3 digits → thousands separator,
        unless the currency has 3 decimals (KWD, BHD...) or the integer part is 0
      * otherwise → decimal separator
    Wrong interpretations are caught later by the arithmetic checks (lines + tax = total).
    """
    s = clean_text(raw)
    if s is None:
        return None
    if "%" in s:
        raise NormalizationError("unparseable_amount", f"percentage instead of an amount in {raw!r}")
    num = _single_number(s, raw)
    value = _to_decimal(num, _decimal_separator(num, currency), raw)
    return -value if _is_negative(s) else value


def _is_negative(s: str) -> bool:
    """'(50.00)', '-50.00', '€ -50', '50.00-'."""
    return (s.startswith("(") and s.endswith(")")) or bool(re.search(r"-\s*\D*\d", s)) or s.endswith("-")


def _single_number(s: str, raw: str | None) -> str:
    """The one number in the text, without space or apostrophe grouping."""
    runs = [r for r in (r.strip() for r in _NUMBER_RUN.findall(s)) if r]
    if not runs:
        raise NormalizationError("unparseable_amount", f"no number in {raw!r}")
    if len(runs) > 1:
        raise NormalizationError("unparseable_amount", f"several numbers in {raw!r}")
    return str(runs[0]).replace(" ", "").replace("'", "")


def _decimal_separator(num: str, currency: str | None) -> str | None:
    """'.' or ',' when the number has a decimal part, None when every separator groups thousands."""
    if "." in num and "," in num:
        return "." if num.rfind(".") > num.rfind(",") else ","
    if "." not in num and "," not in num:
        return None
    sep = "." if "." in num else ","
    int_part, _, frac = num.rpartition(sep)
    thousands_only = num.count(sep) > 1 or (
        len(frac) == 3 and ref.minor_units(currency) != 3 and int_part not in ("0", "")
    )
    return None if thousands_only else sep


def _to_decimal(num: str, decimal_sep: str | None, raw: str | None) -> Decimal:
    if decimal_sep is None:
        digits = num.replace(".", "").replace(",", "")
    else:
        thousands = "," if decimal_sep == "." else "."
        int_part, _, frac = num.rpartition(decimal_sep)
        digits = int_part.replace(thousands, "") + "." + frac
    try:
        return Decimal(digits)
    except InvalidOperation as exc:
        raise NormalizationError("unparseable_amount", f"cannot read {raw!r}") from exc


def parse_rate(raw: str | None) -> Decimal | None:
    """'20 %', '8.5%', '10％', 'TVA 5,5 %' → percent as Decimal (0-100)."""
    s = clean_text(raw)
    if s is None:
        return None
    m = re.search(r"\d+(?:[.,]\d+)?", s)
    if not m:
        raise NormalizationError("unparseable_rate", f"no rate in {raw!r}")
    value = Decimal(m.group().replace(",", "."))
    if not 0 <= value <= 100:
        raise NormalizationError("unparseable_rate", f"rate out of range in {raw!r}")
    return value


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------
DateOrder = Literal["DMY", "MDY"]


@dataclass(frozen=True)
class ParsedDate:
    value: date | None  # set when unambiguous
    dmy: date | None = None  # candidates when the day/month order is unknown
    mdy: date | None = None
    order: DateOrder | None = None  # when the numbers prove the order

    def reading(self, order: DateOrder) -> date | None:
        """The date itself, or for an ambiguous one, its reading in the given order."""
        if not self.ambiguous:
            return self.value
        return self.dmy if order == "DMY" else self.mdy

    @property
    def ambiguous(self) -> bool:
        return self.value is None and (self.dmy is not None or self.mdy is not None)


def _safe_date(y: int, m: int, d: int) -> date | None:
    try:
        return date(y, m, d)
    except ValueError:
        return None


def _year(token: str) -> int:
    y = int(token)
    return 2000 + y if len(token) == 2 else y


def _strip_accents_latin(s: str) -> str:
    out: list[str] = []
    for ch in unicodedata.normalize("NFKD", s):
        if unicodedata.combining(ch) and out and out[-1].isascii():
            continue
        out.append(ch)
    return unicodedata.normalize("NFC", "".join(out))


_CJK_DATE = re.compile(r"(\d{4})\s*[年년]\s*(\d{1,2})\s*[月월]\s*(\d{1,2})\s*[日일]?")
_REIWA_DATE = re.compile(r"令和\s*(\d{1,2}|元)\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日")
_ISO_DATE = re.compile(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?:[T ].*)?$")
_NUMERIC_DATE = re.compile(r"^(\d{1,2})\s*[-/.]\s*(\d{1,2})\s*[-/.]\s*(\d{2}|\d{4})$")


def parse_date(raw: str | None) -> ParsedDate | None:
    """Parse a printed date. Returns None when empty; raises NormalizationError when unreadable.

    Unambiguous forms: year first (2026-11-18, 2026/11/18), CJK (2026年11月18日, 令和8年...),
    month names in EN/FR/DE/ES/IT/PT/NL ('18 novembre 2026', 'Nov 18, 2026'),
    numeric when one part is > 12 or both parts are equal.
    Otherwise both readings are returned and the date is ambiguous.
    """
    s = clean_text(raw)
    if s is None:
        return None
    if d := _year_first_date(s):
        return ParsedDate(d)
    if m := _NUMERIC_DATE.match(s):
        return _numeric_date(m, raw)
    if d := _month_name_date(s):
        return ParsedDate(d)
    raise NormalizationError("unparseable_date", f"cannot read date {raw!r}")


def _year_first_date(s: str) -> date | None:
    """Japanese era (令和), CJK (2026年11月18日) or ISO-like (2026-11-18) dates."""
    if (m := _REIWA_DATE.search(s)) and (
        d := _safe_date(2018 + (1 if m.group(1) == "元" else int(m.group(1))), int(m.group(2)), int(m.group(3)))
    ):
        return d
    for find in (_CJK_DATE.search, _ISO_DATE.match):  # year, month, day groups
        if (m := find(s)) and (d := _safe_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))):
            return d
    return None


def _numeric_date(m: re.Match[str], raw: str | None) -> ParsedDate:
    """dd/mm/yyyy or mm/dd/yyyy: decided only when one reading is impossible or both agree."""
    a, b, y = int(m.group(1)), int(m.group(2)), _year(m.group(3))
    dmy, mdy = _safe_date(y, b, a), _safe_date(y, a, b)
    if dmy and not mdy:
        return ParsedDate(dmy, order="DMY")
    if mdy and not dmy:
        return ParsedDate(mdy, order="MDY")
    if dmy and mdy:
        return ParsedDate(dmy) if dmy == mdy else ParsedDate(None, dmy=dmy, mdy=mdy)
    raise NormalizationError("unparseable_date", f"invalid date {raw!r}")


def _month_name_date(s: str) -> date | None:
    """One month word, one 4-digit year, one day number."""
    text = _strip_accents_latin(s).lower()
    months = {ref.MONTH_LOOKUP[w] for w in re.findall(r"[a-z]+", text) if w in ref.MONTH_LOOKUP}
    numbers = re.findall(r"\d+", text)
    years = [n for n in numbers if len(n) == 4]
    days = [n for n in numbers if len(n) <= 2]
    if len(months) == 1 and len(years) == 1 and len(days) == 1:
        return _safe_date(int(years[0]), months.pop(), int(days[0]))
    return None


def date_order_suggestion(country: str | None) -> tuple[DateOrder | None, str]:
    """Suggested day/month order for ambiguous numeric dates, from the supplier country."""
    if not country:
        return None, "supplier country unknown"
    if country in ref.MONTH_FIRST_COUNTRIES:
        return "MDY", f"supplier country {country} writes month first"
    if country in ref.NO_DATE_ORDER_SUGGESTION:
        return None, f"supplier country {country} uses both conventions"
    return "DMY", f"supplier country {country} writes day first"


# ---------------------------------------------------------------------------
# Currency and country
# ---------------------------------------------------------------------------
def resolve_currency(candidates: list[str | None], country: str | None) -> tuple[str | None, str | None]:
    """Find the ISO 4217 code in the printed currency (then in the printed total).

    Returns (code, note). code is None when nothing or only an ambiguous symbol was found.
    An explicit ISO code always wins over a symbol.
    """
    cleaned = (clean_text(c) for c in candidates if c)
    texts = [t.upper() for t in cleaned if t]
    for t in texts:
        for token in re.findall(r"(?<![A-Z])[A-Z]{3}(?![A-Z])", t):
            if token in ref.ISO_4217:
                return token, None
    for t in texts:
        for symbol in sorted(ref.CURRENCY_SYMBOLS, key=len, reverse=True):
            if re.search(_symbol_pattern(symbol), t):
                return ref.CURRENCY_SYMBOLS[symbol], None
        tokens = re.findall(r"[^\d\s.,'()+-]+", t)
        for token in tokens:
            key = token.rstrip(".")
            if key in ref.AMBIGUOUS_SYMBOLS:
                by_country = ref.AMBIGUOUS_SYMBOLS[key]
                code = by_country.get(country or "") or ref.AMBIGUOUS_SYMBOL_DEFAULT.get(key)
                if code:
                    return code, f"symbol {token!r} read as {code} (supplier country {country or 'unknown'})"
                return None, f"symbol {token!r} is ambiguous without a known supplier country"
    return None, None


def currency_evidence(code: str, ocr_text: str) -> bool:
    """True if the OCR text contains the ISO code or a symbol that can mean this currency.
    Guards against a provider returning a currency that is not printed on the document."""
    text = (clean_text(ocr_text) or "").upper()
    if re.search(rf"(?<![A-Z]){code}(?![A-Z])", text):
        return True
    symbols = [s for s, c in ref.CURRENCY_SYMBOLS.items() if c == code]
    symbols += [s for s, by_country in ref.AMBIGUOUS_SYMBOLS.items() if code in by_country.values()]
    symbols += [s for s, c in ref.AMBIGUOUS_SYMBOL_DEFAULT.items() if c == code]
    return any(re.search(_symbol_pattern(s), text) for s in symbols)


def _symbol_pattern(symbol: str) -> str:
    """Letter symbols (RM, ZŁ, LEI) must be whole tokens, so 'FIRM' never matches 'RM'."""
    escaped = re.escape(symbol)
    return rf"(?<![A-Z]){escaped}(?![A-Z])" if re.search(r"[A-Z]", symbol) else escaped


def resolve_country(code: str | None, tax_id: str | None) -> str | None:
    """Validate the ISO 3166 code; otherwise derive it from an EU-style VAT prefix."""
    c = (clean_text(code) or "").upper()
    if c in ref.ISO_3166:
        return c
    t = normalize_tax_id(tax_id) or ""
    # EU-style VAT number: two letters then digits, e.g. FR12345678901, DE123456789, EL123456789
    if re.fullmatch(r"[A-Z]{2}\d[0-9A-Z]{3,}", t):
        prefix = ref.VAT_PREFIX_TO_COUNTRY.get(t[:2], t[:2])
        if prefix in ref.ISO_3166:
            return prefix
    return None


# ---------------------------------------------------------------------------
# Duplicate-check keys
# ---------------------------------------------------------------------------
def _alnum(s: str) -> str:
    return "".join(ch for ch in s if ch.isalnum())


def _alnum_key(raw: str | None) -> str | None:
    """Letters and digits only, upper case: 'FR 12-345' and 'fr12345' give the same key."""
    s = clean_text(raw)
    return (_alnum(s).upper() or None) if s else None


def normalize_invoice_number(raw: str | None) -> str | None:
    return _alnum_key(raw)


def normalize_tax_id(raw: str | None) -> str | None:
    return _alnum_key(raw)


def normalize_supplier_name(raw: str | None) -> str | None:
    s = clean_text(raw)
    if not s:
        return None
    for form in ref.LEGAL_FORM_SUBSTRINGS:
        s = s.replace(unicodedata.normalize("NFKC", form), " ")
    s = _strip_accents_latin(s).casefold()
    s = re.sub(r"[&+]", " and ", s)
    # Join dotted abbreviations (s.a.r.l. → sarl, s.p.a. → spa) before splitting
    s = re.sub(r"\b(?:\w\.){2,}", lambda m: m.group().replace(".", ""), s)
    tokens = re.findall(r"\w+", s)
    kept = [t for t in tokens if t not in ref.LEGAL_FORM_TOKENS]
    return " ".join(kept or tokens) or None


def normalize_language(raw: str | None) -> str | None:
    s = clean_text(raw)
    if s and re.fullmatch(r"[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*", s):
        primary, *rest = s.split("-")
        return "-".join([primary.lower(), *[p.upper() if len(p) == 2 else p for p in rest]])
    return None
