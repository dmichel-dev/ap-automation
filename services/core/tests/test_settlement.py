"""Review decisions that take effect in QuickBooks (settlement.py): confirming or rejecting a bank match,
writing off or keeping a payment difference, retrying after a failed call; and the "run now" gate.
Real Postgres; QuickBooks answers are passed in as the workflow would pass them."""

import hashlib
import time
from decimal import Decimal as D

import pytest
from app import db, run_now, settlement
from app.main import app
from fastapi.testclient import TestClient

from .conftest import AUTH, AUTOMATION, WRITE_OFF_ACCOUNT
from .test_accounting import bank_page, plaid_tx, posted_bill, posting, register_bank, run, sql

c = TestClient(app)


def review(cfg, invoice_id):
    rows = sql(cfg, "SELECT * FROM ap.review_queue WHERE invoice_id = %s ORDER BY id", invoice_id)
    return rows[-1]


def item(review_id):
    return c.get(f"/review/items/{review_id}", headers=AUTH).json()


def act(review_id, action, **body):
    return c.post(f"/review/items/{review_id}/{action}", headers=AUTH, json={"reviewer": AUTOMATION, **body})


def pay(match_id, external_id="P1"):
    r = c.post(f"/matches/{match_id}/payment", headers=AUTH, json={"external_payment_id": external_id})
    assert r.status_code == 200, r.text
    return r.json()


def log_messages(cfg, invoice_id):
    return [
        r["message"]
        for r in sql(cfg, "SELECT message FROM ap.processing_log WHERE invoice_id = %s ORDER BY id", invoice_id)
    ]


# =====================================================================================
# Ambiguous matches: the reviewer confirms one transaction, or none
# =====================================================================================
@pytest.fixture
def ambiguous(acfg):
    """Two bills of 641.24 from one supplier, one bank payment of 641.24: a matching item on each."""
    register_bank(acfg)
    first = posted_bill(acfg)
    second = posted_bill(acfg, number="QM-1043")
    bank_page(acfg, plaid_tx("t1", 641.24))
    run(acfg)
    return acfg, first, second


def test_a_matching_item_offers_confirm_or_reject_with_its_candidates(ambiguous):
    cfg, first, _ = ambiguous
    data = item(review(cfg, first)["id"])
    assert data["actions"] == ["confirm_match", "reject_match"]
    assert [(m["bank_transaction_id"], m["status"], m["bank_amount"]) for m in data["payment"]["matches"]] == [
        ("t1", "candidate", "641.2400")
    ]


def test_confirming_a_match_waits_for_the_payment_then_closes(ambiguous):
    cfg, first, second = ambiguous
    rid = review(cfg, first)["id"]
    r = act(rid, "confirm-match", bank_transaction_id="t1", note="remittance names QM-1042")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "open" and data["waiting_for"] == "payment" and data["actions"] == []
    assert data["reviewer"] == AUTOMATION and data["decision"]["action"] == "confirm_match"
    assert posting(cfg, first)["payment_reason"] == "payment_pending"
    # the other bill's item waited on the same transaction: settled by this decision, its bill stays unpaid
    other = review(cfg, second)
    assert other["status"] == "rejected" and other["decision"]["action"] == "closed_by_other_decision"
    assert posting(cfg, second)["payment_reason"] == "no_match_yet"
    # the bank workflow records the payment; then the item closes
    due = run(cfg)["payments_due"]
    assert [d["invoice_id"] for d in due] == [first]
    pay(due[0]["match_id"])
    closed = review(cfg, first)
    assert closed["status"] == "approved" and closed["waiting_for"] is None and closed["resolved_at"]
    assert posting(cfg, first)["payment_status"] == "paid"
    assert "review approved: recorded in QuickBooks" in log_messages(cfg, first)


def test_only_a_candidate_can_be_confirmed(ambiguous):
    cfg, first, _ = ambiguous
    bank_page(cfg, plaid_tx("t9", 12.00, name="COFFEE SHOP"), cursor="c2")
    rid = review(cfg, first)["id"]
    r = act(rid, "confirm-match", bank_transaction_id="t9")
    assert r.status_code == 409 and "not a candidate" in r.json()["message"]
    assert review(cfg, first)["status"] == "open" and review(cfg, first)["waiting_for"] is None


def test_rejected_candidates_are_never_proposed_again(ambiguous):
    cfg, first, second = ambiguous
    assert act(review(cfg, first)["id"], "reject-match", note="").status_code == 409  # a reason is needed
    r = act(review(cfg, first)["id"], "reject-match", note="t1 paid the other bill")
    assert r.status_code == 200 and r.json()["status"] == "rejected"
    assert posting(cfg, first)["payment_reason"] == "no_match_yet"
    # next run: t1 is now the only candidate of the second bill, and it fits nothing else
    out = run(cfg)
    assert out["accepted"] == 1 and [d["invoice_id"] for d in out["payments_due"]] == [second]
    assert posting(cfg, first)["payment_reason"] == "no_match_yet"
    assert len(sql(cfg, "SELECT 1 FROM ap.review_queue WHERE invoice_id = %s", first)) == 1  # no new item


# =====================================================================================
# Payment differences: under-payments are written off or left open; over-payments logged
# =====================================================================================
@pytest.fixture
def underpaid(acfg):
    """A bill of 641.24 paid 640.84 by the bank: recorded at the bank amount, 0.40 open."""
    register_bank(acfg)
    inv = posted_bill(acfg)
    bank_page(acfg, plaid_tx("t1", 640.84))
    pay(run(acfg)["payments_due"][0]["match_id"])
    return acfg, inv


def test_an_under_payment_opens_a_difference_review(underpaid):
    cfg, inv = underpaid
    rev = review(cfg, inv)
    assert rev["stage"] == "matching" and rev["reasons"][0]["code"] == "payment_difference"
    assert rev["reasons"][0]["detail"] == "bill 641.24, bank 640.84, open 0.40 USD"
    assert item(rev["id"])["actions"] == ["write_off", "keep_open"]


def test_an_over_payment_is_logged_not_reviewed(acfg):
    register_bank(acfg)
    inv = posted_bill(acfg)
    bank_page(acfg, plaid_tx("t1", 641.64))
    pay(run(acfg)["payments_due"][0]["match_id"])
    assert sql(acfg, "SELECT 1 FROM ap.review_queue WHERE invoice_id = %s", inv) == []
    log = sql(
        acfg, "SELECT level, details FROM ap.processing_log WHERE message = 'over-payment not recorded in QuickBooks'"
    )
    assert log[0]["level"] == "warning" and log[0]["details"]["over_payment"] == "0.4000"


def write_off_flow(cfg, inv):
    """Decides the write-off and claims it, as the bank workflow does."""
    rid = review(cfg, inv)["id"]
    r = act(rid, "write-off", note="bank fee, agreed with the supplier")
    assert r.status_code == 200, r.text
    with db.connect(cfg.database_url) as conn:
        due = settlement.write_offs_due(conn, cfg)
    return rid, r.json(), due


def test_a_write_off_waits_for_quickbooks_then_closes(underpaid):
    cfg, inv = underpaid
    _, data, due = write_off_flow(cfg, inv)
    assert data["status"] == "open" and data["waiting_for"] == "write_off" and data["actions"] == []
    assert data["decision"]["amount"] == "0.4000" and data["reviewer"] == AUTOMATION
    # the vendor credit: on the payment-differences account, numbered for the lookup
    (wo,) = due
    assert wo["doc_number"] == f"AP-WO-{wo['write_off_id']}" and wo["credit"]["DocNumber"] == wo["doc_number"]
    line = wo["credit"]["Line"][0]
    assert line["Amount"] == 0.4 and line["AccountBasedExpenseLineDetail"]["AccountRef"]["value"] == WRITE_OFF_ACCOUNT
    base = f"/write-offs/{wo['write_off_id']}"
    nothing = {"status_code": 200, "body": {"QueryResponse": {}}}
    assert c.post(f"{base}/lookup/credit", headers=AUTH, json=nothing).json()["decision"] == "create"
    apply = c.post(f"{base}/credit", headers=AUTH, json={"external_payment_id": "VC9"}).json()["apply"]
    # the bill payment of 0 applies the credit to the bill: no money moves
    assert apply["TotalAmt"] == 0 and apply["DocNumber"] == wo["doc_number"]
    assert [(ln["Amount"], ln["LinkedTxn"][0]["TxnType"]) for ln in apply["Line"]] == [
        (0.4, "Bill"),
        (0.4, "VendorCredit"),
    ]
    assert apply["Line"][1]["LinkedTxn"][0]["TxnId"] == "VC9"
    assert c.post(f"{base}/lookup/apply", headers=AUTH, json=nothing).json()["decision"] == "create"
    r = c.post(f"{base}/applied", headers=AUTH, json={"external_payment_id": "BP7"})
    assert r.status_code == 200 and r.json()["status"] == "recorded"
    p = posting(cfg, inv)
    assert p["payment_status"] == "paid_with_write_off" and p["amount_written_off"] == D("0.40")
    closed = review(cfg, inv)
    assert closed["status"] == "written_off" and closed["waiting_for"] is None and closed["resolved_at"]
    assert "payment difference written off" in log_messages(cfg, inv)
    # repeated report (a run that stopped after recording): same answer, nothing new
    assert c.post(f"{base}/applied", headers=AUTH, json={"external_payment_id": "BP7"}).status_code == 200
    assert c.post(f"{base}/applied", headers=AUTH, json={"external_payment_id": "BP8"}).status_code == 409


def test_a_rerun_finds_the_write_off_already_in_quickbooks(underpaid):
    cfg, inv = underpaid
    _, _, (wo,) = write_off_flow(cfg, inv)
    base = f"/write-offs/{wo['write_off_id']}"
    vendor = posting(cfg, inv)["vendor_external_id"]
    found = {
        "status_code": 200,
        "body": {"QueryResponse": {"VendorCredit": [{"Id": "VC9", "VendorRef": {"value": vendor}}]}},
    }
    decision = c.post(f"{base}/lookup/credit", headers=AUTH, json=found).json()
    assert decision == {**decision, "decision": "exists", "external_id": "VC9"}  # reused, not created twice


def test_a_failed_write_off_stays_open_with_the_error_and_can_be_retried(underpaid):
    cfg, inv = underpaid
    rid, _, (wo,) = write_off_flow(cfg, inv)
    base = f"/write-offs/{wo['write_off_id']}"
    # a transient failure: retried by the next run; the item still waits and shows the last error
    c.post(f"{base}/failed", headers=AUTH, json={"http_status": 503, "error_message": "unavailable"})
    rev = review(cfg, inv)
    assert rev["waiting_for"] == "write_off" and "unavailable" in rev["effect_error"]
    # a validation error: no automatic retry; the item stops waiting and offers a retry
    with db.connect(cfg.database_url) as conn:
        settlement.write_offs_due(conn, cfg)
    c.post(
        f"{base}/failed", headers=AUTH, json={"http_status": 400, "fault_code": "6000", "error_message": "bad account"}
    )
    data = item(rid)
    assert data["status"] == "open" and data["waiting_for"] is None and data["actions"] == ["retry"]
    assert data["effect_error"].startswith("qbo_6000: bad account")
    with db.connect(cfg.database_url) as conn:
        assert settlement.write_offs_due(conn, cfg) == []  # nothing is retried on its own
    r = act(rid, "retry", note="account fixed in QuickBooks")
    assert r.status_code == 200 and r.json()["waiting_for"] == "write_off" and r.json()["effect_error"] is None
    assert r.json()["decision"]["action"] == "write_off" and r.json()["decision"]["retries"] == 1  # decision stands
    with db.connect(cfg.database_url) as conn:
        assert [w["doc_number"] for w in settlement.write_offs_due(conn, cfg)] == [wo["doc_number"]]  # same number
    assert len(sql(cfg, "SELECT 1 FROM ap.write_offs")) == 1


def test_keeping_the_difference_open_closes_the_item(underpaid):
    cfg, inv = underpaid
    r = act(review(cfg, inv)["id"], "keep-open", note="supplier will invoice the rest")
    assert r.status_code == 200 and r.json()["status"] == "approved"
    assert r.json()["decision"] == {**r.json()["decision"], "action": "keep_open", "open_amount": "0.4000"}
    assert (
        posting(cfg, inv)["payment_status"] == "paid_with_difference" and sql(cfg, "SELECT 1 FROM ap.write_offs") == []
    )


def test_a_write_off_needs_an_under_paid_bill(underpaid):
    cfg, inv = underpaid
    sql(cfg, "UPDATE ap.accounting_postings SET payment_status = 'paid', payment_reason = NULL, amount_paid = amount")
    r = act(review(cfg, inv)["id"], "write-off")
    assert r.status_code == 409 and "no payment difference" in r.json()["message"]
    assert review(cfg, inv)["status"] == "open"


# =====================================================================================
# Failed postings: the item shows the error; a retry never posts twice
# =====================================================================================
def test_a_final_posting_failure_opens_an_item_that_offers_a_retry(acfg):
    from .test_accounting import claim, make_invoice, post_ok, result

    inv = make_invoice(acfg)
    claim(acfg)
    result(acfg, inv, outcome="failed", http_status=400, fault_code="6000", error_message="Business Validation Error")
    rev = review(acfg, inv)
    assert rev["stage"] == "posting" and rev["reasons"][0]["code"] == "quickbooks_refused"
    assert "Business Validation Error" in rev["reasons"][0]["detail"]
    assert item(rev["id"])["actions"] == ["retry"]
    r = act(rev["id"], "retry", note="vendor fixed in QuickBooks")
    assert r.status_code == 200 and r.json()["waiting_for"] == "posting"
    assert [p["invoice_id"] for p in claim(acfg)] == [inv]  # tried again (the lookup by number comes first)
    post_ok(acfg, inv)
    assert review(acfg, inv)["status"] == "approved" and review(acfg, inv)["resolved_at"]


# =====================================================================================
# "Run now": signed requests (the token never travels), one use each, rate limits
# =====================================================================================
TOKEN = "s3cret-run-now-token-for-tests"


@pytest.fixture
def gate(acfg, monkeypatch):
    monkeypatch.setenv("RUN_NOW_TOKEN", TOKEN)
    from app.config import get_settings

    get_settings.cache_clear()
    yield acfg
    get_settings.cache_clear()


def ask(target="posting", token=TOKEN, timestamp=None, signature=None):
    ts = str(int(time.time())) if timestamp is None else timestamp
    sig = run_now.sign(token, ts, target) if signature is None else signature
    body = {"timestamp": ts, "signature": sig, "target": target}
    return c.post("/run-now/authorize", headers=AUTH, json=body).json()


@pytest.mark.parametrize(
    "case",
    [
        {"signature": ""},  # nothing signed
        {"token": "wrong"},  # signed with another secret
        {"token": TOKEN + "x"},
        {"signature": TOKEN},  # the token itself, sent in clear
    ],
)
def test_a_run_now_request_without_a_valid_signature_is_refused(gate, case):
    answer = ask(**case)
    assert answer["decision"] == "refuse" and answer["status"] == 401 and answer["outcome"] == "bad_token"
    assert sql(gate, "SELECT target, outcome, signature_sha256 FROM ap.run_requests") == [
        {"target": "posting", "outcome": "bad_token", "signature_sha256": None}
    ]
    stored = str(sql(gate, "SELECT * FROM ap.processing_log")) + str(sql(gate, "SELECT * FROM ap.run_requests"))
    assert TOKEN not in stored


def test_a_signature_is_valid_once_and_for_a_few_minutes(gate):
    ts = str(int(time.time()))
    sig = run_now.sign(TOKEN, ts, "posting")
    assert ask(timestamp=ts, signature=sig)["decision"] == "run"
    replayed = ask(timestamp=ts, signature=sig)  # a stored or captured request, sent again
    assert replayed["status"] == 401 and replayed["outcome"] == "replayed"
    old = str(int(time.time() - run_now.MAX_AGE.total_seconds() - 5))
    assert ask(timestamp=old)["outcome"] == "expired"  # correctly signed, but too old
    assert ask(timestamp="soon")["outcome"] == "expired"  # not a timestamp
    stored = sql(gate, "SELECT signature_sha256 FROM ap.run_requests WHERE outcome = 'run'")
    assert stored == [{"signature_sha256": hashlib.sha256(sig.encode()).hexdigest()}]  # a hash, not the signature


def test_a_signed_request_for_a_known_target_runs_within_the_rate_limit(gate):
    now = int(time.time())
    first, second = ask(timestamp=str(now - 2)), ask(timestamp=str(now - 1))  # two requests, two signatures
    assert first["decision"] == "run" and second["status"] == 202
    third = ask(timestamp=str(now))
    assert third["decision"] == "refuse" and third["status"] == 429 and third["retry_after"] == 60
    assert ask("bank")["decision"] == "run"  # the limit is per target
    assert ask("everything")["status"] == 400


def test_repeated_bad_signatures_lock_out_every_request_for_a_while(gate):
    for _ in range(run_now.MAX_REFUSED):
        assert ask(token="guess")["status"] == 401
    locked = ask()  # even a valid one: no guessing at speed
    assert locked["status"] == 429 and locked["retry_after"] == 600


def test_without_a_configured_token_every_request_is_refused(acfg):
    from app.config import get_settings

    get_settings.cache_clear()
    answer = ask(token="")
    assert answer["decision"] == "refuse" and answer["status"] == 503


@pytest.mark.parametrize(
    "given, expected, same", [("a", "a", True), ("a", "b", False), ("", "", False), ("a" * 99, "a", False)]
)
def test_secrets_are_compared_whatever_their_length(given, expected, same):
    assert run_now.same_secret(given, expected) is same
