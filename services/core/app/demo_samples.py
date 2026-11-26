"""The public demo's sample invoices: fictional documents a visitor can send with one click.

Each sample's OCR response was recorded once (scripts/demo_samples.py) and is stored next to it, so a
sample goes through the whole pipeline (normalization, checks, routing, bill preview) without an OCR
call: instant, and free. A file is a sample only if its bytes are exactly a sample's (SHA-256), so a
visitor's own file can never borrow a stored result.

The demo company already knows the samples' suppliers, each with a default expense account, so
every sample shows the same outcome every time and the clean ones go straight through. It knows
nothing else: live suppliers, their default accounts and vendor links never decide a demo outcome
(a visitor's own invoice is a new supplier, as it would be for a real company).
"""

import hashlib
import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from . import db, qbo_reference
from .extraction import build_common_invoice
from .models import CommonInvoice
from .providers.base import ProviderResult
from .providers.mistral import MistralOcrProvider
from .validation import SupplierStatus

SAMPLES_DIR = Path(__file__).parent / "demo_samples"
PROVIDER_NAME = "mistral-ocr (stored sample result)"


@dataclass(frozen=True)
class Sample:
    key: str  # also the file name: <key>.pdf, and its recorded response: <key>.mistral.json
    title: str
    shows: str  # what the visitor will see happen, in the review page's words (tests/test_demo.py)
    account: str  # its supplier's default expense account: a name the chart of accounts script creates

    @property
    def file(self) -> Path:
        return SAMPLES_DIR / f"{self.key}.pdf"

    @property
    def response(self) -> Path:
        return SAMPLES_DIR / f"{self.key}.mistral.json"


CATALOG: tuple[Sample, ...] = (
    Sample(
        "fr-eur-discount",
        "French invoice in euros, with a discount",
        "Straight through: ready to post",
        "Office Supplies",
    ),
    Sample(
        "de-eur-wrong-total",
        "German invoice whose total doesn't add up",
        "Review: total doesn't add up",
        "Cost of Goods Sold:Materials",
    ),
    Sample(
        "es-eur-ambiguous-date",
        "Spanish invoice with an ambiguous date",
        "Review: date reads two ways, with a suggested date",
        "Advertising",
    ),
    Sample(
        "ar-aed-tax-invoice",
        "Arabic tax invoice (UAE, dirhams)",
        "Right-to-left text read and checked: ready to post",
        "Office Supplies",
    ),
    Sample(
        "hi-inr-gst-invoice",
        "Hindi GST invoice (India, rupees)",
        "Devanagari text read and checked: ready to post",
        "Office Supplies",
    ),
    Sample(
        "us-hosting-october",
        "Hosting invoice, October",
        "Ready to post; its bank account is remembered",
        "Hosting & Cloud Services",
    ),
    Sample(
        "us-hosting-november-other-account",
        "Hosting invoice, November: same supplier, another bank account",
        "Review: bank details changed? (sent after October)",
        "Hosting & Cloud Services",
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


def _parse(response: Path) -> ProviderResult:
    return MistralOcrProvider.parse_response(json.loads(response.read_text(encoding="utf-8")))


def stored_result(sha256: str) -> ProviderResult | None:
    """The recorded OCR result of the sample with these exact bytes, or None (not a sample)."""
    path = _responses_by_hash().get(sha256)
    return _parse(path) if path else None


@lru_cache(maxsize=1)
def supplier_accounts() -> dict[str, str]:
    """supplier_key → default account name, for the suppliers of the samples. The keys come from the
    samples' recorded results, so they are exactly the keys processing computes."""
    found: dict[str, str] = {}
    for s in CATALOG:
        key = db.supplier_key(build_common_invoice(_parse(s.response), PROVIDER_NAME))
        if key is None:
            raise ValueError(f"sample {s.key}: no supplier key")
        if found.setdefault(key, s.account) != s.account:
            raise ValueError(f"sample {s.key}: its supplier already has the account {found[key]}")
    return found


def supplier_status(conn: db.Conn, inv: CommonInvoice) -> SupplierStatus:
    """The demo's counterpart of db.supplier_status: a sample's supplier is known, with its default
    account found by name in the QuickBooks copy (none if that account is missing or ambiguous there:
    the invoice then goes to review, like any supplier without a usable default)."""
    key = db.supplier_key(inv)
    name = supplier_accounts().get(key) if key else None
    if name is None:
        return SupplierStatus(known=False)
    account = qbo_reference.coding_account_named(conn, name)
    return SupplierStatus(True, "demo_sample_supplier", account["id"] if account else None)
