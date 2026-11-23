"""Integration tests: postings, bank sync and matching against a real Postgres (ap_test)."""

import hashlib
import threading
import uuid
from datetime import date
from decimal import Decimal as D

import psycopg
import pytest
from app import accounting, db, postings
from app.main import app, get_accounting_config
from app.matching import Rules
from fastapi.testclient import TestClient

from .conftest import AUTH, query

ITEM = "plaid-test"
ACCOUNT = "acc-checking"


def sql(cfg, statement, *args):
    return query(cfg.database_url, statement, *args)


def make_invoice(
    cfg,
    *,
    number="QM-1042",
    supplier="Quillmoor Paper Supply LLC",
    key_name="quillmoor paper supply",
    currency="USD",
    lines=("420.00", "171.00"),
    taxes=("50.24",),
    total="641.24",
    status="ready",
    document_type="invoice",
    day=date(2026, 11, 18),
    due=date(2026, 12, 18),
    with_file=True,
):
    inv_id = str(uuid.uuid4())
    rel = f"2026/11/{inv_id}.pdf" if with_file else None
    if rel:
        (cfg.storage_dir / "2026/11").mkdir(parents=True, exist_ok=True)
        (cfg.storage_dir / rel).write_bytes(b"%PDF-1.4 " + inv_id.encode())
    sql(
        cfg,
        """INSERT INTO ap.invoices (id, channel, source_ref, file_name, file_mime_type, file_size_bytes,
                  file_sha256, file_path, document_type, invoice_number, invoice_number_norm, invoice_date, due_date,
                  supplier_name, supplier_name_norm, currency, total, status, tax_mode)
                VALUES (%s, 'email', %s, 'invoice.pdf', 'application/pdf', 100, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, 'exclusive')""",
        inv_id,
        f"test:{inv_id}",
        hashlib.sha256(inv_id.encode()).hexdigest(),
        rel,
        document_type,
        number,
        number.replace("-", "").upper(),
        day,
        due,
        supplier,
        key_name,
        currency,
        D(total),
        status,
    )
    for i, a in enumerate(lines, start=1):
        sql(
            cfg,
            "INSERT INTO ap.invoice_lines (invoice_id, line_no, amount, description) VALUES (%s, %s, %s, %s)",
            inv_id,
            i,
            D(a),
            f"line {i}",
        )
    for a in taxes:
        sql(
            cfg,
            "INSERT INTO ap.invoice_tax_lines (invoice_id, tax_type, label, amount) VALUES (%s, 'vat', 'Tax', %s)",
            inv_id,
            D(a),
        )
    return inv_id


OPEN_BOOKS = postings.BookClose(known=True)  # no closing date set


def claim(cfg, limit=10, close=OPEN_BOOKS):
    with db.connect(cfg.database_url) as conn:
        return accounting.claim_postings(conn, cfg, close, limit)


def result(cfg, invoice_id, **body):
    r = TestClient(app).post(f"/postings/{invoice_id}/result", headers=AUTH, json=body)
    return r


def post_ok(cfg, invoice_id, ext="101", vendor="55", **kw):
    r = result(cfg, invoice_id, outcome="posted", external_id=ext, vendor_external_id=vendor, **kw)
    assert r.status_code == 200, r.text
    return r.json()


def register_bank(cfg, currency="USD"):
    with db.connect(cfg.database_url) as conn:
        accounting.register_account(conn, ITEM, ACCOUNT, currency)


def bank_page(cfg, *txs, has_more=False, removed=(), cursor="c1"):
    with db.connect(cfg.database_url) as conn:
        return accounting.store_sync_page(
            conn, ITEM, list(txs), [], [{"transaction_id": t} for t in removed], cursor, has_more
        )


def plaid_tx(tid, amount, day="2026-11-27", name="QUILLMOOR PAPER SUP ACH", account=ACCOUNT, pending=False):
    return {
        "transaction_id": tid,
        "account_id": account,
        "amount": amount,
        "iso_currency_code": "USD",
        "unofficial_currency_code": None,
        "date": day,
        "authorized_date": None,
        "name": name,
        "merchant_name": None,
        "pending": pending,
        "pending_transaction_id": None,
    }


def run(cfg):
    with db.connect(cfg.database_url) as conn:
        return accounting.run_matching(conn, cfg, ITEM, Rules())


def posting(cfg, invoice_id):
    return sql(cfg, "SELECT * FROM ap.accounting_postings WHERE invoice_id = %s", invoice_id)[0]


# =====================================================================================
# Postings
# =====================================================================================
def test_claim_creates_postings_for_ready_invoices_only(cfg):
    ready = make_invoice(cfg)
    make_invoice(cfg, number="X-1", status="needs_review")
    credit = make_invoice(
        cfg,
        number="CN-1",
        document_type="credit_note",
        lines=("-100.00",),
        taxes=("-20.00",),
        total="-120.00",
        currency="GBP",
        supplier="Thistlecombe",
        key_name="thistlecombe",
    )
    items = {i["invoice_id"]: i for i in claim(cfg)}
    assert set(items) == {ready, credit}
    bill = items[ready]
    assert bill["action"] == "create" and bill["entity"] == "Bill" and bill["needs_exchange_rate"] is False
    assert sum(D(str(ln["Amount"])) for ln in bill["document"]["Line"]) == D("641.24")
    assert bill["document"]["DocNumber"] == "QM-1042" and bill["request_id"] == f"ap-bill-{ready}"
    vc = items[credit]
    assert (
        vc["entity"] == "VendorCredit" and vc["needs_exchange_rate"] and vc["vendor"]["display_name"].endswith("(GBP)")
    )
    assert sum(D(str(ln["Amount"])) for ln in vc["document"]["Line"]) == D("120.00")
    assert posting(cfg, credit)["payment_status"] == "not_applicable"


def test_claim_is_leased(cfg):
    make_invoice(cfg)
    assert len(claim(cfg)) == 1
    assert claim(cfg) == []  # leased: a second run doesn't get it


def test_posted_home_currency_uses_rate_one(cfg):
    inv = make_invoice(cfg)
    claim(cfg)
    p = post_ok(cfg, inv)
    assert p["status"] == "posted" and D(p["exchange_rate"]) == 1 and p["exchange_rate_source"] == "home_currency"
    assert p["payment_status"] == "unpaid" and p["payment_reason"] == "no_match_yet"


def test_foreign_currency_needs_the_rate_used(cfg):
    register_bank(cfg)
    inv = make_invoice(
        cfg, number="AV-1", currency="EUR", supplier="Ateliers Verlibreux", key_name="ateliers verlibreux"
    )
    claim(cfg)
    assert result(cfg, inv, outcome="posted", external_id="7", vendor_external_id="8").status_code == 409
    p = post_ok(cfg, inv, exchange_rate="1.096876", exchange_rate_date="2026-11-20")
    assert D(p["exchange_rate"]) == D("1.096876") and p["exchange_rate_source"] == "qbo"
    assert p["payment_reason"] == "fx_match_pending"  # bank account is USD


def test_repeated_posted_result_is_idempotent_and_a_different_id_conflicts(cfg):
    inv = make_invoice(cfg)
    claim(cfg)
    post_ok(cfg, inv, ext="101")
    assert result(cfg, inv, outcome="posted", external_id="101", vendor_external_id="55").status_code == 200
    assert result(cfg, inv, outcome="posted", external_id="999", vendor_external_id="55").status_code == 409


def test_attachment_deletes_the_stored_file(cfg):
    inv = make_invoice(cfg)
    path = cfg.storage_dir / sql(cfg, "SELECT file_path FROM ap.invoices WHERE id = %s", inv)[0]["file_path"]
    claim(cfg)
    post_ok(cfg, inv, attachment_external_id="900")
    row = sql(cfg, "SELECT file_path, file_deleted_at, file_deletion_reason FROM ap.invoices WHERE id = %s", inv)[0]
    assert not path.exists() and row["file_path"] is None and row["file_deletion_reason"] == "posted"


def test_missing_attachment_is_retried_as_attach_only(cfg):
    inv = make_invoice(cfg)
    claim(cfg)
    post_ok(cfg, inv)  # bill created, upload failed
    sql(cfg, "UPDATE ap.accounting_postings SET next_attempt_at = now()")
    item = claim(cfg)[0]
    assert item["action"] == "attach" and item["document"] is None and item["external_id"] == "101"
    r = result(cfg, inv, outcome="attached", attachment_external_id="901")
    assert r.status_code == 200 and r.json()["attachment_external_id"] == "901"


def test_file_endpoint_serves_and_then_404s(cfg):
    inv = make_invoice(cfg)
    client = TestClient(app)
    assert client.get(f"/invoices/{inv}/file").status_code == 401
    r = client.get(f"/invoices/{inv}/file", headers=AUTH)
    assert r.status_code == 200 and r.content.startswith(b"%PDF") and r.headers["content-type"] == "application/pdf"
    claim(cfg)
    post_ok(cfg, inv, attachment_external_id="900")
    assert client.get(f"/invoices/{inv}/file", headers=AUTH).status_code == 404


def test_permanent_failure_stops_retries(cfg):
    inv = make_invoice(cfg)
    claim(cfg)
    r = result(cfg, inv, outcome="failed", http_status=400, fault_code="6240", error_message="Duplicate Name Exists")
    assert r.json()["status"] == "failed" and r.json()["attempts"] == cfg.max_attempts
    sql(cfg, "UPDATE ap.accounting_postings SET next_attempt_at = now()")
    assert claim(cfg) == []


def test_unpostable_invoice_fails_without_any_api_call(cfg):
    make_invoice(cfg, lines=("100.00",), taxes=(), total="641.24")  # lines don't add up
    assert claim(cfg) == []
    p = sql(cfg, "SELECT status, attempts, last_error FROM ap.accounting_postings")[0]
    assert p["status"] == "failed" and p["last_error"].startswith("lines_do_not_sum_to_total")


def test_excluded_invoice_is_never_posted(cfg):
    inv = make_invoice(cfg)
    r = TestClient(app).post(
        f"/invoices/{inv}/exclude-from-posting",
        headers=AUTH,
        json={"reason": "superseded_test_data", "note": "renamed test supplier"},
    )
    assert r.status_code == 200 and r.json()["posting_excluded_reason"] == "superseded_test_data"
    assert claim(cfg) == []
    logs = sql(cfg, "SELECT message, details FROM ap.processing_log WHERE invoice_id = %s", inv)
    assert logs[0]["message"] == "excluded from posting"


def test_cannot_exclude_after_posting(cfg):
    inv = make_invoice(cfg)
    claim(cfg)
    post_ok(cfg, inv)
    r = TestClient(app).post(f"/invoices/{inv}/exclude-from-posting", headers=AUTH, json={"reason": "manual"})
    assert r.status_code == 409


def test_vendor_link_conflict(cfg):
    c = TestClient(app)
    body = {"supplier_key": "quillmoor paper supply", "currency": "USD", "external_id": "55", "display_name": "Q"}
    assert c.put("/vendor-links", headers=AUTH, json=body).status_code == 200
    assert c.put("/vendor-links", headers=AUTH, json=body).status_code == 200  # same: idempotent
    assert c.put("/vendor-links", headers=AUTH, json={**body, "external_id": "56"}).status_code == 409
    # the same QuickBooks vendor can't serve another currency (a vendor's currency is fixed)
    assert c.put("/vendor-links", headers=AUTH, json={**body, "currency": "EUR"}).status_code == 409


# =====================================================================================
# Bank sync
# =====================================================================================
def test_sync_keeps_only_the_matching_account_and_saves_cursor_last(cfg):
    register_bank(cfg)
    r = bank_page(cfg, plaid_tx("t1", 10.5), plaid_tx("t2", 99, account="acc-credit-card"), has_more=True)
    assert r == {"stored": 1, "skipped_other_accounts": 1, "removed": 0, "cursor_saved": False}
    assert sql(cfg, "SELECT cursor FROM ap.bank_sync_state")[0]["cursor"] is None
    bank_page(cfg, plaid_tx("t1", 10.5), cursor="c2")  # page re-sent: idempotent
    assert sql(cfg, "SELECT cursor FROM ap.bank_sync_state")[0]["cursor"] == "c2"
    assert sql(cfg, "SELECT count(*) AS n FROM ap.bank_transactions")[0]["n"] == 1


def test_amounts_keep_plaid_precision_and_sign(cfg):
    register_bank(cfg)
    bank_page(cfg, plaid_tx("t1", 2.8096795), plaid_tx("t2", -56.78))
    rows = {r["transaction_id"]: r["amount"] for r in sql(cfg, "SELECT * FROM ap.bank_transactions")}
    assert rows == {"t1": D("2.80967950"), "t2": D("-56.78000000")}


def test_removed_transactions_are_marked(cfg):
    register_bank(cfg)
    bank_page(cfg, plaid_tx("t1", 10))
    bank_page(cfg, removed=["t1"], cursor="c2")
    assert sql(cfg, "SELECT removed_at FROM ap.bank_transactions")[0]["removed_at"] is not None


# =====================================================================================
# Matching
# =====================================================================================
def posted_bill(cfg, **kw):
    inv = make_invoice(cfg, **kw)
    claim(cfg)
    post_ok(cfg, inv, ext=f"b-{inv[:6]}")
    return inv


def test_exact_match_is_paid(cfg):
    register_bank(cfg)
    inv = posted_bill(cfg)
    bank_page(cfg, plaid_tx("t1", 641.24))
    out = run(cfg)
    assert out["accepted"] == 1 and posting(cfg, inv)["payment_reason"] == "payment_pending"
    pay = out["payments_due"][0]
    assert pay["document"]["TotalAmt"] == 641.24 and pay["document"]["Line"][0]["LinkedTxn"][0]["TxnId"].startswith(
        "b-"
    )
    assert pay["document"]["CheckPayment"]["BankAccountRef"]["value"] == "35"
    r = TestClient(app).post(f"/matches/{pay['match_id']}/payment", headers=AUTH, json={"external_payment_id": "P1"})
    assert r.status_code == 200 and r.json()["payment_status"] == "paid" and D(r.json()["amount_paid"]) == D("641.24")


@pytest.mark.parametrize("bank, applied", [("641.64", "641.24"), ("640.84", "640.84")])
def test_difference_within_tolerance_is_flagged(cfg, bank, applied):
    register_bank(cfg)
    posted_bill(cfg)
    bank_page(cfg, plaid_tx("t1", float(bank)))
    pay = run(cfg)["payments_due"][0]
    assert D(str(pay["document"]["TotalAmt"])) == D(applied)  # never more than the bill
    with db.connect(cfg.database_url) as conn:
        p = accounting.record_payment(conn, pay["match_id"], "P1")
    assert p["payment_status"] == "paid_with_difference" and p["payment_reason"] == "payment_difference"
    assert p["amount_paid"] == D(applied)
    m = sql(cfg, "SELECT bank_amount, difference FROM ap.matches")[0]
    assert m["bank_amount"] == D(bank) and m["difference"] == D(bank) - D("641.24")  # actual bank amount kept


def test_incoming_money_is_never_matched(cfg):
    register_bank(cfg)
    inv = posted_bill(cfg)
    bank_page(cfg, plaid_tx("t1", -641.24))
    assert run(cfg)["accepted"] == 0 and posting(cfg, inv)["payment_reason"] == "no_match_yet"


def test_ambiguous_match_keeps_candidates_and_pays_nothing(cfg):
    register_bank(cfg)
    inv = posted_bill(cfg)
    bank_page(cfg, plaid_tx("t1", 641.24), plaid_tx("t2", 641.24, day="2026-11-28"))
    out = run(cfg)
    assert out["payments_due"] == [] and posting(cfg, inv)["payment_reason"] == "ambiguous_match"
    assert [r["status"] for r in sql(cfg, "SELECT status FROM ap.matches")] == ["candidate", "candidate"]


def test_ambiguous_match_opens_one_matching_review(cfg):
    register_bank(cfg)
    first = posted_bill(cfg)
    second = posted_bill(cfg, number="QM-1043")  # same supplier, same amount
    bank_page(cfg, plaid_tx("t1", 641.24))
    run(cfg)
    run(cfg)  # the hourly run sees the same ambiguity: still one review item per bill
    reviews = sql(cfg, "SELECT invoice_id, stage, status, reasons FROM ap.review_queue ORDER BY id")
    assert [(str(r["invoice_id"]), r["stage"], r["status"]) for r in reviews] == [
        (first, "matching", "open"),
        (second, "matching", "open"),
    ]
    assert {r["reasons"][0]["code"] for r in reviews} == {"ambiguous_match"}
    assert {r["reasons"][0]["detail"] for r in reviews} == {"candidate bank transactions: t1"}
    assert len(sql(cfg, "SELECT id FROM ap.processing_log WHERE message = 'matching needs review'")) == 2
    assert run(cfg)["payments_due"] == []


def test_foreign_currency_bill_waits_for_fx(cfg):
    register_bank(cfg)
    inv = make_invoice(cfg, number="AV-1", currency="EUR", supplier="Quillmoor", key_name="quillmoor paper supply")
    claim(cfg)
    post_ok(cfg, inv, exchange_rate="1.1", exchange_rate_date="2026-11-18")
    bank_page(cfg, plaid_tx("t1", 641.24))
    out = run(cfg)
    assert out["fx_match_pending"] == 1 and out["accepted"] == 0
    assert posting(cfg, inv)["payment_reason"] == "fx_match_pending"


def test_a_used_transaction_is_not_matched_again(cfg):
    register_bank(cfg)
    first = posted_bill(cfg)
    bank_page(cfg, plaid_tx("t1", 641.24))
    pay = run(cfg)["payments_due"][0]
    with db.connect(cfg.database_url) as conn:
        accounting.record_payment(conn, pay["match_id"], "P1")
    second = posted_bill(cfg, number="QM-1043")
    assert run(cfg)["accepted"] == 0 and posting(cfg, second)["payment_reason"] == "no_match_yet"
    assert posting(cfg, first)["payment_status"] == "paid"


def test_database_allows_one_invoice_per_transaction(cfg):
    """Change 4: enforced by the database itself, not only by the matching code."""
    register_bank(cfg)
    a, b = posted_bill(cfg), posted_bill(cfg, number="QM-1043")
    bank_page(cfg, plaid_tx("t1", 641.24), plaid_tx("t2", 641.24))
    insert = """INSERT INTO ap.matches (invoice_id, bank_transaction_id, status, invoice_amount, bank_amount,
                  difference, currency, days_from_invoice, name_similarity, invoice_number_found, rule_version)
                VALUES (%s, %s, 'accepted', 641.24, 641.24, 0, 'USD', 9, 1, false, 'm1')"""
    sql(cfg, insert, a, "t1")
    with pytest.raises(psycopg.errors.UniqueViolation):
        sql(cfg, insert, b, "t1")  # same transaction, second invoice
    with pytest.raises(psycopg.errors.UniqueViolation):
        sql(cfg, insert, a, "t2")  # same invoice, second transaction


def test_payments_due_are_leased(cfg):
    register_bank(cfg)
    posted_bill(cfg)
    bank_page(cfg, plaid_tx("t1", 641.24))
    assert len(run(cfg)["payments_due"]) == 1
    assert run(cfg)["payments_due"] == []  # second run doesn't get it again


def test_concurrent_matching_runs_produce_one_payment(cfg):
    register_bank(cfg)
    posted_bill(cfg)
    bank_page(cfg, plaid_tx("t1", 641.24))
    due = []

    def go():
        due.extend(run(cfg)["payments_due"])

    threads = [threading.Thread(target=go) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(due) == 1
    assert sql(cfg, "SELECT count(*) AS n FROM ap.matches WHERE status = 'accepted'")[0]["n"] == 1


def test_payment_recorded_twice_is_idempotent_and_conflicts_on_other_id(cfg):
    register_bank(cfg)
    posted_bill(cfg)
    bank_page(cfg, plaid_tx("t1", 641.24))
    mid = run(cfg)["payments_due"][0]["match_id"]
    c = TestClient(app)
    assert c.post(f"/matches/{mid}/payment", headers=AUTH, json={"external_payment_id": "P1"}).status_code == 200
    assert c.post(f"/matches/{mid}/payment", headers=AUTH, json={"external_payment_id": "P1"}).status_code == 200
    assert c.post(f"/matches/{mid}/payment", headers=AUTH, json={"external_payment_id": "P2"}).status_code == 409


# =====================================================================================
# Posting failures that need a person
# =====================================================================================
def test_vendor_currency_conflict_opens_a_posting_review(cfg):
    inv = make_invoice(
        cfg, number="AV-1", currency="EUR", supplier="Ateliers Verlibreux", key_name="ateliers verlibreux"
    )
    claim(cfg)
    r = result(
        cfg,
        inv,
        outcome="failed",
        error_code="vendor_currency_conflict",
        error_message="vendor 58 uses USD",
        http_status=503,
    )  # a failure that needs a person is never retried, whatever the HTTP status
    assert r.status_code == 200 and r.json()["status"] == "failed" and r.json()["attempts"] == cfg.max_attempts
    review = sql(cfg, "SELECT * FROM ap.review_queue WHERE invoice_id = %s", inv)
    assert len(review) == 1 and review[0]["stage"] == "posting" and review[0]["status"] == "open"
    assert review[0]["reasons"][0]["code"] == "vendor_currency_conflict"
    assert sql(cfg, "SELECT status FROM ap.invoices WHERE id = %s", inv)[0]["status"] == "ready"  # data is fine
    # reported again (e.g. a manual re-run): still one open review item
    result(cfg, inv, outcome="failed", error_code="vendor_currency_conflict", error_message="again")
    assert len(sql(cfg, "SELECT id FROM ap.review_queue WHERE invoice_id = %s", inv)) == 1
    sql(cfg, "UPDATE ap.accounting_postings SET next_attempt_at = now()")
    assert claim(cfg) == []  # no blind retries


def test_ordinary_failure_opens_no_review(cfg):
    inv = make_invoice(cfg)
    claim(cfg)
    result(cfg, inv, outcome="failed", http_status=503, error_message="unavailable")
    assert sql(cfg, "SELECT id FROM ap.review_queue") == []
    assert posting(cfg, inv)["attempts"] == 1  # will be retried


def test_claim_returns_the_api_base(cfg):
    make_invoice(cfg)
    app.dependency_overrides[get_accounting_config] = lambda: accounting.AccountingConfig(
        **{**cfg.__dict__, "qbo_base": "https://sandbox.example/v3/company/123"}
    )
    c = TestClient(app)
    assert c.get("/accounting/target", headers=AUTH).json()["qbo_base"] == "https://sandbox.example/v3/company/123"
    r = c.post("/postings/claim", headers=AUTH, json={"preferences": {"status_code": 200, "body": {"Preferences": {}}}})
    assert r.json()["qbo_base"] == "https://sandbox.example/v3/company/123" and r.json()["count"] == 1


def test_request_ids_fit_quickbooks_limit(cfg):
    make_invoice(cfg)
    make_invoice(
        cfg, number="CN-1", document_type="credit_note", lines=("-100.00",), taxes=("-20.00",), total="-120.00"
    )
    items = claim(cfg)
    assert {i["entity"] for i in items} == {"Bill", "VendorCredit"}
    assert all(len(i["request_id"]) <= 50 for i in items)


def test_retry_resets_a_failed_posting_only(cfg):
    inv = make_invoice(cfg)
    claim(cfg)
    c = TestClient(app)
    assert c.post(f"/postings/{inv}/retry", headers=AUTH, json={}).status_code == 409  # still pending
    result(cfg, inv, outcome="failed", http_status=400, fault_code="6000", error_message="bad")
    r = c.post(f"/postings/{inv}/retry", headers=AUTH, json={"note": "request id fixed"})
    assert r.status_code == 200 and r.json()["attempts"] == 0
    assert claim(cfg)[0]["invoice_id"] == inv


def test_retry_refused_while_a_posting_review_is_open(cfg):
    inv = make_invoice(cfg)
    claim(cfg)
    result(cfg, inv, outcome="failed", error_code="vendor_currency_conflict", error_message="x")
    assert TestClient(app).post(f"/postings/{inv}/retry", headers=AUTH, json={}).status_code == 409


def test_lookup_endpoints_decide_from_quickbooks_answers(cfg):
    inv = make_invoice(cfg, currency="EUR")
    claim(cfg)
    c = TestClient(app)
    found = {"QueryResponse": {"Vendor": [{"Id": "58", "CurrencyRef": {"value": "USD"}, "Active": True}]}}
    r = c.post(f"/postings/{inv}/lookup/vendor", headers=AUTH, json={"status_code": 200, "body": found})
    assert r.status_code == 200 and r.json()["decision"] == "review"
    assert r.json()["error_code"] == "vendor_currency_conflict"
    bills = {"QueryResponse": {"Bill": [{"Id": "150", "VendorRef": {"value": "58"}}]}}
    r = c.post(
        f"/postings/{inv}/lookup/document", headers=AUTH, json={"status_code": 200, "body": bills, "vendor_id": "58"}
    )
    assert r.json() == {**r.json(), "decision": "exists", "external_id": "150"}
    unknown = "00000000-0000-0000-0000-000000000000"
    assert c.post(f"/postings/{unknown}/lookup/vendor", headers=AUTH, json={"status_code": 200}).status_code == 404


def test_duplicate_in_accounting_opens_a_review_on_the_invoice_number(cfg):
    inv = make_invoice(cfg)
    claim(cfg)
    result(cfg, inv, outcome="failed", error_code="duplicate_in_accounting", error_message="2 Bill records")
    review = sql(cfg, "SELECT stage, reasons FROM ap.review_queue WHERE invoice_id = %s", inv)[0]
    assert review["stage"] == "posting" and review["reasons"][0]["field"] == "invoice_number"


def test_foreign_bill_posted_before_any_bank_account_waits_for_fx(cfg):
    """KS-2026-088 (JPY) was posted before a bank account was registered and got no_match_yet."""
    inv = make_invoice(
        cfg,
        number="KS-1",
        currency="JPY",
        supplier="架空商事",
        key_name="架空商事",
        lines=("16500",),
        taxes=("1635",),
        total="18135",
    )
    claim(cfg)
    p = post_ok(cfg, inv, exchange_rate="0.006306", exchange_rate_date="2026-11-20")
    assert p["payment_reason"] == "fx_match_pending"  # home currency USD assumed for payments


def test_home_currency_bill_posted_before_any_bank_account_waits_for_a_match(cfg):
    inv = make_invoice(cfg)
    claim(cfg)
    assert post_ok(cfg, inv)["payment_reason"] == "no_match_yet"


# =====================================================================================
# Bill payments: found again after a crash, failures classified by the core
# =====================================================================================
def due_payment(cfg):
    register_bank(cfg)
    inv = posted_bill(cfg)
    bank_page(cfg, plaid_tx("t1", 641.24))
    return inv, run(cfg)["payments_due"][0]


def test_payment_carries_a_number_to_find_it_again(cfg):
    _, pay = due_payment(cfg)
    assert pay["doc_number"] == f"AP-PAY-{pay['match_id']}" == pay["document"]["DocNumber"]
    assert pay["vendor_external_id"] == pay["document"]["VendorRef"]["value"] == "55"


def test_payment_lookup_reuses_an_existing_payment(cfg):
    _, pay = due_payment(cfg)
    c, url = TestClient(app), f"/matches/{pay['match_id']}/lookup"
    none: dict[str, dict[str, object]] = {"QueryResponse": {}}
    assert c.post(url, headers=AUTH, json={"status_code": 200, "body": none}).json()["decision"] == "create"
    found = {"QueryResponse": {"BillPayment": [{"Id": "88", "VendorRef": {"value": "55"}}]}}
    r = c.post(url, headers=AUTH, json={"status_code": 200, "body": found}).json()
    assert (r["decision"], r["external_id"]) == ("exists", "88")


def test_payment_failure_retryable_or_not(cfg):
    inv, pay = due_payment(cfg)
    c, url = TestClient(app), f"/matches/{pay['match_id']}/payment-failed"
    r = c.post(url, headers=AUTH, json={"http_status": 503, "error_message": "unavailable"})
    assert r.status_code == 200 and r.json()["payment_attempts"] == 1 and r.json()["payment_claimed_until"] is None
    assert [p["match_id"] for p in run(cfg)["payments_due"]] == [pay["match_id"]]  # retried at once (lease cleared)
    r = c.post(url, headers=AUTH, json={"http_status": 400, "fault_code": "6000", "error_message": "bad"})
    assert r.json()["payment_attempts"] == cfg.max_attempts and r.json()["payment_last_error"].startswith("qbo_6000")
    assert run(cfg)["payments_due"] == []  # stopped: a person looks at it
    assert posting(cfg, inv)["payment_status"] == "unpaid"


# =====================================================================================
# Closed periods (QuickBooks book close date)
# =====================================================================================
def test_nothing_is_claimed_while_the_book_close_date_is_unknown(cfg):
    make_invoice(cfg)
    assert claim(cfg, close=postings.BookClose(known=False)) == []
    assert (
        sql(cfg, "SELECT message FROM ap.processing_log")[-1]["message"]
        == "posting paused: book close date unavailable"
    )
    assert len(claim(cfg)) == 1  # posted once the date is known again


def test_invoice_dated_in_a_closed_period_goes_to_review(cfg):
    inv = make_invoice(cfg, day=date(2026, 10, 30), due=date(2026, 11, 30))
    assert claim(cfg, close=postings.BookClose(known=True, closed_through=date(2026, 10, 31))) == []
    p = posting(cfg, inv)
    assert p["status"] == "failed" and p["attempts"] == cfg.max_attempts and p["last_error"].startswith("period_closed")
    review = sql(cfg, "SELECT stage, reasons FROM ap.review_queue WHERE invoice_id = %s", inv)
    assert [(r["stage"], r["reasons"][0]["code"]) for r in review] == [("posting", "period_closed")]


def test_service_period_in_a_closed_period_goes_to_review(cfg):
    inv = make_invoice(cfg)  # invoice dated 2026-11-18, in an open period
    sql(cfg, "UPDATE ap.invoices SET service_period_end = '2026-10-31' WHERE id = %s", inv)
    assert claim(cfg, close=postings.BookClose(known=True, closed_through=date(2026, 10, 31))) == []
    assert "service period ending 2026-10-31" in posting(cfg, inv)["last_error"]


def test_open_period_is_posted_normally(cfg):
    make_invoice(cfg)
    assert len(claim(cfg, close=postings.BookClose(known=True, closed_through=date(2026, 10, 31)))) == 1


def test_book_close_date_read_from_the_preferences():
    prefs = {"Preferences": {"AccountingInfoPrefs": {"BookCloseDate": "2026-10-31"}}}
    assert postings.book_close(200, prefs) == postings.BookClose(True, date(2026, 10, 31))
    assert postings.book_close(200, {"Preferences": {"AccountingInfoPrefs": {}}}) == postings.BookClose(True, None)
    assert postings.book_close(401, {"Fault": {}}) == postings.BookClose(False)
    assert postings.book_close(200, {"Preferences": {"AccountingInfoPrefs": {"BookCloseDate": "soon"}}}).known is False


def test_claim_endpoint_reads_the_preferences(cfg):
    make_invoice(cfg)
    c = TestClient(app)
    unknown = c.post("/postings/claim", headers=AUTH, json={"preferences": {"status_code": 503, "body": None}})
    assert unknown.json()["count"] == 0 and unknown.json()["book_close"] == {"known": False, "closed_through": None}
    ok = c.post(
        "/postings/claim", headers=AUTH, json={"preferences": {"status_code": 200, "body": {"Preferences": {}}}}
    )
    assert ok.json()["count"] == 1
