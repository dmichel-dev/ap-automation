"""How values read wherever a person sees them: pages, emails and reason messages. Display only:
stored values, calculations and what is sent to QuickBooks or Plaid keep their full precision and
ISO formats.

- Money: the currency's usual decimals (2 for most, 0 for JPY, 3 for KWD…), thousands separated,
  the ISO code after the amount when it is known: 1,598.61 EUR, 2,345,678 JPY.
- Confidence and thresholds: whole percentages, rounded down, so a score just under the threshold
  never reads as the threshold itself (0.8499 → 84%, below 85%).
- Dates: day, month name, year (26 Nov 2026), so no date can be read the wrong way round; with a
  time, 26 Nov 2026, 14:05 (UTC).
"""

from datetime import UTC, date, datetime
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from .reference import minor_units

NONE = "–"
# A stated amount compared with the amount computed from its parts: (what is stated, what it is compared with)
MISMATCHES = {
    "lines": ("Subtotal", "the sum of the lines"),
    "tax": ("Tax total", "the sum of the tax lines"),
    "total": ("Total", "lines + adjustments + tax"),
    "bill": ("Sum of the bill lines", "the total"),
}


def _decimal(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def amount(value: Any, currency: str | None = None) -> str:
    """1598.6100 → "1,598.61"; with JPY, 2345678.00 → "2,345,678". Unreadable values are shown as they are."""
    if value is None:
        return NONE
    number = _decimal(value)
    if number is None or not number.is_finite():
        return str(value)
    places = minor_units(currency)
    return f"{number.quantize(Decimal(1).scaleb(-places), ROUND_HALF_UP):,.{places}f}"


def money(value: Any, currency: str | None = None) -> str:
    """An amount with its currency code when known: "1,598.61 EUR"."""
    shown = amount(value, currency)
    return f"{shown} {currency}" if currency and value is not None else shown


def percent(score: Any) -> str:
    """0.9775 → "97%" (whole percent, rounded down)."""
    number = _decimal(score) if score is not None else None
    if number is None:
        return NONE
    return f"{(number * 100).to_integral_value(ROUND_DOWN)}%"


def _parse(value: Any) -> date | datetime | None:
    if isinstance(value, (date, datetime)):
        return value
    try:
        text = str(value)
        return datetime.fromisoformat(text) if len(text) > 10 else date.fromisoformat(text)
    except ValueError:
        return None


def day(value: Any) -> str:
    """2026-11-26 (a date, a datetime or its ISO text) → "26 Nov 2026"."""
    if value is None or value == "":
        return NONE
    parsed = _parse(value)
    if parsed is None:
        return str(value)
    return f"{parsed.day} {parsed:%b %Y}"


def moment(value: Any) -> str:
    """2026-11-26T14:05:31+00:00 → "26 Nov 2026, 14:05" (UTC). A date alone is shown as a day."""
    if value is None or value == "":
        return NONE
    parsed = _parse(value)
    if not isinstance(parsed, datetime):
        return day(value)
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(UTC)
    return f"{day(parsed)}, {parsed:%H:%M}"


def mismatch(kind: str, stated: Decimal, computed: Decimal, currency: str | None) -> str:
    """ "Total (875.13 EUR) doesn't match lines + adjustments + tax (865.13 EUR): 10.00 EUR off." """
    what, against = MISMATCHES[kind]
    gap = abs(stated - computed)
    return (
        f"{what} ({money(stated, currency)}) doesn't match {against} ({money(computed, currency)}): "
        f"{money(gap, currency)} off."
    )


def below_threshold(score: Any, threshold: Any) -> str:
    return f"Confidence ({percent(score)}) is below the review threshold ({percent(threshold)})."
