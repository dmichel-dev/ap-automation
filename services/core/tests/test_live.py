"""Live smoke test against the real Mistral OCR API (fictional invoices only).
Skipped by default; costs a few cents:  docker compose run --rm core-test pytest -m live
"""

import io
import os
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFont

from app.extraction import build_common_invoice
from app.files import prepare
from app.providers.mistral import MistralOcrProvider

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not os.environ.get("MISTRAL_API_KEY"), reason="MISTRAL_API_KEY not set"),
]
FIXTURES = Path(__file__).parent / "fixtures"


def run(content: bytes):
    provider = MistralOcrProvider(os.environ["MISTRAL_API_KEY"])
    prepared = prepare(content)
    return build_common_invoice(provider.extract(prepared.content, prepared.mime_type), provider.name)


def test_live_pdf_us_invoice():
    inv = run((FIXTURES / "us_invoice_brightwater.pdf").read_bytes())
    print(inv.model_dump_json(indent=2, exclude={"ocr_text"}))
    assert inv.is_invoice and inv.document_type == "invoice"
    assert inv.invoice_number == "BW-1042" and inv.currency == "USD"
    assert inv.invoice_date == date(2026, 11, 18) and inv.due_date == date(2026, 12, 18)
    assert inv.total == Decimal("641.24") and inv.tax_total == Decimal("50.24")
    assert len(inv.lines) == 2 and inv.supplier.country == "US"
    assert inv.issues == []


def test_live_png_french_credit_note_with_ambiguous_date():
    img = Image.new("RGB", (1240, 1000), "white")
    draw = ImageDraw.Draw(img)
    font = ImageFont.load_default(size=30)   # built-in font has no € glyph, so amounts use 'EUR'
    lines = [
        "Atelier Fictif des Lilas SARL", "8 rue Imaginaire, 69000 Lyon, France", "TVA : FR00999999999", "",
        "AVOIR N° AV-2026-007", "Date : 03/04/2026", "Facture d'origine : F-2026-0311", "",
        "Remise commerciale : 100,00 EUR", "",
        "Total HT : 100,00 EUR", "TVA 20 % : 20,00 EUR", "Total TTC : 120,00 EUR",
    ]
    for i, line in enumerate(lines):
        draw.text((60, 60 + i * 60), line, fill="black", font=font)
    buf = io.BytesIO()
    img.save(buf, format="PNG")

    inv = run(buf.getvalue())
    print(inv.model_dump_json(indent=2, exclude={"ocr_text"}))
    assert inv.document_type == "credit_note"
    assert inv.total == Decimal("-120.00") and inv.currency == "EUR"
    assert inv.supplier.country == "FR" and inv.language == "fr"
    assert inv.invoice_date is None and inv.dates.ambiguous
    assert inv.dates.suggested_order == "DMY" and inv.dates.suggested_invoice_date == date(2026, 4, 3)
    assert inv.related_invoice_number == "F-2026-0311"
    assert [i.code for i in inv.issues] == ["ambiguous_date"]
