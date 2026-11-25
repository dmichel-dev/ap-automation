"""The public demo's sample invoices: fictional documents a visitor can send with one click.

Each sample's OCR response was recorded once (scripts/demo_samples.py) and is stored next to it, so a
sample goes through the whole pipeline (normalization, checks, routing, bill preview) without an OCR
call: instant, and free. A file is a sample only if its bytes are exactly a sample's (SHA-256), so a
visitor's own file can never borrow a stored result.
"""

import hashlib
import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .providers.base import ProviderResult
from .providers.mistral import MistralOcrProvider

SAMPLES_DIR = Path(__file__).parent / "demo_samples"
PROVIDER_NAME = "mistral-ocr (stored sample result)"


@dataclass(frozen=True)
class Sample:
    key: str  # also the file name: <key>.pdf, and its recorded response: <key>.mistral.json
    title: str
    shows: str  # what the visitor will see happen

    @property
    def file(self) -> Path:
        return SAMPLES_DIR / f"{self.key}.pdf"

    @property
    def response(self) -> Path:
        return SAMPLES_DIR / f"{self.key}.mistral.json"


CATALOG: tuple[Sample, ...] = (
    Sample("fr-eur-discount", "French invoice in euros, with a discount", "Straight through: ready to post"),
    Sample("de-eur-wrong-total", "German invoice whose total doesn't add up", "Sent to review: totals mismatch"),
    Sample("es-eur-ambiguous-date", "Spanish invoice with an ambiguous date", "Sent to review, with a suggested date"),
    Sample("ar-aed-tax-invoice", "Arabic tax invoice (UAE, dirhams)", "Right-to-left text read and checked"),
    Sample("hi-inr-gst-invoice", "Hindi GST invoice (India, rupees)", "Devanagari text read and checked"),
    Sample("us-hosting-october", "Hosting invoice, October", "Ready; its bank account is remembered"),
    Sample(
        "us-hosting-november-other-account",
        "Hosting invoice, November: same supplier, another bank account",
        "Fraud alert if October was sent first",
    ),
)


def sample(key: str) -> Sample | None:
    return next((s for s in CATALOG if s.key == key), None)


@lru_cache(maxsize=1)
def _responses_by_hash() -> dict[str, Path]:
    found = {}
    for s in CATALOG:
        if s.file.is_file() and s.response.is_file():
            found[hashlib.sha256(s.file.read_bytes()).hexdigest()] = s.response
    return found


def stored_result(sha256: str) -> ProviderResult | None:
    """The recorded OCR result of the sample with these exact bytes, or None (not a sample)."""
    path = _responses_by_hash().get(sha256)
    if path is None:
        return None
    return MistralOcrProvider.parse_response(json.loads(path.read_text(encoding="utf-8")))
