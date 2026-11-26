"""The daily summary (daily_summary.py): each figure on seeded data, the 24-hour edge, the touchless
rate's definition, open items oldest first, masked bank details, and a day with nothing to report.
Real Postgres; invoices, postings, payments and write-offs are made through the same helpers and
endpoints as in the accounting tests."""

import json

import pytest
from app import daily_summary, db
from app.main import app
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from .conftest import AUTH, WRITE_OFF_ACCOUNT, query, set_scope
from .test_accounting import bank_page, make_invoice, plaid_tx, posted_bill, register_bank, run, sql
from .test_settlement import pay, review, write_off_flow

c = TestClient(app)
FULL_NUMBER = "DE89370400440532013000"  # printed on the document; must never reach the summary
MASKED = "IBAN •••• 3000"


def figures(cfg):
    with db.connect(cfg.database_url) as conn:
        return daily_summary.figures(conn)


def email(cfg):
    with db.connect(cfg.database_url) as conn:
        return daily_summary.daily(conn, ["ap@example.test"])


def set_cols(cfg, invoice_id, **cols):
    sets = ", ".join(f"{k} = %s" for k in cols)
    values = [Jsonb(v) if isinstance(v, (list, dict)) else v for v in cols.values()]
    sql(cfg, f"UPDATE ap.invoices SET {sets} WHERE id = %s", *values, invoice_id)


def received(cfg, ago, **kw):
    """An invoice received `ago` before now (a Postgres interval)."""
    inv = make_invoice(cfg, number=kw.pop("number", "QM-1"), **kw)
    sql(cfg, "UPDATE ap.invoices SET received_at = now() - %s::interval WHERE id = %s", ago, inv)
    return inv


def open_review(cfg, invoice_id, code="total_mismatch", stage="validation", ago="1 hour"):
    sql(
        cfg,
        """INSERT INTO ap.review_queue (invoice_id, reasons, stage, created_at)
           VALUES (%s, %s, %s, now() - %s::interval)""",
        invoice_id,
        Jsonb([{"code": code, "field": None, "message": "test"}]),
        stage,
        ago,
    )


def rejected(cfg, code, number):
    inv = received(cfg, "1 hour", number=number, status="rejected", account=None)
    set_cols(cfg, inv, status_reasons=[{"code": code, "field": None, "message": "test"}])
    return inv


# =====================================================================================
# Invoices and the touchless rate
# =====================================================================================
@pytest.fixture
def mixed_day(acfg):
    """Received today: two posted with no review, one posted after a validation review, one possible
    duplicate waiting for review, one ready but not posted yet, one unreadable, one not an invoice,
    one exact duplicate file. Plus one posted yesterday and a test-data invoice."""
    for n in ("T-1", "T-2"):
        posted_bill(acfg, number=n)
    reviewed = posted_bill(acfg, number="R-1")
    open_review(acfg, reviewed)
    sql(acfg, "UPDATE ap.review_queue SET status = 'approved', reviewer = 'x', resolved_at = now()")
    dup = received(acfg, "2 hours", number="D-1", status="needs_review", account=None)
    open_review(acfg, dup, code="possible_duplicate")
    make_invoice(acfg, number="P-1")  # ready, not posted yet
    rejected(acfg, "unreadable", "U-1")
    rejected(acfg, "not_invoice", "N-1")
    rejected(acfg, "duplicate_file", "F-1")
    old = posted_bill(acfg, number="OLD-1")
    sql(acfg, "UPDATE ap.invoices SET received_at = now() - interval '24 hours 1 minute' WHERE id = %s", old)
    sql(acfg, "UPDATE ap.accounting_postings SET posted_at = now() - interval '2 days' WHERE invoice_id = %s", old)
    test_data = posted_bill(acfg, number="E2E-1")
    sql(acfg, "UPDATE ap.invoices SET scope = 'e2e' WHERE id = %s", test_data)
    return acfg


def test_received_invoices_by_what_intake_decided(mixed_day):
    inv = figures(mixed_day)["invoices"]
    # T-1, T-2, R-1, D-1, P-1, U-1, N-1, F-1: OLD-1 is outside the period, E2E-1 is test data
    assert inv["received"] == 8 and inv["to_review"] == 2 and inv["rejected"] == 3


def test_touchless_rate_counts_unreadable_but_not_non_invoices_or_resent_files(mixed_day):
    inv = figures(mixed_day)["invoices"]
    # base: T-1, T-2, R-1, D-1, P-1, U-1 (N-1 not an invoice, F-1 the same file twice: left out)
    # touchless: T-1, T-2 (R-1 was reviewed, D-1 waits for review, P-1 not posted, U-1 unreadable)
    assert (inv["touchless"], inv["touchless_base"]) == (2, 6)
    assert "Touchless: 33% (2 of 6 invoices received went to QuickBooks with no review)" in email(mixed_day)["text"]


def test_a_matching_review_does_not_count_against_touchless(acfg):
    inv = posted_bill(acfg, number="M-1")
    open_review(acfg, inv, code="ambiguous_match", stage="matching")
    assert (figures(acfg)["invoices"]["touchless"], figures(acfg)["invoices"]["touchless_base"]) == (1, 1)


def test_a_posting_review_counts_against_touchless(acfg):
    inv = posted_bill(acfg, number="V-1")
    open_review(acfg, inv, code="vendor_currency_conflict", stage="posting")
    assert figures(acfg)["invoices"]["touchless"] == 0


def test_an_excluded_invoice_is_left_out_of_the_rate(acfg):
    inv = make_invoice(acfg, number="X-1")
    set_cols(acfg, inv, posting_excluded_reason="superseded_test_data", posting_excluded_at="now")
    assert figures(acfg)["invoices"]["touchless_base"] == 0
    assert "Touchless" not in email(acfg)["text"]


def test_the_period_is_the_last_24_hours(acfg):
    received(acfg, "23 hours 59 minutes", number="IN-1")
    received(acfg, "24 hours 1 minute", number="OUT-1")
    assert figures(acfg)["invoices"]["received"] == 1


def test_posted_counts_bills_and_credit_notes_posted_in_the_period(mixed_day):
    posted_bill(mixed_day, number="CN-1", document_type="credit_note", lines=("-10.00",), taxes=(), total="-10.00")
    # T-1, T-2, R-1 and the credit note; OLD-1 was posted two days ago, E2E-1 is test data
    assert figures(mixed_day)["posted"] == {"bills": 3, "credits": 1, "received_earlier": 0}
    assert "Posted to QuickBooks: 3 bills, 1 credit note" in email(mixed_day)["text"]


def test_posted_says_how_many_were_received_before_the_period(acfg):
    today = posted_bill(acfg, number="B-1")
    earlier = posted_bill(acfg, number="B-2")
    sql(acfg, "UPDATE ap.invoices SET received_at = now() - interval '3 days' WHERE id = %s", earlier)
    text = email(acfg)["text"]
    assert today and "Posted to QuickBooks: 2 bills, incl. 1 received earlier\n" in text
    assert "Touchless: 100% (1 of 1 invoice received went to QuickBooks with no review)" in text


def test_a_kind_with_nothing_posted_is_not_mentioned(acfg):
    posted_bill(acfg, number="B-1")
    assert "Posted to QuickBooks: 1 bill\n" in email(acfg)["text"]


# =====================================================================================
# Payments and write-offs
# =====================================================================================
def record_write_off(cfg, inv):
    _, _, (wo,) = write_off_flow(cfg, inv)
    base = f"/write-offs/{wo['write_off_id']}"
    assert c.post(f"{base}/credit", headers=AUTH, json={"external_payment_id": f"VC-{inv[:4]}"}).status_code == 200
    assert c.post(f"{base}/applied", headers=AUTH, json={"external_payment_id": f"BP-{inv[:4]}"}).status_code == 200


def test_payments_and_write_offs_per_currency(acfg):
    assert acfg.write_off_account_id == WRITE_OFF_ACCOUNT
    register_bank(acfg)
    a = posted_bill(acfg, number="A-1")
    b = posted_bill(acfg, number="B-1", total="200.00", lines=("200.00",), taxes=())
    bank_page(acfg, plaid_tx("t1", 640.84), plaid_tx("t2", 199.50, name="QUILLMOOR PAPER SUP B-1"))
    for i, due in enumerate(run(acfg)["payments_due"]):
        pay(due["match_id"], external_id=f"P{i}")
    for inv in (a, b):
        record_write_off(acfg, inv)
    # one write-off in another currency (same table, as a EUR bill would record it)
    sql(acfg, "UPDATE ap.write_offs SET currency = 'EUR' WHERE invoice_id = %s", b)
    f = figures(acfg)
    assert f["payments"] == 2
    assert [(w["currency"], w["count"], str(w["amount"])) for w in f["write_offs"]] == [
        ("EUR", 1, "0.5000"),
        ("USD", 1, "0.4000"),
    ]
    text = email(acfg)["text"]
    assert "Payments matched and recorded: 2" in text
    assert "Written off: 0.50 EUR (1 bill)" in text and "Written off: 0.40 USD (1 bill)" in text
    assert review(acfg, a)["status"] == "written_off"


def test_bank_amount_not_booked_to_the_cent_is_listed(acfg):
    from .test_fx_matching import jpy_payment_with_a_gap

    jpy_payment_with_a_gap(acfg)
    text = email(acfg)["text"]
    assert "Bank amounts not booked to the cent" in text
    assert "架空商事 KS-9 (2,345,678 JPY): bank 14,801.00 USD, booked 14,800.99 USD, gap -0.01 USD" in text
    assert "journal entry between the bank account and Exchange Gain or Loss" in text


def test_exact_foreign_currency_payment_is_not_listed(acfg):
    from .test_fx_matching import ECB_EUR, answer, eur_bill, eur_payment

    register_bank(acfg)
    eur_bill(acfg)
    answer("EUR", 200, ECB_EUR)
    bank_page(acfg, eur_payment(1760.00))
    pay(run(acfg)["payments_due"][0]["match_id"])
    assert figures(acfg)["fx_gaps"] == [] and "not booked to the cent" not in email(acfg)["text"]


# =====================================================================================
# Review items, fraud alerts, first-seen bank details, intake problems
# =====================================================================================
def test_open_review_items_oldest_first_with_their_age(acfg):
    for number, ago in (("NEW-1", "2 hours"), ("OLD-1", "3 days 4 hours"), ("MID-1", "1 day 1 hour")):
        inv = make_invoice(acfg, number=number, status="needs_review", supplier=f"Supplier {number}", account=None)
        open_review(acfg, inv, ago=ago)
    closed = make_invoice(acfg, number="DONE-1", status="needs_review", account=None)
    open_review(acfg, closed)
    sql(acfg, "UPDATE ap.review_queue SET status = 'approved', reviewer = 'x', resolved_at = now() WHERE id = 4")
    lines = email(acfg)["text"].split("Needs review (3, oldest first)\n")[1].split("\n\n")[0].splitlines()
    assert lines == [
        "- 3 days: Supplier OLD-1 OLD-1: Total doesn't add up",
        "- 1 day: Supplier MID-1 MID-1: Total doesn't add up",
        "- today: Supplier NEW-1 NEW-1: Total doesn't add up",
    ]


def test_the_review_list_stops_at_ten(acfg):
    for n in range(12):
        inv = make_invoice(acfg, number=f"L-{n}", status="needs_review", account=None)
        open_review(acfg, inv, ago=f"{12 - n} hours")
    text = email(acfg)["text"]
    assert "Needs review (12, oldest first)" in text and "… and 2 more on the review page." in text
    assert text.count("Total doesn't add up") == 10


def bank_accounts():
    return [{"label": MASKED, "iban": "•••• 3000", "fingerprint": "f" * 64, "holder": "Tarnbrook GmbH"}]


def test_fraud_alerts_and_first_seen_bank_details_are_masked(acfg):
    fraud = make_invoice(acfg, number="F-1", status="needs_review", supplier="Tarnbrook GmbH", account=None)
    set_cols(
        acfg,
        fraud,
        status_reasons=[{"code": "possible_bank_detail_change", "field": "bank_accounts", "message": "x"}],
        bank_accounts=bank_accounts(),
        ocr_text=f"Neue Bankverbindung: {FULL_NUMBER}",
    )
    first = make_invoice(acfg, number="S-1", supplier="Brightwell Studio")
    set_cols(acfg, first, checks={"info": [{"code": "bank_details_first_seen"}]}, bank_accounts=bank_accounts())
    sql(
        acfg,
        """INSERT INTO ap.processing_log (channel, source_ref, step, level, message)
           VALUES ('email', 'gmail:m9', 'intake', 'warning',
                   'possible_bank_detail_change: email asks to change bank details, no invoice attached')""",
    )
    out = email(acfg)
    assert "Tarnbrook GmbH F-1: bank details changed (IBAN •••• 3000)" in out["text"]
    assert "1 email asked to change bank details, with no invoice (Gmail label AP/Fraud alert)." in out["text"]
    assert "Brightwell Studio: IBAN •••• 3000 (read from the invoice, not verified)" in out["text"]
    assert out["subject"].endswith("2 fraud alerts, 2 received, 0 waiting for review")
    assert out["text"].index("Fraud alerts") < out["text"].index("Invoices")
    whole = json.dumps(out, ensure_ascii=False)
    assert FULL_NUMBER not in whole and "0532013" not in whole  # neither the number nor its middle digits


def test_intake_problems_come_from_the_failures_summary(acfg):
    query(
        acfg.database_url,
        """INSERT INTO ap.intake_failures (channel, message_id, state, attempts, last_error_code, given_up_at)
           VALUES ('email', 'm1', 'given_up', 5, 'provider_timeout', now()),
                  ('email', 'm2', 'waiting', 1, 'core_unreachable', NULL)""",
    )
    sql(acfg, "INSERT INTO ap.processing_log (step, level, message) VALUES ('workflow_error', 'error', 'x')")
    text = email(acfg)["text"]
    assert "Given up after 5 attempts: email m1:" in text
    assert "Still retrying: 1 email" in text and "Failed workflow runs: 1" in text


def test_test_and_demo_data_stay_out(acfg):
    for scope in ("e2e", "demo"):
        inv = make_invoice(acfg, number=f"{scope}-1", status="needs_review", account=None)
        open_review(acfg, inv)
        set_scope(acfg.database_url, inv, scope)
    out = email(acfg)
    assert out["text"].startswith(daily_summary.NOTHING)


# =====================================================================================
# A day with nothing to report, through the API
# =====================================================================================
def test_a_day_with_nothing_to_report(acfg, monkeypatch):
    monkeypatch.setenv("DAILY_SUMMARY_TO", "ap@example.test, second@example.test")
    from app.config import get_settings

    get_settings.cache_clear()
    try:
        r = c.get("/summary/daily", headers=AUTH)
    finally:
        get_settings.cache_clear()
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["to"] == "ap@example.test, second@example.test"
    assert out["subject"].endswith(": nothing to report")
    assert out["text"] == f"{daily_summary.NOTHING}\n\n{daily_summary.FOOTER}\n"
    assert "<h2" not in out["html"] and "© David Michel" in out["html"]


def test_the_summary_needs_the_token():
    assert c.get("/summary/daily").status_code == 401
