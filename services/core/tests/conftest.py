import os
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row

MIGRATIONS = Path("/migrations")
TABLES = (
    "matches, bank_transactions, bank_sync_state, accounting_postings, vendor_links, "
    "invoice_lines, invoice_tax_lines, review_queue, processing_log, invoices"
)


# --- shared by several test files -------------------------------------------------------
AUTH = {"Authorization": "Bearer test-token"}
OCR_TEXT = "Quillmoor Paper Supply LLC ... Total due: USD 641.24 (all amounts in $)"
FIXTURES = Path(__file__).parent / "fixtures"


def query(url: str, sql: str, *args: Any) -> list[dict[str, Any]]:
    """Runs one statement on the test database; the rows it returns (none for an UPDATE)."""
    with psycopg.connect(url, row_factory=dict_row) as conn:
        cur = conn.execute(sql, args)
        return cur.fetchall() if cur.description else []


def base_annotation(**overrides):
    """A complete, consistent provider annotation (fictional US supplier)."""
    base = {
        "document_kind": "invoice",
        "supplier_name": "Quillmoor Paper Supply LLC",
        "supplier_tax_id": "00-0000000",
        "supplier_address": "412 Fictional Ave, Springfield, OR 97477, USA",
        "supplier_country_code": "US",
        "buyer_name": "Example Buyer Inc.",
        "buyer_tax_id": None,
        "invoice_number": "QM-1042",
        "related_invoice_number": None,
        "invoice_date": "11/18/2026",
        "due_date": "12/18/2026",
        "currency": "$",
        "line_items": [
            {
                "kind": "item",
                "description": "Copy paper",
                "quantity": "10",
                "unit_price": "$42.00",
                "amount": "$420.00",
                "tax_rate": None,
            },
            {
                "kind": "item",
                "description": "Toner",
                "quantity": "2",
                "unit_price": "$85.50",
                "amount": "$171.00",
                "tax_rate": None,
            },
        ],
        "adjustments": [],
        "tax_lines": [
            {
                "tax_type": "sales_tax",
                "label": "Sales tax 8.5%",
                "rate": "8.5%",
                "taxable_base": "$591.00",
                "amount": "$50.24",
            }
        ],
        "subtotal": "$591.00",
        "tax_total": "$50.24",
        "total": "USD 641.24",
        "language": "en",
    }
    base.update(overrides)
    return base


@pytest.fixture(scope="session")
def test_db_url():
    url = os.environ.get("AP_TEST_DATABASE_URL")
    if not url:
        pytest.skip("AP_TEST_DATABASE_URL not set")
    # Rebuild the ap schema from the migrations, once per test session
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS ap CASCADE")
        conn.execute("CREATE SCHEMA ap")
        for f in sorted(MIGRATIONS.glob("*.sql")):
            conn.execute(f.read_text())
    return url


@pytest.fixture
def clean_db(test_db_url):
    """Empty tables before each test (the append-only trigger is disabled just for this)."""
    with psycopg.connect(test_db_url, autocommit=True) as conn:
        conn.execute("ALTER TABLE ap.processing_log DISABLE TRIGGER USER")
        conn.execute(f"TRUNCATE {', '.join('ap.' + t.strip() for t in TABLES.split(','))} RESTART IDENTITY")
        conn.execute("ALTER TABLE ap.processing_log ENABLE TRIGGER USER")
    return test_db_url
