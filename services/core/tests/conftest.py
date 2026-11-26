import dataclasses
import os
from pathlib import Path
from typing import Any

import psycopg
import pytest
from app import accounting, demo
from app.main import app, get_accounting_config
from psycopg.rows import dict_row

MIGRATIONS = Path("/migrations")
TABLES = (
    "demo_deletions, demo_external_items, demo_uploads, demo_bill_previews, demo_senders, demo_budget, demo_starts, "
    "demo_sessions, "
    "e2e_qbo_objects, e2e_runs, intake_failures, intake_leases, write_offs, run_requests, matches, bank_transactions, "
    "bank_sync_state, accounting_postings, fx_rates, fx_unsupported, "
    "vendor_links, supplier_defaults, "
    "qbo_accounts, qbo_vendors, login_attempts, review_sessions, review_users, "
    "invoice_lines, invoice_tax_lines, review_queue, processing_log, invoices"
)


# --- shared by several test files -------------------------------------------------------
AUTH = {"Authorization": "Bearer test-token"}
AUTOMATION = "e2e-test (automated test)"  # how the bearer-token API records who acted (actors.py)
OCR_TEXT = "Quillmoor Paper Supply LLC ... Total due: USD 641.24 (all amounts in $)"
FIXTURES = Path(__file__).parent / "fixtures"
BANK_KEY = b"test-bank-fingerprint-key"  # keys the bank-account fingerprints on the test database
WRITE_OFF_ACCOUNT = "1150040015"  # "Payment differences" in the sandbox


def query(url: str, sql: str, *args: Any) -> list[dict[str, Any]]:
    """Runs one statement on the test database; the rows it returns (none for an UPDATE)."""
    with psycopg.connect(url, row_factory=dict_row) as conn:
        cur = conn.execute(sql, args)
        return cur.fetchall() if cur.description else []


# A few accounts as the sync workflow stores them (sandbox IDs and names)
ACCOUNTS = (
    ("20", "Supplies", "Expense"),
    ("7", "Advertising", "Expense"),
    ("40", "Machinery & Equipment", "Fixed Asset"),
    ("1", "Services", "Income"),  # not an account an invoice can be coded to
)
QUILLMOOR_KEY = "000000000"  # supplier_key of base_annotation's supplier (its tax ID, normalized)


def set_scope(url: str, invoice_id: Any, scope: str) -> None:
    """Moves an invoice and its review items to another scope. A demo invoice gets a session of its
    own (every demo invoice belongs to one)."""
    session = None
    if scope == "demo":
        [row] = query(
            url,
            """INSERT INTO ap.demo_sessions (code, token_digest, client_digest, expires_at)
               VALUES (%s, %s, %s, now() + interval '1 hour') RETURNING id""",
            demo.new_code(),
            os.urandom(32),
            "0" * 64,
        )
        session = row["id"]
    query(url, "UPDATE ap.invoices SET scope = %s, demo_session_id = %s WHERE id = %s", scope, session, invoice_id)
    query(url, "UPDATE ap.review_queue SET scope = %s WHERE invoice_id = %s", scope, invoice_id)


def seed_accounts(url: str) -> None:
    for account_id, name, kind in ACCOUNTS:
        query(
            url,
            """INSERT INTO ap.qbo_accounts (external_id, name, fully_qualified_name, account_type, active)
               VALUES (%s, %s, %s, %s, true) ON CONFLICT DO NOTHING""",
            account_id,
            name,
            name,
            kind,
        )


def known_supplier(url: str, key: str = QUILLMOOR_KEY, account: str = "20") -> None:
    """A supplier whose default account was already chosen (what the e2e test setup does too)."""
    seed_accounts(url)
    query(
        url,
        """INSERT INTO ap.supplier_defaults (supplier_key, expense_account_id, source, set_by)
           VALUES (%s, %s, 'test_setup', 'tests')""",
        key,
        account,
    )


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


def rebuild_schema(url: str) -> None:
    """Drops the ap schema and builds it again from the migrations (test database only)."""
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS ap CASCADE")
        conn.execute("CREATE SCHEMA ap")
        for f in sorted(MIGRATIONS.glob("*.sql")):
            conn.execute(f.read_text())


@pytest.fixture(scope="session")
def test_db_url():
    url = os.environ.get("AP_TEST_DATABASE_URL")
    if not url:
        pytest.skip("AP_TEST_DATABASE_URL not set")
    rebuild_schema(url)  # once per test session
    return url


@pytest.fixture
def clean_db(test_db_url):
    """Empty tables before each test (the append-only trigger is disabled just for this)."""
    with psycopg.connect(test_db_url, autocommit=True) as conn:
        conn.execute("ALTER TABLE ap.processing_log DISABLE TRIGGER USER")
        conn.execute(f"TRUNCATE {', '.join('ap.' + t.strip() for t in TABLES.split(','))} RESTART IDENTITY")
        conn.execute("ALTER TABLE ap.processing_log ENABLE TRIGGER USER")
    return test_db_url


@pytest.fixture
def cfg(clean_db, tmp_path):
    """Accounting settings on the test database (sandbox account IDs), also used by the API."""
    c = accounting.AccountingConfig(
        database_url=clean_db,
        storage_dir=tmp_path,
        home_currency="USD",
        withholding_account_id="91",
        bank_account_id="35",
        max_attempts=3,
        lease_minutes=10,
    )
    app.dependency_overrides[get_accounting_config] = lambda: c
    yield c
    app.dependency_overrides.clear()


@pytest.fixture
def acfg(cfg):
    """Accounting settings with the payment-differences account, also used by the API."""
    config = dataclasses.replace(cfg, write_off_account_id=WRITE_OFF_ACCOUNT)
    app.dependency_overrides[get_accounting_config] = lambda: config
    return config
