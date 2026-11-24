"""Integration tests: the full /invoices/process pipeline against a real Postgres (ap_test)."""

import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest
from app.main import app, get_processing_config, get_provider
from app.processing import Config
from app.providers.base import ProviderError, ProviderResult
from fastapi.testclient import TestClient

from .conftest import AUTH, BANK_KEY, OCR_TEXT, base_annotation, known_supplier, query


class FakeProvider:
    name = "fake"

    def __init__(self, error=None, ocr_text=OCR_TEXT, **overrides):
        self.error, self.ocr_text, self.overrides, self.calls = error, ocr_text, overrides, 0

    def extract(self, content, mime_type):
        self.calls += 1
        if self.error:
            raise self.error
        return ProviderResult(
            annotation=base_annotation(**self.overrides),
            ocr_text=self.ocr_text,
            pages=1,
            model="fake-1",
            ocr_confidence_avg=0.97,
            ocr_confidence_min=0.5,
            raw={"model": "fake-1"},
        )


@pytest.fixture
def env(clean_db, tmp_path):
    known_supplier(clean_db)  # the test supplier already has a default account (unless a test removes it)
    cfg = Config(
        database_url=clean_db,
        storage_dir=tmp_path,
        retention_days=30,
        confidence_threshold=0.85,
        bank_fingerprint_key=BANK_KEY,
    )
    state = {"provider": FakeProvider()}
    app.dependency_overrides[get_processing_config] = lambda: cfg
    app.dependency_overrides[get_provider] = lambda: state["provider"]
    yield state, cfg
    app.dependency_overrides.clear()


def post(content=b"%PDF-1.4 invoice A", source_ref="gmail:m1:a1", name="invoice.pdf", **form):
    """`form`: more fields, or other values (e.g. message_id, n8n_execution_id)."""
    return TestClient(app).post(
        "/invoices/process",
        headers=AUTH,
        files={"file": (name, content)},
        data={
            "channel": "email",
            "source_ref": source_ref,
            "n8n_execution_id": "42",
            "received_at": "2026-11-22T10:00:00Z",
            **form,
        },
    )


q = query


def test_ready_invoice_is_stored(env):
    _state, cfg = env
    r = post()
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ready" and body["review_id"] is None and not body["already_processed"]
    assert body["summary"]["total"] == "641.24"  # decimal as string, exact
    assert body["confidence"] == {"score": 0.97, "ocr": 0.97, "completeness": 1.0, "tax_mode": "exclusive"}

    inv = q(cfg.database_url, "SELECT * FROM ap.invoices")[0]
    assert inv["status"] == "ready" and inv["supplier_key"] == "000000000" and inv["n8n_execution_id"] == "42"
    assert inv["confidence_completeness"] == 1 and float(inv["confidence_threshold"]) == 0.85
    assert inv["file_delete_after"] == datetime(2026, 12, 22, 10, tzinfo=UTC)
    assert (cfg.storage_dir / inv["file_path"]).read_bytes() == b"%PDF-1.4 invoice A"
    assert inv["file_path"].startswith("2026/11/")
    assert len(q(cfg.database_url, "SELECT * FROM ap.invoice_lines")) == 2
    assert len(q(cfg.database_url, "SELECT * FROM ap.invoice_tax_lines")) == 1
    steps = [(r["step"], r["level"]) for r in q(cfg.database_url, "SELECT * FROM ap.processing_log ORDER BY id")]
    assert steps == [("intake", "info"), ("validate", "info")]


def test_log_contains_no_document_content(env):
    _state, cfg = env
    post()
    text = str(q(cfg.database_url, "SELECT message, details FROM ap.processing_log"))
    assert "Quillmoor" not in text and "641.24" not in text


def test_same_source_is_idempotent(env):
    state, cfg = env
    first = post().json()
    second = post().json()
    assert second["already_processed"] and second["invoice_id"] == first["invoice_id"]
    assert state["provider"].calls == 1
    assert len(q(cfg.database_url, "SELECT id FROM ap.invoices")) == 1


def test_exact_duplicate_file_is_rejected_without_extraction(env):
    state, cfg = env
    first = post(source_ref="gmail:m1:a1").json()
    second = post(source_ref="gmail:m2:a1").json()
    assert second["status"] == "rejected" and second["reasons"][0]["code"] == "duplicate_file"
    assert state["provider"].calls == 1
    row = q(cfg.database_url, "SELECT duplicate_of, file_path FROM ap.invoices WHERE id = %s", second["invoice_id"])[0]
    assert str(row["duplicate_of"]) == first["invoice_id"] and row["file_path"] is None


def test_same_number_different_file_goes_to_review(env):
    _state, cfg = env
    first = post(content=b"%PDF-1.4 original", source_ref="gmail:m1:a1").json()
    second = post(content=b"%PDF-1.4 resent, slightly different", source_ref="gmail:m2:a1").json()
    assert second["status"] == "needs_review"
    assert [r["code"] for r in second["reasons"]] == ["possible_duplicate"]
    review = q(cfg.database_url, "SELECT * FROM ap.review_queue")[0]
    assert review["id"] == second["review_id"] and review["status"] == "open"
    dup = q(cfg.database_url, "SELECT duplicate_of FROM ap.invoices WHERE id = %s", second["invoice_id"])[0]
    assert str(dup["duplicate_of"]) == first["invoice_id"]


def test_ambiguous_date_stores_suggestion(env):
    state, cfg = env
    state["provider"] = FakeProvider(invoice_date="03/04/2026", due_date="05/04/2026")
    body = post().json()
    assert body["status"] == "needs_review"
    assert body["suggested_dates"] == {
        "order": "MDY",
        "invoice_date": "2026-03-04",
        "due_date": "2026-05-04",
        "basis": "supplier country US writes month first",
    }
    row = q(cfg.database_url, "SELECT * FROM ap.invoices")[0]
    assert row["invoice_date"] is None and row["dates_ambiguous"]
    assert str(row["invoice_date_suggested"]) == "2026-03-04" and row["date_order_suggested"] == "MDY"
    assert row["invoice_date_raw"] == "03/04/2026"


def test_wrong_sign_is_stored_for_review(env):
    """Migration 003 lets a negative invoice total be stored, as long as it isn't 'ready'."""
    state, _cfg = env
    state["provider"] = FakeProvider(total="-641.24")
    body = post().json()
    assert body["status"] == "needs_review" and "wrong_sign" in [r["code"] for r in body["reasons"]]


def test_due_before_invoice_date_is_stored_for_review(env):
    state, _cfg = env
    state["provider"] = FakeProvider(invoice_date="2026-11-18", due_date="2026-10-01")
    body = post().json()
    assert body["status"] == "needs_review"


def test_not_invoice_is_rejected_and_file_not_kept(env):
    state, cfg = env
    state["provider"] = FakeProvider(document_kind="not_invoice", invoice_number=None, total=None)
    body = post().json()
    assert body["status"] == "rejected"
    row = q(cfg.database_url, "SELECT file_path, file_sha256 FROM ap.invoices")[0]
    assert row["file_path"] is None and row["file_sha256"]  # hash kept for duplicate detection
    assert list(Path(cfg.storage_dir).rglob("*.pdf")) == []


def test_adjustment_lines_are_stored_with_kind(env):
    state, cfg = env
    state["provider"] = FakeProvider(
        adjustments=[{"kind": "shipping", "label": "Shipping", "amount": "15.00", "position": "after_tax"}],
        total="USD 656.24",
        ocr_text=OCR_TEXT,
    )
    assert post().json()["status"] == "ready"
    rows = q(cfg.database_url, "SELECT kind, tax_stage, amount FROM ap.invoice_lines ORDER BY line_no")
    assert rows[-1] == {"kind": "shipping", "tax_stage": "after_tax", "amount": 15}


def test_provider_error_is_logged_and_returns_503(env):
    state, cfg = env
    state["provider"] = FakeProvider(error=ProviderError("rate_limit", "slow down", retryable=True))
    r = post()
    assert r.status_code == 503 and r.json()["retryable"] is True
    assert q(cfg.database_url, "SELECT id FROM ap.invoices") == []
    log = q(cfg.database_url, "SELECT step, level, source_ref, details FROM ap.processing_log ORDER BY id")[-1]
    assert log["step"] == "extract" and log["level"] == "error" and log["source_ref"] == "gmail:m1:a1"
    # retry after the outage succeeds
    state["provider"] = FakeProvider()
    assert post().json()["status"] == "ready"


def test_unsupported_file_is_logged(env):
    _state, cfg = env
    r = post(content=b"MZ\x90\x00 executable", name="invoice.pdf")
    assert r.status_code == 415
    log = q(cfg.database_url, "SELECT step, details FROM ap.processing_log")[0]
    assert log["step"] == "intake" and log["details"]["code"] == "unsupported_type"


def test_concurrent_duplicates_only_one_passes(env):
    """Two copies of the same invoice (different files) arriving at once: one must be flagged."""
    _state, _cfg = env
    results = []

    def send(i):
        results.append(post(content=f"%PDF-1.4 copy {i}".encode(), source_ref=f"gmail:c{i}").json()["status"])

    threads = [threading.Thread(target=send, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == ["needs_review", "ready"]
