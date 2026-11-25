"""Persistence in the ap schema. Plain SQL with psycopg 3."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .bank_details import KnownAccount
from .models import CommonInvoice, Issue
from .validation import DUPLICATE_DATE_WINDOW_DAYS, Duplicate, SupplierStatus, Validation

Row = dict[str, Any]
Conn = psycopg.Connection[Row]  # every connection returns rows as dicts
# Whose data a row is: live, the daily end-to-end test, or the public demo. Every comparison with
# earlier documents (same file, duplicates, bank details) stays within one scope, and for the demo
# within one session (demo_session_id; NULL for the other scopes).
Scope = Literal["live", "e2e", "demo"]
SAME_SESSION = "demo_session_id IS NOT DISTINCT FROM %(session)s::uuid"


def jsonable(value: Any) -> Any:
    """DB values → JSON-safe (Decimal as an exact string, dates as ISO 8601), recursively."""
    if isinstance(value, dict):
        return {k: jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    if isinstance(value, (Decimal, UUID)):
        return str(value)
    return value.isoformat() if isinstance(value, (date, datetime)) else value


def connect(url: str) -> Conn:
    return psycopg.Connection.connect(url, row_factory=dict_row, options="-c search_path=ap", connect_timeout=5)


def one(conn: Conn, query: str, params: Any = None) -> Row:
    """A query that always returns a row (INSERT … RETURNING, aggregates)."""
    row = conn.execute(query, params).fetchone()
    if row is None:
        raise LookupError("query returned no row")
    return row


def lock(conn: Conn, *keys: str) -> None:
    """Transaction-level advisory locks, taken in a fixed order (no deadlocks).
    Serializes concurrent processing of the same source, file or supplier+number."""
    for key in sorted(keys):
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (key,))


def find_by_source(conn: Conn, channel: str, source_ref: str) -> dict[str, Any] | None:
    return conn.execute(
        """SELECT i.id, i.status, i.status_reasons, r.id AS review_id
             FROM invoices i LEFT JOIN review_queue r ON r.invoice_id = i.id AND r.status = 'open'
            WHERE i.channel = %s AND i.source_ref = %s""",
        (channel, source_ref),
    ).fetchone()


def find_same_file(conn: Conn, sha256: str, scope: Scope = "live", session: str | None = None) -> dict[str, Any] | None:
    """Earliest earlier document of the scope (and demo session) with the same bytes (exact duplicate file)."""
    return conn.execute(
        "SELECT id, status FROM invoices WHERE file_sha256 = %(sha)s AND scope = %(scope)s AND "
        + SAME_SESSION
        + " ORDER BY created_at LIMIT 1",
        {"sha": sha256, "scope": scope, "session": session},
    ).fetchone()


def supplier_key(inv: CommonInvoice) -> str | None:
    # Must match the generated column invoices.supplier_key
    return inv.normalized.supplier_tax_id or inv.normalized.supplier_name


def find_possible_duplicates(
    conn: Conn, inv: CommonInvoice, exclude_id: str | None = None, scope: Scope = "live", session: str | None = None
) -> list[Duplicate]:
    """Earlier documents of the same scope (and demo session), supplier and type (rejected ones don't count) with
    - the same number (same_number), or
    - the same total and currency, dated within DUPLICATE_DATE_WINDOW_DAYS, and a different number
      (same_amount_near_date: the same bill sent again with a new number, or a typo in the number)."""
    key, number = supplier_key(inv), inv.normalized.invoice_number
    if not key or not inv.document_type:
        return []
    rows = conn.execute(
        """SELECT id, status,
                  CASE WHEN invoice_number_norm = %(number)s THEN 'same_number' ELSE 'same_amount_near_date' END AS rule
             FROM invoices
            WHERE supplier_key = %(key)s AND document_type = %(type)s AND status <> 'rejected'
              AND scope = %(scope)s AND """
        + SAME_SESSION
        + """ AND id IS DISTINCT FROM %(exclude)s::uuid
              AND (invoice_number_norm = %(number)s
                   OR (%(total)s::numeric IS NOT NULL AND total = %(total)s AND currency = %(currency)s
                       AND invoice_date BETWEEN %(day)s::date - %(days)s AND %(day)s::date + %(days)s
                       AND invoice_number_norm IS DISTINCT FROM %(number)s))
            ORDER BY created_at""",
        {
            "key": key,
            "type": inv.document_type,
            "number": number,
            "total": inv.total,
            "currency": inv.currency,
            "day": inv.invoice_date,
            "days": DUPLICATE_DATE_WINDOW_DAYS,
            "exclude": exclude_id,
            "scope": scope,
            "session": session,
        },
    ).fetchall()
    return [Duplicate(str(r["id"]), r["status"], r["rule"]) for r in rows]


def supplier_status(conn: Conn, inv: CommonInvoice) -> SupplierStatus:
    """Known supplier (default account, vendor link, or one active accounting vendor with the same
    normalized name) and its default account. Two vendors with the same name: not known (no guess)."""
    key = supplier_key(inv)
    if not key:
        return SupplierStatus(known=False)
    default = conn.execute(
        "SELECT expense_account_id FROM supplier_defaults WHERE supplier_key = %s", (key,)
    ).fetchone()
    account = default["expense_account_id"] if default else None
    if account:
        return SupplierStatus(True, "supplier_default", account)
    if conn.execute("SELECT 1 FROM vendor_links WHERE supplier_key = %s LIMIT 1", (key,)).fetchone():
        return SupplierStatus(True, "vendor_link")
    name = inv.normalized.supplier_name
    same_name = one(conn, "SELECT count(*) AS n FROM qbo_vendors WHERE active AND name_norm = %s", (name,))["n"]
    return SupplierStatus(True, "accounting_vendor_name") if name and same_name == 1 else SupplierStatus(False)


def bank_baseline(
    conn: Conn, inv: CommonInvoice, scope: Scope = "live", session: str | None = None
) -> list[KnownAccount]:
    """The accounts on this supplier's earlier invoices of the scope (and demo session), for the
    bank-details comparison. Not counted:
    rejected invoices, and invoices that were flagged as a possible change themselves (even if a
    reviewer later verified them: verified details are not a trusted record)."""
    key = supplier_key(inv)
    if not key:
        return []
    rows = conn.execute(
        """SELECT a->>'fingerprint' AS fingerprint, min(a->>'label') AS label
             FROM invoices i, jsonb_array_elements(i.bank_accounts) a
            WHERE i.supplier_key = %(key)s AND i.scope = %(scope)s AND i."""
        + SAME_SESSION
        + """
              AND i.status <> 'rejected' AND a->>'fingerprint' IS NOT NULL
              AND NOT i.status_reasons @> '[{"code": "possible_bank_detail_change"}]'
            GROUP BY 1 ORDER BY min(i.created_at)""",
        {"key": key, "scope": scope, "session": session},
    ).fetchall()
    return [KnownAccount(r["fingerprint"], r["label"]) for r in rows]


def _reasons(items: list[Issue]) -> Jsonb:
    return Jsonb([i.model_dump() for i in items])


def insert_invoice(
    conn: Conn,
    *,
    invoice_id: str,
    channel: str,
    source_ref: str,
    received_at: datetime,
    file_name: str,
    mime_type: str,
    size: int,
    sha256: str,
    file_path: str | None,
    file_delete_after: datetime | None,
    inv: CommonInvoice | None,
    validation: Validation,
    raw_extraction: dict[str, Any] | None,
    execution_id: str | None,
    confidence_threshold: float,
    duplicate_of: str | None = None,
    expense_account_id: str | None = None,
    bank_accounts: list[dict[str, Any]] | None = None,
    scope: Scope = "live",
    demo_session_id: str | None = None,
) -> None:
    """expense_account_id: the supplier's default account, recorded on a ready invoice (audit trail).
    bank_accounts: the stored form (masked numbers and fingerprints, bank_details.stored_form)."""
    v = validation
    row: dict[str, Any] = {
        "id": invoice_id,
        "channel": channel,
        "source_ref": source_ref,
        "received_at": received_at,
        "file_name": file_name,
        "file_mime_type": mime_type,
        "file_size_bytes": size,
        "file_sha256": sha256,
        "file_path": file_path,
        "file_delete_after": file_delete_after,
        "status": v.status,
        "status_reasons": _reasons(v.reasons),
        "checks": Jsonb(v.checks),
        "confidence": v.confidence,
        "ocr_confidence_avg": v.confidence_ocr,
        "confidence_completeness": v.confidence_completeness,
        "confidence_threshold": confidence_threshold,
        "tax_mode": v.tax_mode,
        "n8n_execution_id": execution_id,
        "duplicate_of": duplicate_of,
        "extraction": Jsonb(raw_extraction) if raw_extraction is not None else None,
        "expense_account_id": expense_account_id,
        "expense_account_source": "supplier_default" if expense_account_id else None,
        "bank_accounts": Jsonb(bank_accounts or []),
        "scope": scope,
        "demo_session_id": demo_session_id,
    }
    if inv is not None:
        d = inv.dates
        row.update(
            {
                "document_type": inv.document_type or "invoice",
                "invoice_number": inv.invoice_number,
                "invoice_number_norm": inv.normalized.invoice_number,
                "related_invoice_number": inv.related_invoice_number,
                "invoice_date": inv.invoice_date,
                "due_date": inv.due_date,
                "dates_ambiguous": d.ambiguous,
                "invoice_date_raw": d.invoice_date_raw,
                "due_date_raw": d.due_date_raw,
                "date_order_suggested": d.suggested_order,
                "invoice_date_suggested": d.suggested_invoice_date,
                "due_date_suggested": d.suggested_due_date,
                "date_suggestion_basis": d.suggestion_basis,
                "service_period_start": inv.service_period_start,
                "service_period_end": inv.service_period_end,
                "service_period_start_raw": d.service_period_start_raw,
                "service_period_end_raw": d.service_period_end_raw,
                "service_period_start_suggested": d.suggested_service_period_start,
                "service_period_end_suggested": d.suggested_service_period_end,
                "supplier_name": inv.supplier.name,
                "supplier_name_norm": inv.normalized.supplier_name,
                "supplier_tax_id": inv.supplier.tax_id,
                "supplier_tax_id_norm": inv.normalized.supplier_tax_id,
                "supplier_country": inv.supplier.country,
                "buyer_name": inv.buyer.name,
                "buyer_tax_id": inv.buyer.tax_id,
                "currency": inv.currency,
                "subtotal": inv.subtotal,
                "tax_total": inv.tax_total,
                "total": inv.total,
                "language": inv.language,
                "ocr_text": inv.ocr_text,
                "extraction_provider": inv.extraction.provider,
                "ocr_confidence_min": inv.extraction.ocr_confidence_min,
            }
        )
    cols = list(row)
    conn.execute(f"INSERT INTO invoices ({', '.join(cols)}) VALUES ({', '.join('%(' + c + ')s' for c in cols)})", row)
    if inv is None:
        return
    with conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO invoice_lines (invoice_id, line_no, kind, tax_stage, description, quantity,
                                          unit_price, amount, tax_rate)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            [
                (
                    invoice_id,
                    li.line_no,
                    li.kind,
                    li.tax_stage,
                    li.description,
                    li.quantity,
                    li.unit_price,
                    li.amount,
                    li.tax_rate,
                )
                for li in inv.lines
            ],
        )
        cur.executemany(
            """INSERT INTO invoice_tax_lines (invoice_id, tax_type, label, rate, taxable_base, amount)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            [(invoice_id, t.tax_type, t.label, t.rate, t.taxable_base, t.amount) for t in inv.taxes],
        )


def open_review(conn: Conn, invoice_id: str, reasons: list[Issue]) -> int:
    """A review item takes its invoice's scope (review_queue.scope filters the review page and the summary)."""
    row = one(
        conn,
        "INSERT INTO review_queue (invoice_id, reasons, scope) SELECT id, %s, scope FROM invoices WHERE id = %s "
        "RETURNING id",
        (_reasons(reasons), invoice_id),
    )
    return int(row["id"])


def log(
    conn: Conn,
    *,
    step: str,
    level: str,
    message: str,
    invoice_id: str | None = None,
    channel: str | None = None,
    source_ref: str | None = None,
    execution_id: str | None = None,
    details: dict[str, Any] | None = None,
    scope: Scope | None = None,
) -> None:
    """Append to processing_log. Never pass document content in message/details (privacy).
    Scope: the invoice's when there is one and none is given, otherwise live."""
    conn.execute(
        """INSERT INTO processing_log (invoice_id, channel, source_ref, n8n_execution_id, step, level, message,
                                       details, scope)
           VALUES (%(invoice)s, %(channel)s, %(source)s, %(run)s, %(step)s, %(level)s, %(message)s, %(details)s,
                   COALESCE(%(scope)s, (SELECT scope FROM invoices WHERE id = %(invoice)s::uuid), 'live'))""",
        {
            "invoice": invoice_id,
            "channel": channel,
            "source": source_ref,
            "run": execution_id,
            "step": step,
            "level": level,
            "message": message,
            "details": Jsonb(details or {}),
            "scope": scope,
        },
    )
