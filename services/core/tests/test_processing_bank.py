"""The bank-details check in the full pipeline: what each invoice is compared with, and that full
bank details are stored nowhere except the document's own text (test database, fictional numbers)."""

import json

from app.main import app
from fastapi.testclient import TestClient

from .conftest import AUTH, OCR_TEXT, base_annotation, query
from .test_processing import FakeProvider, env  # noqa: F401  (fixture)

OLD, NEW = "GB00 FAKE 0000 0000 0000 21", "GB00 FAKE 9999 9999 9999 17"
FULL = ("GB00FAKE9999", "FAKE 9999", "99999999")  # never outside the document text


def iban(value, holder="Quillmoor Paper Supply LLC"):
    return {"iban": value, "bic": None, "account_number": None, "routing_code": None, "account_holder": holder}


class RawProvider(FakeProvider):
    """Also returns the annotation in the raw answer, as the real provider does."""

    def extract(self, content, mime_type):
        result = super().extract(content, mime_type)
        result.raw = {"model": "fake-1", "document_annotation": json.dumps(result.annotation)}
        return result


def send(state, n, accounts=(), email_text=None, ocr_text=OCR_TEXT, **overrides):
    # A new number and a month apart each time: not a possible duplicate of the one before
    overrides = {"invoice_number": f"QM-{1000 + n}", "invoice_date": f"{n:02d}/18/2026"} | overrides
    state["provider"] = RawProvider(ocr_text=ocr_text, bank_accounts=list(accounts), **overrides)
    data = {"channel": "email", "source_ref": f"gmail:m{n}:a1", "received_at": "2026-11-22T10:00:00Z"}
    if email_text is not None:
        data["email_text"] = email_text
    r = TestClient(app).post(
        "/invoices/process", headers=AUTH, files={"file": ("i.pdf", f"%PDF-1.4 invoice {n}".encode())}, data=data
    )
    assert r.status_code == 200, r.text
    return r.json()


def reason_codes(body):
    return [r["code"] for r in body["reasons"]]


def test_changed_details_are_flagged_and_never_join_the_baseline(env):  # noqa: F811
    state, _cfg = env
    first = send(state, 1, [iban(OLD)])
    assert first["status"] == "ready"  # known supplier, first bank details: information only
    assert send(state, 2, [iban(OLD)])["status"] == "ready"  # same details

    changed = send(state, 3, [iban(NEW)])
    assert changed["status"] == "needs_review" and reason_codes(changed) == ["possible_bank_detail_change"]
    assert "IBAN •••• 9917" in changed["reasons"][0]["message"]

    # The flagged invoice is not a reference: the new account is still new, the old one still known
    assert reason_codes(send(state, 4, [iban(NEW)])) == ["possible_bank_detail_change"]
    assert send(state, 5, [iban(OLD)])["status"] == "ready"


def test_a_verified_change_does_not_join_the_baseline_either(env):  # noqa: F811
    state, cfg = env
    send(state, 1, [iban(OLD)])
    flagged = send(state, 2, [iban(NEW)])
    query(cfg.database_url, "UPDATE ap.invoices SET status = 'ready' WHERE id = %s", flagged["invoice_id"])
    assert reason_codes(send(state, 3, [iban(NEW)])) == ["possible_bank_detail_change"]


def test_rejected_invoices_are_not_a_reference(env):  # noqa: F811
    state, cfg = env
    first = send(state, 1, [iban(OLD)])
    query(cfg.database_url, "UPDATE ap.invoices SET status = 'rejected' WHERE id = %s", first["invoice_id"])
    second = send(state, 2, [iban(NEW)])
    assert second["status"] == "ready"  # nothing to compare with: first seen
    info = query(cfg.database_url, "SELECT checks FROM ap.invoices WHERE id = %s", second["invoice_id"])[0]
    assert [i["code"] for i in info["checks"]["info"]] == ["bank_details_first_seen"]


def test_other_suppliers_are_not_a_reference(env):  # noqa: F811
    state, _cfg = env
    send(state, 1, [iban(OLD)], supplier_name="Fenwhistle Other LLC", supplier_tax_id="00-0000009")
    assert send(state, 2, [iban(NEW)])["status"] == "ready"


def test_a_change_announced_in_the_email_body(env):  # noqa: F811
    state, cfg = env
    body = send(state, 1, email_text="URGENT: new bank details\n\nPlease update our bank details. Ref ZQX-7731")
    assert body["status"] == "needs_review" and reason_codes(body) == ["possible_bank_detail_change"]
    # The email text is checked in memory, never stored or logged
    dump = json.dumps(query(cfg.database_url, "SELECT * FROM ap.invoices"), default=str)
    dump += json.dumps(query(cfg.database_url, "SELECT * FROM ap.processing_log"), default=str)
    assert "ZQX-7731" not in dump
    log = query(cfg.database_url, "SELECT details FROM ap.processing_log WHERE step = 'validate'")[0]["details"]
    assert log["bank"] == {"accounts": 0, "notice": ["email:en"], "new": 0, "first_seen": False}


def test_a_bank_change_letter_opens_a_fraud_review_and_keeps_the_file(env):  # noqa: F811
    state, cfg = env
    letter = "Change of bank details. Please update our payment details: IBAN GB00 FAKE 9999 9999 9999 17"
    body = send(
        state,
        1,
        [iban(NEW)],
        ocr_text=letter,
        bank_details_change_notice=True,
        document_kind="not_invoice",
        invoice_number=None,
        total=None,
    )
    assert body["status"] == "needs_review" and reason_codes(body) == ["possible_bank_detail_change"]
    inv = query(cfg.database_url, "SELECT file_path, status FROM ap.invoices")[0]
    assert inv["status"] == "needs_review" and inv["file_path"]
    assert query(cfg.database_url, "SELECT count(*) AS n FROM ap.review_queue WHERE status = 'open'")[0]["n"] == 1


def test_full_bank_details_are_stored_only_in_the_document_text(env):  # noqa: F811
    state, cfg = env
    send(state, 1, [iban(OLD)])
    text = OCR_TEXT + "\nPAYMENT DETAILS HAVE CHANGED. Fictional Savings Bank, IBAN GB00 FAKE 9999 9999 9999 17"
    body = send(state, 2, [iban(NEW)], ocr_text=text)
    assert body["status"] == "needs_review"

    url = cfg.database_url
    rows = query(url, "SELECT * FROM ap.invoices ORDER BY created_at")
    assert "FAKE 9999" in rows[1]["ocr_text"]  # the document's own text, as received
    stored = json.dumps([{k: v for k, v in r.items() if k != "ocr_text"} for r in rows], default=str)
    for table in ("processing_log", "review_queue", "invoice_lines"):
        stored += json.dumps(query(url, f"SELECT * FROM ap.{table}"), default=str)
    stored += json.dumps(body)
    for fragment in FULL:
        assert fragment not in stored, fragment
    accounts = rows[1]["bank_accounts"]
    assert accounts[0]["iban"] == "•••• 9917" and accounts[0]["label"] == "IBAN •••• 9917"
    assert accounts[0]["account_holder"] == "Quillmoor Paper Supply LLC" and len(accounts[0]["fingerprint"]) == 32


def test_the_api_answer_holds_no_bank_numbers(env):  # noqa: F811
    state, _cfg = env
    body = send(state, 1, [iban(NEW)])
    assert "9999" not in json.dumps(body).replace("•••• 9917", "")


def test_the_annotation_in_the_raw_answer_is_unchanged_apart_from_masking(env):  # noqa: F811
    state, cfg = env
    send(state, 1, [iban(NEW)])
    raw = query(cfg.database_url, "SELECT extraction FROM ap.invoices")[0]["extraction"]
    annotation = json.loads(raw["document_annotation"])
    assert annotation["bank_accounts"][0]["iban"] == "•••• 9917"
    assert annotation == base_annotation(
        invoice_number="QM-1001", invoice_date="01/18/2026", bank_accounts=[iban("•••• 9917")]
    )
