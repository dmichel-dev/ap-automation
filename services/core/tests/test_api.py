import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import files
from app.main import app, get_provider
from app.providers.base import ProviderError, ProviderResult

AUTH = {"Authorization": "Bearer test-token"}


class FakeProvider:
    name = "fake"

    def __init__(self, error: ProviderError | None = None):
        self.error = error
        self.calls = []

    def extract(self, content, mime_type):
        self.calls.append(mime_type)
        if self.error:
            raise self.error
        return ProviderResult(annotation={"document_kind": "not_invoice", **{k: None for k in (
            "supplier_name supplier_tax_id supplier_address supplier_country_code buyer_name buyer_tax_id "
            "invoice_number related_invoice_number invoice_date due_date currency subtotal tax_total total language"
        ).split()}, "line_items": [], "tax_lines": []}, ocr_text="Dear customer", pages=1)


@pytest.fixture
def fake():
    provider = FakeProvider()
    app.dependency_overrides[get_provider] = lambda: provider
    yield provider
    app.dependency_overrides.clear()


client = TestClient(app)


def test_extract_requires_token(fake):
    assert client.post("/extract", files={"file": ("a.pdf", b"%PDF-1.4")}).status_code == 401
    r = client.post("/extract", files={"file": ("a.pdf", b"%PDF-1.4")}, headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401 and fake.calls == []


def test_extract_returns_common_format(fake):
    r = client.post("/extract", files={"file": ("a.pdf", b"%PDF-1.4 ...")}, headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["is_invoice"] is False and body["issues"][0]["code"] == "not_invoice"
    assert fake.calls == ["application/pdf"]


def test_file_type_comes_from_content_not_name(fake):
    r = client.post("/extract", files={"file": ("invoice.pdf", b"MZ\x90\x00 not a pdf", "application/pdf")}, headers=AUTH)
    assert r.status_code == 415 and r.json()["error"] == "unsupported_type" and fake.calls == []


def test_too_large(fake, monkeypatch):
    monkeypatch.setattr(files, "MAX_FILE_BYTES", 10)
    r = client.post("/extract", files={"file": ("a.pdf", b"%PDF-" + b"x" * 20)}, headers=AUTH)
    assert r.status_code == 413


def test_tiff_is_converted_to_pdf(fake):
    buf = io.BytesIO()
    Image.new("RGB", (40, 40), "white").save(buf, format="TIFF")
    r = client.post("/extract", files={"file": ("scan.tif", buf.getvalue())}, headers=AUTH)
    assert r.status_code == 200 and fake.calls == ["application/pdf"]


@pytest.mark.parametrize("retryable, status", [(True, 503), (False, 502)])
def test_provider_errors_map_to_http(retryable, status):
    app.dependency_overrides[get_provider] = lambda: FakeProvider(ProviderError("server", "down", retryable))
    try:
        r = client.post("/extract", files={"file": ("a.pdf", b"%PDF-1.4")}, headers=AUTH)
    finally:
        app.dependency_overrides.clear()
    assert r.status_code == status and r.json()["retryable"] is retryable
