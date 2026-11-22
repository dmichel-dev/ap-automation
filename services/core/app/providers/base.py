"""Extraction provider interface. Mistral OCR is the only provider for now; a fallback
provider can be added later by implementing the same interface."""

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class ProviderResult:
    annotation: dict[str, Any] | None      # structured data, parsed from the provider's JSON
    ocr_text: str                          # full text (markdown), all pages
    pages: int
    model: str | None = None
    ocr_confidence_avg: float | None = None
    ocr_confidence_min: float | None = None
    raw: dict[str, Any] = field(default_factory=dict)   # trimmed raw response, kept for audit
    annotation_error: str | None = None   # set when the annotation is missing or not valid JSON


class ProviderError(Exception):
    """kind: auth | rate_limit | bad_request | server | timeout | invalid_response"""

    def __init__(self, kind: str, message: str, retryable: bool):
        super().__init__(message)
        self.kind = kind
        self.retryable = retryable


class ExtractionProvider(Protocol):
    name: str

    def extract(self, content: bytes, mime_type: str) -> ProviderResult: ...
