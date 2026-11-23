"""Mistral OCR (Document AI) provider: POST /v1/ocr with a JSON-schema document annotation.

Verified against the live API on 2026-11-22:
  * documents are sent as base64 data URIs (document_url for PDF, image_url for images)
  * document_annotation is returned as a JSON string
  * page-level OCR confidence via confidence_scores_granularity = "page"
"""

import base64
import json
import logging
import time
from collections.abc import Callable
from typing import Any

import httpx

from ..models import ANNOTATION_PROMPT, ExtractionResult, strict_json_schema
from .base import ProviderError, ProviderResult

log = logging.getLogger(__name__)

API_URL = "https://api.mistral.ai/v1/ocr"
RETRY_STATUS = {429, 500, 502, 503, 504}


class MistralOcrProvider:
    name = "mistral-ocr"

    def __init__(
        self,
        api_key: str,
        model: str = "mistral-ocr-latest",
        max_pages: int = 10,
        timeout_s: float = 120.0,
        max_attempts: int = 3,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not api_key:
            raise ProviderError("auth", "MISTRAL_API_KEY is not set", retryable=False)
        self._api_key = api_key
        self.model = model
        self.max_pages = max_pages
        self.max_attempts = max_attempts
        self._client = client or httpx.Client(timeout=httpx.Timeout(timeout_s, connect=10.0))
        self._sleep = sleep
        self._schema = strict_json_schema(ExtractionResult)

    def build_request(self, content: bytes, mime_type: str) -> dict[str, Any]:
        data_uri = f"data:{mime_type};base64,{base64.b64encode(content).decode()}"
        if mime_type == "application/pdf":
            document = {"type": "document_url", "document_url": data_uri}
        else:
            document = {"type": "image_url", "image_url": data_uri}
        return {
            "model": self.model,
            "document": document,
            "pages": list(range(self.max_pages)) if mime_type == "application/pdf" else None,
            "include_image_base64": False,
            "confidence_scores_granularity": "page",
            "document_annotation_format": {
                "type": "json_schema",
                "json_schema": {"name": "invoice_extraction", "schema": self._schema, "strict": True},
            },
            "document_annotation_prompt": ANNOTATION_PROMPT,
        }

    def extract(self, content: bytes, mime_type: str) -> ProviderResult:
        body = self.build_request(content, mime_type)
        body = {k: v for k, v in body.items() if v is not None}
        response = self._post_with_retry(body)
        return self.parse_response(response)

    def _post_with_retry(self, body: dict[str, Any]) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self._api_key}"}
        for attempt in range(1, self.max_attempts + 1):
            try:
                r = self._client.post(API_URL, json=body, headers=headers)
            except httpx.TimeoutException as exc:
                error = ProviderError("timeout", f"Mistral OCR timed out: {exc}", retryable=True)
            except httpx.TransportError as exc:
                error = ProviderError("server", f"Mistral OCR unreachable: {exc}", retryable=True)
            else:
                if r.status_code == 200:
                    try:
                        payload: dict[str, Any] = r.json()
                        return payload
                    except ValueError as exc:
                        raise ProviderError("invalid_response", "Mistral OCR returned non-JSON", False) from exc
                error = self._http_error(r)
            if not error.retryable or attempt == self.max_attempts:
                raise error
            delay = min(2 ** (attempt - 1), 8)
            log.warning("Mistral OCR attempt %d failed (%s), retrying in %ss", attempt, error.kind, delay)
            self._sleep(delay)
        raise AssertionError("unreachable")

    @staticmethod
    def _http_error(r: httpx.Response) -> ProviderError:
        detail = r.text[:300]
        if r.status_code in (401, 403):
            return ProviderError("auth", f"Mistral OCR rejected the API key (HTTP {r.status_code})", False)
        if r.status_code == 429:
            return ProviderError("rate_limit", "Mistral OCR rate limit reached", True)
        if r.status_code in RETRY_STATUS:
            return ProviderError("server", f"Mistral OCR server error HTTP {r.status_code}", True)
        return ProviderError("bad_request", f"Mistral OCR HTTP {r.status_code}: {detail}", False)

    def parse_response(self, data: dict[str, Any]) -> ProviderResult:
        pages = data.get("pages")
        if not isinstance(pages, list):
            raise ProviderError("invalid_response", "Mistral OCR response has no pages", False)

        texts, avgs, mins = [], [], []
        for p in sorted(pages, key=lambda p: p.get("index", 0)):
            texts.append(p.get("markdown") or "")
            scores = p.get("confidence_scores") or {}
            if (a := scores.get("average_page_confidence_score")) is not None:
                avgs.append(float(a))
            if (m := scores.get("minimum_page_confidence_score")) is not None:
                mins.append(float(m))

        annotation, annotation_error = None, None
        raw_annotation = data.get("document_annotation")
        if raw_annotation is None:
            annotation_error = "no document annotation returned"
        else:
            try:
                annotation = json.loads(raw_annotation) if isinstance(raw_annotation, str) else raw_annotation
                if not isinstance(annotation, dict):
                    annotation, annotation_error = None, "document annotation is not a JSON object"
            except ValueError:
                annotation_error = "document annotation is not valid JSON"

        trimmed = {
            "model": data.get("model"),
            "usage_info": data.get("usage_info"),
            "document_annotation": raw_annotation,
            "pages": [
                {
                    "index": p.get("index"),
                    "confidence_scores": {
                        k: v for k, v in (p.get("confidence_scores") or {}).items() if k != "word_confidence_scores"
                    },
                }
                for p in pages
            ],
        }
        return ProviderResult(
            annotation=annotation,
            ocr_text="\n\n".join(texts).strip(),
            pages=len(pages),
            model=data.get("model"),
            ocr_confidence_avg=round(sum(avgs) / len(avgs), 4) if avgs else None,
            ocr_confidence_min=round(min(mins), 4) if mins else None,
            raw=trimmed,
            annotation_error=annotation_error,
        )
