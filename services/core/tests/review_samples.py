"""Five review items from five fictional suppliers, each with its own invoice file and an extraction
that matches it (name, number, amount, currency), so every item shows a believable reason for review.

Used by the review-page tests and by scripts/review_screenshots.sh (test database only). The files
come from test-data/generate.py; the provider below plays the OCR's part, and the real pipeline
(POST /invoices/process) decides the status and the reasons, the Tarnbrook item's possible bank-detail
change included.
"""

import json
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any, NamedTuple

from app.main import app, get_processing_config, get_provider
from app.normalize import normalize_tax_id
from app.processing import Config
from app.providers.base import ProviderResult
from fastapi.testclient import TestClient

from .conftest import AUTH, BANK_KEY, FIXTURES, base_annotation, known_supplier, query, rebuild_schema, seed_accounts

TEST_DATA = Path("/test-data/invoices")  # mounted read-only in the test container and in CI


def _us(supplier: tuple[str, str, str], number: str, dates: tuple[str, str], lines: list[tuple[str, str, str]]) -> dict:
    """A US invoice as the OCR returns it (8.5 % sales tax), from its printed values."""
    subtotal = sum(Decimal(q) * Decimal(p) for _, q, p in lines)
    tax = (subtotal * Decimal("0.085")).quantize(Decimal("0.01"))
    return base_annotation(
        supplier_name=supplier[0],
        supplier_tax_id=supplier[1],
        supplier_address=supplier[2],
        invoice_number=number,
        invoice_date=dates[0],
        due_date=dates[1],
        line_items=[
            {
                "kind": "item",
                "description": d,
                "quantity": q,
                "unit_price": f"${Decimal(p):,.2f}",
                "amount": f"${Decimal(q) * Decimal(p):,.2f}",
                "tax_rate": None,
            }
            for d, q, p in lines
        ],
        tax_lines=[
            {
                "tax_type": "sales_tax",
                "label": "Sales tax 8.5%",
                "rate": "8.5%",
                "taxable_base": f"${subtotal:,.2f}",
                "amount": f"${tax:,.2f}",
            }
        ],
        subtotal=f"${subtotal:,.2f}",
        tax_total=f"${tax:,.2f}",
        total=f"USD {subtotal + tax:,.2f}",
    )


NUBEAZUL = base_annotation(
    supplier_name="Estudio Nubeazul S.L.",
    supplier_tax_id="B00000000",
    supplier_address="Calle Inventada 7, 28001 Madrid, España",
    supplier_country_code="ES",
    invoice_number="2026/045",
    invoice_date="03/04/2026",  # day or month first: the reason for review
    due_date=None,
    currency="€",
    line_items=[
        {
            "kind": "item",
            "description": "Diseño de identidad visual",
            "quantity": "1",
            "unit_price": "1.200,00 €",
            "amount": "1.200,00 €",
            "tax_rate": None,
        },
        {
            "kind": "item",
            "description": "Maquetación de folleto (horas)",
            "quantity": "8",
            "unit_price": "45,00 €",
            "amount": "360,00 €",
            "tax_rate": None,
        },
    ],
    tax_lines=[
        {"tax_type": "vat", "label": "IVA 21 %", "rate": "21%", "taxable_base": "1.560,00 €", "amount": "327,60 €"},
        {
            "tax_type": "withholding",
            "label": "Retención IRPF 15 %",
            "rate": "15%",
            "taxable_base": "1.560,00 €",
            "amount": "234,00 €",
        },
    ],
    subtotal="1.560,00 €",
    tax_total="93,60 €",
    total="1.653,60 €",
    language="es",
)


class Sample(NamedTuple):
    key: str
    file: Path
    annotation: dict[str, Any]
    total: str  # as the page shows it
    ocr_average: float = 0.97
    known: bool = True  # the supplier already has a default expense account
    ocr_extra: str = ""  # more of the document's text, as the OCR reads it


SAMPLES = (
    Sample(
        "quillmoor",
        FIXTURES / "us_invoice_quillmoor.pdf",
        base_annotation(),
        "641.24 USD",
        ocr_average=0.81,  # a faint copy: below the review threshold
    ),
    Sample(
        "tarnbrook",
        FIXTURES / "tarnbrook_new_bank_account.pdf",
        _us(
            ("Tarnbrook Courier Services LLC", "00-0000002", "5 Placeholder Lane, Salem, OR 97301, USA"),
            "TB-5547",
            ("11/21/2026", "12/21/2026"),
            [("Same-day courier, flat monthly route", "1", "80.00"), ("Saturday deliveries", "4", "22.50")],
        )
        | {
            "bank_accounts": [
                {
                    "iban": None,
                    "bic": None,
                    "account_number": "0000-FAKE-0017",
                    "routing_code": "000000000",
                    "account_holder": "Tarnbrook Courier Services LLC",
                }
            ],
            "bank_details_change_notice": True,
        },
        "184.45 USD",
        ocr_extra="PAYMENT DETAILS HAVE CHANGED. We have moved banks: please pay this and all future invoices "
        "to our new account. Routing number: 000000000  Account number: 0000-FAKE-0017",
    ),
    Sample(
        "pellingham",
        TEST_DATA / "M-en-usd-possible-capex.pdf",
        _us(
            ("Pellingham Machine Works Inc.", "00-0000004", "1200 Sample Industrial Way, Medford, OR 97501, USA"),
            "PMW-8841",
            ("11/19/2026", "12/19/2026"),
            [("Laser engraver LX-40", "1", "4800.00"), ("Installation and training", "1", "350.00")],
        ),
        "5,587.75 USD",
    ),
    Sample("nubeazul", TEST_DATA / "D-es-eur-ambiguous-date.pdf", NUBEAZUL, "1,653.60 EUR"),
    Sample(
        "halvercombe",
        TEST_DATA / "N1-en-usd-new-supplier.pdf",
        _us(
            ("Halvercombe Bookkeeping LLC", "00-0000005", "18 Specimen Row, Eugene, OR 97401, USA"),
            "HB-2411",
            ("11/20/2026", "12/20/2026"),
            [
                ("Monthly bookkeeping, November 2026", "1", "850.00"),
                ("Payroll processing (12 employees)", "1", "180.00"),
            ],
        ),
        "1,117.55 USD",
        known=False,  # never seen before: onboarding review
    ),
)


class SampleProvider:
    """Plays the OCR: returns the sample's extraction for its file."""

    name = "sample"

    def __init__(self, sample: Sample):
        self.sample = sample

    def extract(self, content: bytes, mime_type: str) -> ProviderResult:
        return ProviderResult(
            annotation=self.sample.annotation,
            ocr_text=f"{self.sample.annotation['supplier_name']} ... {self.sample.annotation['total']} "
            + self.sample.ocr_extra,
            pages=1,
            model="sample-1",
            ocr_confidence_avg=self.sample.ocr_average,
            ocr_confidence_min=self.sample.ocr_average,
            raw={"model": "sample-1"},
        )


def supplier_key(sample: Sample) -> str:
    return str(normalize_tax_id(sample.annotation["supplier_tax_id"]))


@contextmanager
def _pipeline(url: str, storage: Path) -> Iterator[dict[str, Any]]:
    state: dict[str, Any] = {}
    cfg = Config(
        database_url=url,
        storage_dir=storage,
        retention_days=30,
        confidence_threshold=0.85,
        bank_fingerprint_key=BANK_KEY,
    )
    app.dependency_overrides[get_processing_config] = lambda: cfg
    app.dependency_overrides[get_provider] = lambda: state["provider"]
    try:
        yield state
    finally:
        app.dependency_overrides.pop(get_processing_config, None)
        app.dependency_overrides.pop(get_provider, None)


def seed(url: str, storage: Path) -> dict[str, int]:
    """Runs every sample through the pipeline; returns its review item's id, by sample key."""
    seed_accounts(url)
    query(url, "DELETE FROM ap.supplier_defaults")
    for s in SAMPLES:
        if s.known:
            known_supplier(url, supplier_key(s))
    ids: dict[str, int] = {}
    with _pipeline(url, storage) as state:
        for n, s in enumerate(SAMPLES):
            state["provider"] = SampleProvider(s)
            r = TestClient(app).post(
                "/invoices/process",
                headers=AUTH,
                files={"file": (s.file.name, s.file.read_bytes())},
                data={"channel": "email", "source_ref": f"gmail:sample{n}:a1", "received_at": "2026-11-22T10:00:00Z"},
            )
            r.raise_for_status()
            assert r.json()["status"] == "needs_review", (s.key, r.json())
            ids[s.key] = r.json()["review_id"]
    return ids


def main(storage: str) -> None:
    """Rebuilds the test database with the five samples and one fictional reviewer
    (rita@example.test, password from SAMPLE_PASSWORD), for screenshots."""
    url = os.environ["AP_TEST_DATABASE_URL"]
    from app import db
    from app.web import accounts

    rebuild_schema(url)
    ids = seed(url, Path(storage))
    with db.connect(url) as conn:
        accounts.add_user(conn, "rita@example.test", "Rita Reviewer", "reviewer", "live", os.environ["SAMPLE_PASSWORD"])
    print(json.dumps(ids))


if __name__ == "__main__":
    main(sys.argv[1])
