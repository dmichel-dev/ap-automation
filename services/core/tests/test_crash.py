"""Crash tests: the OCR service, the core and QuickBooks fail at the worst moments, then recover.
Whatever happens, a retry never creates a second invoice, bill or payment. Real Postgres; the OCR
service is a fake that fails on demand, and QuickBooks answers are passed in as the workflow would."""

import pytest
from app import accounting, db
from app.main import app
from app.providers.base import ProviderError
from fastapi.testclient import TestClient

from .conftest import AUTH, query
from .test_accounting import claim, due_payment, make_invoice, post_ok, posting, result
from .test_intake_failures import due, failure, report, time_passes
from .test_processing import FakeProvider, env, post  # noqa: F401  (env is a fixture)

c = TestClient(app)
TIMEOUT = ProviderError("timeout", "Mistral OCR timed out", retryable=True)


@pytest.fixture
def intake(env, cfg):  # noqa: F811  (env is the imported fixture)
    """Processing and the failure endpoints on the same test database."""
    state, pcfg = env
    return state, pcfg.database_url


def invoices(url):
    return query(url, "SELECT id, source_ref, status FROM ap.invoices ORDER BY created_at")


def process(source_ref="gmail:m1:0", run="1", content=b"%PDF-1.4 invoice A"):
    return post(content=content, source_ref=source_ref, message_id="m1", n8n_execution_id=run)


# =====================================================================================
# Intake: OCR service and core failures
# =====================================================================================
def test_ocr_service_down_then_back(intake):
    state, url = intake
    state["provider"] = FakeProvider(error=TIMEOUT)
    r = process(run="1")
    assert r.status_code == 503  # n8n labels AP/Error and reports what it saw
    assert report(status=r.status_code, error=r.json()["error"], run="1")["state"] == "waiting"
    assert invoices(url) == []

    time_passes(url)
    state["provider"] = FakeProvider()
    assert due("m1", run="2") == ["m1"]
    assert process(run="2").json()["status"] == "ready"
    assert failure(url)["state"] == "resolved"

    again = process(run="3")  # the same email once more (e.g. a run started twice)
    assert again.json()["already_processed"] is True
    assert len(invoices(url)) == 1 and state["provider"].calls == 1


def test_core_unreachable_when_the_email_failed(intake):
    """n8n labelled the email AP/Error but couldn't report it: the core has no row. The re-check finds
    it by its label and processes it on its first attempt."""
    _, url = intake
    assert query(url, "SELECT 1 FROM ap.intake_failures") == []
    assert due("m1", run="2") == ["m1"]
    assert process(run="2").json()["status"] == "ready"
    assert failure(url)["state"] == "resolved" and len(invoices(url)) == 1


def test_answer_lost_after_the_core_stored_the_invoice(intake):
    """The core committed, but n8n gave up waiting and reported no answer. The retry gets the stored result:
    no second invoice, and not a false 'duplicate file' rejection either."""
    state, url = intake
    assert process(run="1").json()["status"] == "ready"
    report(status=None, error=None, run="1")
    time_passes(url)
    assert due("m1", run="2") == ["m1"]
    r = process(run="2").json()
    assert (r["status"], r["already_processed"]) == ("ready", True)
    assert failure(url)["state"] == "resolved"
    assert len(invoices(url)) == 1 and state["provider"].calls == 1


def test_one_attachment_fails_the_other_works(intake):
    """Within one run, a failure wins over a success: the email stays for the re-check, which processes
    only what is missing."""
    state, url = intake
    assert process("gmail:m1:0", run="1").status_code == 200
    state["provider"] = FakeProvider(error=TIMEOUT)
    assert process("gmail:m1:1", run="1", content=b"%PDF-1.4 invoice B").status_code == 503
    report(run="1")
    state["provider"] = FakeProvider()
    process("gmail:m1:0", run="1")  # a late success in the same run doesn't close it
    assert failure(url)["state"] == "waiting"

    time_passes(url)
    assert due("m1", run="2") == ["m1"]
    assert process("gmail:m1:0", run="2").json()["already_processed"] is True
    assert process("gmail:m1:1", run="2", content=b"%PDF-1.4 invoice B").status_code == 200
    assert failure(url)["state"] == "resolved"
    assert [i["source_ref"] for i in invoices(url)] == ["gmail:m1:0", "gmail:m1:1"]


def test_a_file_the_ocr_service_refuses_goes_to_a_person(intake):
    state, url = intake
    state["provider"] = FakeProvider(error=ProviderError("bad_request", "HTTP 400: invalid document", retryable=False))
    r = process()
    assert r.status_code == 502
    assert report(status=502, error=r.json()["error"])["state"] == "given_up"
    time_passes(url)
    assert due("m1") == []
    summary = c.get("/intake/failures/summary", headers=AUTH).json()
    assert [g["code"] for g in summary["given_up"]] == ["provider_bad_request"]


def test_an_unsupported_file_is_an_answer_not_a_failure(intake):
    """A re-checked email whose file turns out unsupported (415, labelled rejected) is done."""
    _, url = intake
    report(status=None, error=None)
    time_passes(url)
    due("m1", run="2")
    assert post(content=b"MZ\x90\x00", message_id="m1", n8n_execution_id="2").status_code == 415
    assert failure(url)["state"] == "resolved"


# =====================================================================================
# QuickBooks: the call worked but its answer was lost
# =====================================================================================
def retry_due(url, table="accounting_postings"):
    query(url, f"UPDATE ap.{table} SET next_attempt_at = now()")


def test_bill_created_but_answer_lost_is_found_again_by_number(cfg):
    inv = make_invoice(cfg)
    first = claim(cfg)[0]
    # QuickBooks created the bill, then the connection dropped: the workflow saw no answer
    r = result(cfg, inv, outcome="failed", http_status=0, error_message="socket hang up")
    assert r.status_code == 200 and posting(cfg, inv)["status"] == "failed"
    assert claim(cfg) == []  # backoff: not at once
    retry_due(cfg.database_url)
    second = claim(cfg)[0]
    assert (second["request_id"], second["doc_number"]) == (first["request_id"], first["doc_number"])
    # the retry searches by number first, finds the bill and reuses it instead of creating one
    found = {"QueryResponse": {"Bill": [{"Id": "150", "VendorRef": {"value": "55"}}]}}
    body = {"status_code": 200, "body": found, "vendor_id": "55"}
    decision = TestClient(app).post(f"/postings/{inv}/lookup/document", headers=AUTH, json=body).json()
    assert (decision["decision"], decision["external_id"]) == ("exists", "150")
    post_ok(cfg, inv, ext="150")
    post_ok(cfg, inv, ext="150")  # the same answer twice: nothing changes
    assert result(cfg, inv, outcome="posted", external_id="151", vendor_external_id="55").status_code == 409
    assert len(query(cfg.database_url, "SELECT 1 FROM ap.accounting_postings")) == 1


def test_quickbooks_server_error_is_retried_then_stops_at_the_limit(cfg):
    inv = make_invoice(cfg)
    for _ in range(cfg.max_attempts):
        retry_due(cfg.database_url)
        assert [p["invoice_id"] for p in claim(cfg)] == [inv]
        result(cfg, inv, outcome="failed", http_status=503, error_message="Service Unavailable")
    retry_due(cfg.database_url)
    assert claim(cfg) == []  # attempts used up: a person looks (review item, "Try again")
    item = query(cfg.database_url, "SELECT reasons FROM ap.review_queue WHERE invoice_id = %s", inv)[0]
    assert item["reasons"][0]["code"] == "quickbooks_refused"


def test_payment_recorded_but_answer_lost_is_found_again_by_number(cfg):
    _, pay = due_payment(cfg)
    c.post(f"/matches/{pay['match_id']}/payment-failed", headers=AUTH, json={"http_status": 0})
    with db.connect(cfg.database_url) as conn:
        (again,) = accounting.payments_due(conn, cfg)
    assert (again["request_id"], again["doc_number"]) == (pay["request_id"], pay["doc_number"])
    found = {"QueryResponse": {"BillPayment": [{"Id": "88", "VendorRef": {"value": "55"}}]}}
    url = f"/matches/{pay['match_id']}/lookup"
    assert c.post(url, headers=AUTH, json={"status_code": 200, "body": found}).json()["decision"] == "exists"
    paid = f"/matches/{pay['match_id']}/payment"
    assert c.post(paid, headers=AUTH, json={"external_payment_id": "88"}).status_code == 200
    assert c.post(paid, headers=AUTH, json={"external_payment_id": "88"}).status_code == 200  # repeated: no-op
    assert c.post(paid, headers=AUTH, json={"external_payment_id": "89"}).status_code == 409
    with db.connect(cfg.database_url) as conn:
        assert accounting.payments_due(conn, cfg) == []
