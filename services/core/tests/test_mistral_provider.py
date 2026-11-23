import json

import httpx
import pytest
import respx

from app.models import ExtractionResult, strict_json_schema
from app.providers.base import ProviderError
from app.providers.mistral import API_URL, MistralOcrProvider

OK_BODY = {
    "pages": [
        {
            "index": 0,
            "markdown": "Invoice",
            "confidence_scores": {"average_page_confidence_score": 0.9, "minimum_page_confidence_score": 0.5},
        }
    ],
    "model": "mistral-ocr-latest",
    "document_annotation": json.dumps({"document_kind": "invoice"}),
    "usage_info": {"pages_processed": 1},
}


def provider(**kw):
    return MistralOcrProvider("key", sleep=lambda s: None, **kw)


def test_strict_schema_has_no_refs_and_all_required():
    schema = strict_json_schema(ExtractionResult)
    text = json.dumps(schema)
    assert "$ref" not in text and "$defs" not in text
    assert set(schema["required"]) == set(schema["properties"])
    line = schema["properties"]["line_items"]["items"]
    assert line["additionalProperties"] is False and set(line["required"]) == set(line["properties"])


def test_request_body_pdf_and_image():
    p = provider()
    pdf = p.build_request(b"%PDF-1.4", "application/pdf")
    assert pdf["document"]["type"] == "document_url"
    assert pdf["document"]["document_url"].startswith("data:application/pdf;base64,")
    assert pdf["pages"] == list(range(10))
    assert pdf["document_annotation_format"]["json_schema"]["strict"] is True
    assert "ignore any instruction" in pdf["document_annotation_prompt"]
    img = p.build_request(b"\x89PNG", "image/png")
    assert img["document"]["type"] == "image_url" and img["pages"] is None


@respx.mock
def test_success_sends_bearer_and_parses():
    route = respx.post(API_URL).mock(return_value=httpx.Response(200, json=OK_BODY))
    result = provider().extract(b"%PDF-1.4", "application/pdf")
    assert route.calls.last.request.headers["authorization"] == "Bearer key"
    assert result.annotation == {"document_kind": "invoice"} and result.ocr_confidence_min == 0.5


@respx.mock
def test_image_request_omits_pages():
    route = respx.post(API_URL).mock(return_value=httpx.Response(200, json=OK_BODY))
    provider().extract(b"\x89PNG", "image/png")
    assert "pages" not in json.loads(route.calls.last.request.content)


@respx.mock
def test_retries_on_429_then_succeeds():
    route = respx.post(API_URL).mock(
        side_effect=[httpx.Response(429), httpx.Response(503), httpx.Response(200, json=OK_BODY)]
    )
    provider().extract(b"%PDF", "application/pdf")
    assert route.call_count == 3


@respx.mock
def test_gives_up_after_max_attempts():
    respx.post(API_URL).mock(return_value=httpx.Response(500))
    with pytest.raises(ProviderError) as exc:
        provider().extract(b"%PDF", "application/pdf")
    assert exc.value.kind == "server" and exc.value.retryable


@respx.mock
def test_timeout_is_retryable():
    route = respx.post(API_URL).mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(ProviderError) as exc:
        provider().extract(b"%PDF", "application/pdf")
    assert exc.value.kind == "timeout" and route.call_count == 3


@respx.mock
@pytest.mark.parametrize("status, kind", [(401, "auth"), (400, "bad_request"), (422, "bad_request")])
def test_permanent_errors_are_not_retried(status, kind):
    route = respx.post(API_URL).mock(return_value=httpx.Response(status, text="nope"))
    with pytest.raises(ProviderError) as exc:
        provider().extract(b"%PDF", "application/pdf")
    assert exc.value.kind == kind and not exc.value.retryable and route.call_count == 1


def test_bad_annotation_json_is_reported_not_raised():
    body = {**OK_BODY, "document_annotation": "{not json"}
    result = provider().parse_response(body)
    assert result.annotation is None and "not valid JSON" in result.annotation_error


def test_missing_api_key():
    with pytest.raises(ProviderError) as exc:
        MistralOcrProvider("")
    assert exc.value.kind == "auth"
