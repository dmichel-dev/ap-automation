"""Foreign-currency bank matching against a real Postgres: a EUR bill paid from the USD bank account.

Rates arrive as the workflow passes Frankfurter's answers (ECB provider); the payment document is what
the workflow sends to QuickBooks."""

from datetime import date
from decimal import Decimal as D

from app import accounting, db
from app.main import app
from app.matching import Rules
from fastapi.testclient import TestClient

from .conftest import AUTH
from .test_accounting import ITEM, bank_page, claim, make_invoice, plaid_tx, post_ok, posting, register_bank, run, sql
from .test_settlement import act, pay, review

c = TestClient(app)
ECB_EUR = [  # Frankfurter /v2/providers/ecb/rates?base=EUR&quotes=USD&from=2026-11-18&to=2026-11-24
    {"date": "2026-11-18", "base": "EUR", "quote": "USD", "rate": 1.0968},
    {"date": "2026-11-19", "base": "EUR", "quote": "USD", "rate": 1.0992},
    {"date": "2026-11-20", "base": "EUR", "quote": "USD", "rate": 1.099},
    {"date": "2026-11-23", "base": "EUR", "quote": "USD", "rate": 1.0939},
]
NOT_QUOTED = {"status": 422, "message": "ECB doesn't quote AED; try base=EUR"}


def answer(base, status_code, body):
    r = c.post(
        "/fx/rates",
        headers=AUTH,
        json={"answers": [{"base": base, "quote": "USD", "status_code": status_code, "body": body}]},
    )
    assert r.status_code == 200, r.text
    return r.json()


def eur_bill(cfg, currency="EUR", number="AV-2026-0147"):
    inv = make_invoice(
        cfg,
        number=number,
        supplier="Ateliers Verlibreux SARL",
        key_name="ateliers verlibreux",
        currency=currency,
        lines=("1332.18",),
        taxes=("266.43",),
        total="1598.61",
        day=date(2026, 11, 20),
        due=date(2026, 12, 20),
    )
    claim(cfg)
    post_ok(cfg, inv, ext=f"b-{inv[:6]}", exchange_rate="1.099904", exchange_rate_date="2026-11-20")
    return inv


def eur_payment(amount, tid="t-eur"):
    return plaid_tx(tid, amount, day="2026-11-24", name="ATELIERS VERLIBREUX SEPA")


def test_needs_lists_one_series_per_foreign_currency(cfg):
    register_bank(cfg)
    eur_bill(cfg)
    with db.connect(cfg.database_url) as conn:
        needs = accounting.fx_needs(conn, ITEM, Rules(), date(2026, 11, 24))
    assert needs == {
        "provider": "ecb",
        "series": [{"base": "EUR", "quote": "USD", "from": "2026-11-07", "to": "2026-11-24"}],  # 20th − 3 − 10
    }


def test_eur_bill_is_paid_in_full_at_the_bank_rate(cfg):
    register_bank(cfg)
    inv = eur_bill(cfg)
    assert answer("EUR", 200, ECB_EUR) == {"stored": ["EUR"], "not_quoted": [], "failed": []}
    bank_page(cfg, eur_payment(1760.00))  # 1598.61 EUR × 1.0939 = 1748.72 USD expected: +0.65 %
    out = run(cfg)
    assert out["accepted"] == 1 and out["foreign_currency_bills"] == 1
    m = sql(cfg, "SELECT * FROM ap.matches")[0]
    assert (m["bank_currency"], m["fx_rate"], m["fx_rate_date"]) == ("USD", D("1.0939"), date(2026, 11, 23))
    assert (m["expected_bank_amount"], m["difference"]) == (D("1748.72"), D("11.28"))

    work = out["payments_due"][0]
    doc = work["document"]
    assert doc["CurrencyRef"] == {"value": "EUR"} and doc["TotalAmt"] == 1598.61 and doc["ExchangeRate"] == 1.1009565
    assert doc["Line"][0]["Amount"] == 1598.61 and work["booked_bank_amount"] == "1760.00"
    p = pay(work["match_id"])
    assert p["payment_status"] == "paid" and p["payment_reason"] is None and D(p["amount_paid"]) == D("1598.61")
    assert sql(cfg, "SELECT id FROM ap.review_queue WHERE invoice_id = %s", inv) == []  # no difference, no write-off
    assert sql(cfg, "SELECT id FROM ap.write_offs") == []


def test_eur_payment_outside_tolerance_goes_to_review_with_expected_and_actual(cfg):
    register_bank(cfg)
    inv = eur_bill(cfg)
    answer("EUR", 200, ECB_EUR)
    bank_page(cfg, eur_payment(1900.00))
    out = run(cfg)
    assert out["accepted"] == 0 and out["fx_review"] == 1 and out["payments_due"] == []
    assert posting(cfg, inv)["payment_reason"] == "fx_amount_difference"
    item = review(cfg, inv)
    assert item["stage"] == "matching" and item["reasons"][0]["code"] == "fx_amount_difference"
    assert item["reasons"][0]["detail"] == (
        "2026-11-24 bank 1900.00 USD, expected 1748.72 USD (1598.61 EUR × 1.0939, ECB 2026-11-23), +8.65 %"
    )
    # The reviewer confirms it: the whole bill is paid at the bank rate, never a write-off
    assert act(item["id"], "confirm-match", bank_transaction_id="t-eur").status_code == 200
    work = run(cfg)["payments_due"][0]
    assert work["document"]["TotalAmt"] == 1598.61 and work["document"]["ExchangeRate"] == 1.1885325
    assert pay(work["match_id"])["payment_status"] == "paid"
    assert review(cfg, inv)["status"] == "approved" and sql(cfg, "SELECT id FROM ap.write_offs") == []


def test_currency_the_ecb_does_not_quote_goes_to_review(cfg):
    register_bank(cfg)
    inv = eur_bill(cfg, currency="AED", number="AV-AED-1")
    assert answer("AED", 422, NOT_QUOTED)["not_quoted"] == ["AED"]
    run(cfg)
    assert posting(cfg, inv)["payment_reason"] == "fx_rate_unavailable"  # said so even before any payment
    bank_page(cfg, eur_payment(435.30))
    run(cfg)
    item = review(cfg, inv)
    assert item["reasons"][0]["code"] == "fx_rate_unavailable"
    assert item["reasons"][0]["detail"] == "2026-11-24 bank 435.30 USD, no ECB rate for AED"
    m = sql(cfg, "SELECT status, fx_rate, expected_bank_amount, difference FROM ap.matches")[0]
    assert (m["status"], m["fx_rate"], m["expected_bank_amount"], m["difference"]) == ("candidate", None, None, None)


def test_failed_rate_fetch_stores_nothing_and_is_retried(cfg):
    register_bank(cfg)
    inv = eur_bill(cfg)
    assert answer("EUR", 503, {"message": "busy"})["failed"] == ["EUR"]
    assert answer("EUR", 200, [{"date": "2026-11-23", "base": "EUR", "quote": "JPY", "rate": 1}])["failed"] == ["EUR"]
    assert sql(cfg, "SELECT * FROM ap.fx_rates") == [] and sql(cfg, "SELECT * FROM ap.fx_unsupported") == []
    run(cfg)
    assert posting(cfg, inv)["payment_reason"] == "no_match_yet"  # not "unavailable": the next run fetches again
    assert sql(cfg, "SELECT message FROM ap.processing_log WHERE message = 'exchange rates not fetched'")


def jpy_payment_with_a_gap(cfg):
    """2,345,678 JPY paid with 14,801.00 USD: QuickBooks can only book 14,800.99 (7-decimal rate)."""
    register_bank(cfg)
    inv = make_invoice(
        cfg,
        number="KS-9",
        currency="JPY",
        supplier="架空商事",
        key_name="架空商事",
        lines=("2345678",),
        taxes=(),
        total="2345678",
    )
    claim(cfg)
    post_ok(cfg, inv, exchange_rate="0.0063", exchange_rate_date="2026-11-18")
    jpy = [{"date": "2026-11-23", "base": "JPY", "quote": "USD", "rate": 0.0062864}]
    answer("JPY", 200, jpy)
    bank_page(cfg, plaid_tx("t-jpy", 14801.00, day="2026-11-24", name="架空商事"))
    work = run(cfg)["payments_due"][0]
    assert work["booked_bank_amount"] == "14800.99"
    assert pay(work["match_id"])["payment_status"] == "paid"
    return inv


def test_gap_left_by_quickbooks_rate_precision_is_logged(cfg):
    jpy_payment_with_a_gap(cfg)
    log = sql(cfg, "SELECT details FROM ap.processing_log WHERE message = 'bank amount not booked to the cent'")
    assert log[0]["details"]["gap"] == "-0.01"
