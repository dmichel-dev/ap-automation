"""Exchange rates for foreign-currency bank matching, and the rate a bill payment carries.

n8n fetches the rates (Frankfurter API, ECB reference rates only); the core decides which ones it
needs, what each answer means, and which rate applies to a transaction:
  series_needed  the (bill currency → bank currency) series and date range for the open bills
  store_answers  Frankfurter answers: rates stored, "not quoted" remembered, failures retried next run
  lookup         the latest rate published on or before a day (the matching rules check its age)
  payment_rate   the exchange rate of a bill payment: bank amount ÷ bill amount, at QuickBooks' precision
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from . import db, matching
from .db import Conn
from .reference import minor_units

PROVIDER = "ecb"
QBO_RATE_DECIMALS = 7  # QuickBooks keeps 7 decimals of a transaction's ExchangeRate (verified live)
LOOKBACK_DAYS = 10  # before the earliest day a transaction can match: covers weekends and holidays


# =====================================================================================
# The rate a bill payment carries
# =====================================================================================
@dataclass(frozen=True)
class PaymentRate:
    rate: Decimal  # sent as ExchangeRate, already at QuickBooks' precision
    booked: Decimal  # what QuickBooks will credit the bank account: bill amount × rate, rounded
    gap: Decimal  # booked − bank amount; 0 unless the precision can't reach the exact cent


def payment_rate(bill_amount: Decimal, bank_amount: Decimal, bank_currency: str) -> PaymentRate:
    """The nearest rate at QuickBooks' precision gives the booked amount closest to the bank amount.

    Exact for any bill below 100,000 units (one rate step then moves the booked amount by less than a
    cent); above that, the booked amount can be a few cents off and the gap is reported."""
    if bill_amount <= 0 or bank_amount <= 0:
        raise ValueError("amounts must be positive")
    rate = (bank_amount / bill_amount).quantize(Decimal(1).scaleb(-QBO_RATE_DECIMALS), ROUND_HALF_UP)
    cent = Decimal(1).scaleb(-minor_units(bank_currency))
    booked = (bill_amount * rate).quantize(cent, ROUND_HALF_UP)
    return PaymentRate(rate, booked, (booked - bank_amount).quantize(cent))


# =====================================================================================
# Frankfurter answers
# =====================================================================================
def _not_quoted(base: str, status_code: int, body: Any) -> str | None:
    """Frankfurter answers 422 "ECB doesn't quote AED; try base=EUR" for a currency it can't price."""
    message = str(body.get("message", "")) if isinstance(body, dict) else ""
    if status_code == 422 and base in message and "quote" in message:
        return message[:300]
    return None


def _valid_rate(base: str, quote: str, r: Any) -> tuple[date, Decimal] | None:
    if not isinstance(r, dict) or r.get("base") != base or r.get("quote") != quote:
        return None
    try:
        rate, day = Decimal(str(r.get("rate"))), date.fromisoformat(str(r.get("date")))
    except (InvalidOperation, ValueError):
        return None
    return (day, rate) if rate.is_finite() and rate > 0 else None


def _valid_rates(base: str, quote: str, body: Any) -> list[tuple[date, Decimal]] | None:
    """All records valid, or None: a partly malformed answer is not trusted at all."""
    if not isinstance(body, list):
        return None
    out = [_valid_rate(base, quote, r) for r in body]
    return [r for r in out if r is not None] if all(out) else None


def store_answer(conn: Conn, base: str, quote: str, status_code: int, body: Any) -> str:
    """One Frankfurter answer → 'stored', 'not_quoted' or 'failed' (nothing stored; retried next run)."""
    rates = _valid_rates(base, quote, body) if status_code == 200 else None
    if rates is not None:
        for day, rate in rates:
            conn.execute(
                """INSERT INTO fx_rates (provider, base, quote, rate_date, rate) VALUES (%s, %s, %s, %s, %s)
                   ON CONFLICT (provider, base, quote, rate_date) DO UPDATE
                   SET rate = EXCLUDED.rate, fetched_at = now()""",
                (PROVIDER, base, quote, day, rate),
            )
        conn.execute(
            "DELETE FROM fx_unsupported WHERE provider = %s AND base = %s AND quote = %s", (PROVIDER, base, quote)
        )
        return "stored"
    message = _not_quoted(base, status_code, body)
    if message:
        conn.execute(
            """INSERT INTO fx_unsupported (provider, base, quote, message) VALUES (%s, %s, %s, %s)
               ON CONFLICT (provider, base, quote) DO UPDATE SET message = EXCLUDED.message, checked_at = now()""",
            (PROVIDER, base, quote, message),
        )
        return "not_quoted"
    return "failed"


def store_answers(conn: Conn, answers: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, list[str]] = defaultdict(list)
    for a in answers:
        outcome = store_answer(conn, a["base"], a["quote"], a["status_code"], a["body"])
        out[outcome].append(a["base"])
    if out["failed"]:
        db.log(
            conn,
            step="match",
            level="warning",
            message="exchange rates not fetched",
            channel="system",
            details={"currencies": out["failed"], "provider": PROVIDER, "note": "retried on the next run"},
        )
    return {k: out[k] for k in ("stored", "not_quoted", "failed")}


def series_needed(currencies: dict[str, date], quote: str, rules: matching.Rules, today: date) -> list[dict[str, str]]:
    """currencies: bill currency → earliest invoice date among its open bills. One series each, from
    the earliest day such a bill can be paid (with room for weekends and holidays) to today."""
    out = []
    for base, first_day in sorted(currencies.items()):
        start = min(first_day - timedelta(days=rules.days_before_invoice + LOOKBACK_DAYS), today)
        out.append({"base": base, "quote": quote, "from": start.isoformat(), "to": today.isoformat()})
    return out


# =====================================================================================
# Lookup
# =====================================================================================
def lookup(conn: Conn, quote: str, bases: set[str]) -> matching.RateLookup:
    """All stored rates of these currencies, read once; the lookup itself is pure."""
    series: dict[str, list[tuple[date, Decimal]]] = defaultdict(list)
    if bases:
        rows = conn.execute(
            """SELECT base, rate_date, rate FROM fx_rates
                WHERE provider = %s AND quote = %s AND base = ANY(%s) ORDER BY rate_date""",
            (PROVIDER, quote, sorted(bases)),
        ).fetchall()
        for r in rows:
            series[r["base"]].append((r["rate_date"], r["rate"]))

    def latest(base: str, to: str, day: date) -> matching.FxRate | None:
        if to != quote:
            return None
        found = [(d, rate) for d, rate in series.get(base, []) if d <= day]
        return matching.FxRate(found[-1][1], found[-1][0]) if found else None

    return latest


def not_quoted(conn: Conn, quote: str, bases: set[str]) -> set[str]:
    rows = conn.execute(
        "SELECT base FROM fx_unsupported WHERE provider = %s AND quote = %s AND base = ANY(%s)",
        (PROVIDER, quote, sorted(bases)),
    ).fetchall()
    return {r["base"] for r in rows}
