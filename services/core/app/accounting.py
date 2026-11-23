"""Database side of Day 2: postings to the accounting system, bank sync storage, matching runs.

n8n makes every external call (QuickBooks, Plaid). The core keeps the state and the rules:
  claim_postings   → which invoices to post now, with the exact document to send (leased, retried)
  record_result    → what happened; deletes the stored file once it's attached
  store_sync_page  → Plaid transactions, cursor saved only after the last page
  run_matching     → deterministic matching; returns the bill payments to record
  record_payment   → the payment recorded in QuickBooks; paid vs paid_with_difference
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, TypedDict

from psycopg.types.json import Jsonb

from . import db, matching, postings
from .db import Conn, Row


class LogContext(TypedDict):
    invoice_id: str
    execution_id: str | None


log = logging.getLogger(__name__)


class Conflict(Exception):
    """The request contradicts recorded state (HTTP 409)."""


class NotFound(Exception):
    """Unknown invoice, posting, match or bank account (HTTP 404)."""


@dataclass(frozen=True)
class AccountingConfig:
    database_url: str
    storage_dir: Path
    home_currency: str
    expense_account_id: str
    tax_account_id: str
    bank_account_id: str
    qbo_base: str = ""  # https://…/v3/company/<realm>
    max_attempts: int = 5
    lease_minutes: int = 10


# =====================================================================================
# Postings
# =====================================================================================
def _posting(conn: Conn, posting_id: int) -> Row:
    return db.one(conn, "SELECT * FROM accounting_postings WHERE id = %s", (posting_id,))


def _posting_of(conn: Conn, invoice_id: Any) -> Row:
    return db.one(conn, "SELECT * FROM accounting_postings WHERE invoice_id = %s", (invoice_id,))


def _lock_posting(conn: Conn, invoice_id: str) -> Row:
    p = conn.execute("SELECT * FROM accounting_postings WHERE invoice_id = %s FOR UPDATE", (invoice_id,)).fetchone()
    if p is None:
        raise NotFound("no posting for this invoice")
    return p


def _object_type(inv: Row) -> str:
    return "vendor_credit" if inv["document_type"] == "credit_note" else "bill"


def _build_document(conn: Conn, inv: Row, cfg: AccountingConfig) -> Row:
    """QuickBooks body without VendorRef/ExchangeRate (the workflow adds those)."""
    lines = conn.execute(
        "SELECT line_no, kind, description, amount FROM invoice_lines WHERE invoice_id = %s ORDER BY line_no",
        (inv["id"],),
    ).fetchall()
    taxes = conn.execute(
        "SELECT tax_type, label, amount FROM invoice_tax_lines WHERE invoice_id = %s ORDER BY id", (inv["id"],)
    ).fetchall()
    doc_lines = postings.document_lines(
        lines=lines,
        taxes=taxes,
        tax_mode=inv["tax_mode"],
        total=inv["total"],
        currency=inv["currency"],
        is_credit_note=inv["document_type"] == "credit_note",
    )
    accounts = {"expense": cfg.expense_account_id, "tax": cfg.tax_account_id}
    body: dict[str, Any] = {
        "CurrencyRef": {"value": inv["currency"]},
        "DocNumber": postings.doc_number(inv["invoice_number"]),
        "TxnDate": inv["invoice_date"].isoformat(),
        "PrivateNote": f"AP Automation · invoice {inv['invoice_number']} · id {inv['id']}",
        "Line": [
            {
                "DetailType": "AccountBasedExpenseLineDetail",
                "Amount": float(ln.amount),
                "Description": ln.description,
                "AccountBasedExpenseLineDetail": {"AccountRef": {"value": accounts[ln.account]}},
            }
            for ln in doc_lines
        ],
    }
    if inv["due_date"] and inv["document_type"] != "credit_note":
        body["DueDate"] = inv["due_date"].isoformat()
    return body


def _account_currency(conn: Conn) -> str | None:
    row = conn.execute("SELECT account_currency FROM bank_sync_state ORDER BY created_at LIMIT 1").fetchone()
    return row["account_currency"] if row else None


def _create_missing_postings(conn: Conn, cfg: AccountingConfig, limit: int) -> None:
    """A posting row is created BEFORE any API call: that row is what prevents double posting."""
    rows = conn.execute(
        """SELECT i.* FROM invoices i
            WHERE i.status = 'ready' AND i.posting_excluded_reason IS NULL
              AND NOT EXISTS (SELECT 1 FROM accounting_postings p WHERE p.invoice_id = i.id)
            ORDER BY i.created_at LIMIT %s FOR UPDATE OF i SKIP LOCKED""",
        (limit,),
    ).fetchall()
    for inv in rows:
        kind = _object_type(inv)
        values = {
            "invoice_id": inv["id"],
            "object_type": kind,
            "currency": inv["currency"],
            "amount": abs(inv["total"]),
            "doc_number": postings.doc_number(inv["invoice_number"]),
            "payment_status": "not_applicable" if kind == "vendor_credit" else "unpaid",
            "payment_reason": None if kind == "vendor_credit" else "no_match_yet",
            "status": "pending",
            "attempts": 0,
            "last_error": None,
        }
        try:
            _build_document(conn, inv, cfg)
        except postings.PostingError as e:  # can't be posted correctly: stop, a person decides
            values.update(status="failed", attempts=cfg.max_attempts, last_error=f"{e.code}: {e}")
            db.log(
                conn,
                step="post",
                level="error",
                message="invoice cannot be posted",
                invoice_id=str(inv["id"]),
                details={"code": e.code},
            )
        cols = list(values)
        conn.execute(
            f"INSERT INTO accounting_postings ({', '.join(cols)}) VALUES "
            f"({', '.join('%(' + c + ')s' for c in cols)}) ON CONFLICT (invoice_id) DO NOTHING",
            values,
        )


def claim_postings(conn: Conn, cfg: AccountingConfig, limit: int = 10) -> list[Row]:
    """Returns up to `limit` postings to work on now, each leased for `lease_minutes`."""
    _create_missing_postings(conn, cfg, limit)
    due = conn.execute(
        """SELECT p.*, i.file_path FROM accounting_postings p JOIN invoices i ON i.id = p.invoice_id
            WHERE i.posting_excluded_reason IS NULL
              AND p.attempts < %(max)s AND p.next_attempt_at <= now()
              AND (p.claimed_until IS NULL OR p.claimed_until < now())
              AND (p.status IN ('pending', 'failed')
                   OR (p.status = 'posted' AND p.attachment_external_id IS NULL AND i.file_path IS NOT NULL))
            ORDER BY p.created_at LIMIT %(limit)s FOR UPDATE OF p SKIP LOCKED""",
        {"max": cfg.max_attempts, "limit": limit},
    ).fetchall()
    out = []
    for p in due:
        conn.execute(
            """UPDATE accounting_postings
                  SET claimed_until = now() + make_interval(mins => %(lease)s), attempts = attempts + 1,
                      next_attempt_at = now() + make_interval(mins => least(%(lease)s * power(2, attempts)::int, 720))
                WHERE id = %(id)s""",
            {"lease": cfg.lease_minutes, "id": p["id"]},
        )
        out.append(_work_item(conn, cfg, p))
    return out


def _work_item(conn: Conn, cfg: AccountingConfig, p: Row) -> Row:
    inv = db.one(conn, "SELECT * FROM invoices WHERE id = %s", (p["invoice_id"],))
    link = conn.execute(
        "SELECT external_id FROM vendor_links WHERE system = 'qbo' AND supplier_key = %s AND currency = %s",
        (inv["supplier_key"], inv["currency"]),
    ).fetchone()
    display = postings.vendor_display_name(inv["supplier_name"], inv["currency"], cfg.home_currency)
    entity = "VendorCredit" if p["object_type"] == "vendor_credit" else "Bill"
    ext = (inv["file_name"].rsplit(".", 1)[-1] if "." in inv["file_name"] else "pdf").lower()
    return {
        "posting_id": p["id"],
        "invoice_id": str(inv["id"]),
        "action": "attach" if p["status"] == "posted" else "create",
        "attempt": p["attempts"] + 1,
        "object_type": p["object_type"],
        "entity": entity,
        "endpoint": entity.lower(),
        # QuickBooks limits requestid to 50 characters
        "request_id": f"ap-{'vc' if p['object_type'] == 'vendor_credit' else 'bill'}-{inv['id']}",
        "currency": inv["currency"],
        "home_currency": cfg.home_currency,
        "needs_exchange_rate": inv["currency"] != cfg.home_currency,
        "exchange_rate_date": inv["invoice_date"].isoformat(),
        "amount": str(p["amount"]),
        "doc_number": p["doc_number"],
        "doc_number_q": postings.qbo_quote(p["doc_number"]),
        "external_id": p["external_id"],
        "vendor_external_id": p["vendor_external_id"],
        "vendor": {
            "external_id": link["external_id"] if link else None,
            "supplier_key": inv["supplier_key"],
            "display_name": display,
            "display_name_q": postings.qbo_quote(display),
            "company_name": inv["supplier_name"][:100],
        },
        "document": _build_document(conn, inv, cfg) if p["status"] != "posted" else None,
        "file": {
            "available": inv["file_path"] is not None,
            "path": f"/invoices/{inv['id']}/file",
            "file_name": f"{p['doc_number']}.{ext}".replace("/", "-"),
            "mime_type": inv["file_mime_type"],
        },
    }


def record_result(conn: Conn, cfg: AccountingConfig, invoice_id: str, r: Row) -> tuple[Row, str | None]:
    """Applies the workflow's outcome. Returns (posting, file_path_to_delete_after_commit)."""
    p = _lock_posting(conn, invoice_id)
    inv = db.one(conn, "SELECT * FROM invoices WHERE id = %s FOR UPDATE", (invoice_id,))
    ctx = LogContext(invoice_id=invoice_id, execution_id=r.get("execution_id"))
    if r["outcome"] == "posted":
        if p["status"] == "posted":
            if p["external_id"] != r["external_id"]:
                raise Conflict(f"already posted as {p['external_id']}")
            return p, None  # idempotent repeat
        to_delete = _record_posted(conn, cfg, p, inv, r, ctx)
    elif r["outcome"] == "attached":
        to_delete = _record_attached(conn, p, inv, r, ctx)
    else:
        _record_failed(conn, cfg, p, r, ctx)
        to_delete = None
    return _posting(conn, p["id"]), to_delete


def _exchange_rate(cfg: AccountingConfig, p: Row, inv: Row, r: Row) -> tuple[Decimal, date, str]:
    """(rate, rate date, source): 1 in the home currency, otherwise the rate QuickBooks used."""
    if p["currency"] == cfg.home_currency:
        return Decimal(1), inv["invoice_date"], "home_currency"
    if not (r.get("exchange_rate") and r.get("exchange_rate_date")):
        raise Conflict("a foreign-currency posting needs the exchange rate that was used")
    return Decimal(str(r["exchange_rate"])), r["exchange_rate_date"], "qbo"


def _record_posted(conn: Conn, cfg: AccountingConfig, p: Row, inv: Row, r: Row, ctx: LogContext) -> str | None:
    rate, rate_date, source = _exchange_rate(cfg, p, inv, r)
    reason = None
    if p["object_type"] == "bill":
        # Until FX matching (Day 4), only bills in the bank account's currency can be matched. Before any
        # bank account is registered, the payments account is assumed to be in the home currency.
        account_ccy = _account_currency(conn) or cfg.home_currency
        reason = "fx_match_pending" if p["currency"] != account_ccy else "no_match_yet"
    conn.execute(
        """UPDATE accounting_postings SET status = 'posted', posted_at = now(), claimed_until = NULL,
                  last_error = NULL, external_id = %s, vendor_external_id = %s, exchange_rate = %s,
                  exchange_rate_date = %s, exchange_rate_source = %s, payment_reason = %s,
                  attachment_external_id = COALESCE(%s, attachment_external_id)
            WHERE id = %s""",
        (
            r["external_id"],
            r["vendor_external_id"],
            rate,
            rate_date,
            source,
            reason,
            r.get("attachment_external_id"),
            p["id"],
        ),
    )
    db.log(
        conn,
        step="post",
        level="info",
        message=f"posted as {p['object_type']}",
        **ctx,
        details={
            "external_id": r["external_id"],
            "currency": p["currency"],
            "exchange_rate": str(rate),
            "attachment": bool(r.get("attachment_external_id")),
        },
    )
    return _mark_file_deleted(conn, inv) if r.get("attachment_external_id") else None


def _record_attached(conn: Conn, p: Row, inv: Row, r: Row, ctx: LogContext) -> str | None:
    if p["status"] != "posted":
        raise Conflict("cannot attach a file before the document is posted")
    conn.execute(
        "UPDATE accounting_postings SET attachment_external_id = %s, claimed_until = NULL, "
        "last_error = NULL WHERE id = %s",
        (r["attachment_external_id"], p["id"]),
    )
    db.log(conn, step="post", level="info", message="original file attached", **ctx)
    return _mark_file_deleted(conn, inv)


def _record_failed(conn: Conn, cfg: AccountingConfig, p: Row, r: Row, ctx: LogContext) -> None:
    """The core decides what a failure means: retried later, or stopped (and reviewed when a person must decide)."""
    code, retryable = postings.classify_failure(
        r.get("error_code"), r.get("http_status"), r.get("fault_code"), REVIEW_ERRORS
    )
    if code in REVIEW_ERRORS:  # a person must decide; no blind retries
        _open_review(conn, str(p["invoice_id"]), "posting", code, r.get("error_message"))
    conn.execute(
        """UPDATE accounting_postings
              SET status = CASE WHEN status = 'posted' THEN 'posted' ELSE 'failed' END,
                  claimed_until = NULL, last_error = %s,
                  attempts = CASE WHEN %s THEN attempts ELSE %s END
            WHERE id = %s""",
        (f"{code}: {(r.get('error_message') or '')[:500]}", retryable, cfg.max_attempts, p["id"]),
    )
    db.log(
        conn,
        step="post",
        level="warning" if retryable else "error",
        message="posting failed" if p["status"] != "posted" else "attachment failed",
        **ctx,
        details={"code": code, "retryable": retryable},
    )


# Posting failures that need a person (Day 3 review flow), not retries: code → (field, explanation)
REVIEW_ERRORS = {
    "vendor_currency_conflict": (
        "supplier_name",
        "A vendor with this name already exists in the accounting system in another currency; "
        "vendor currencies are fixed, so a different vendor must be chosen or created.",
    ),
    "vendor_inactive": ("supplier_name", "The matching vendor in the accounting system is inactive."),
    "duplicate_in_accounting": (
        "invoice_number",
        "Several documents with this number already exist for this vendor in the accounting system.",
    ),
}


def lookup_decision(
    conn: Conn, invoice_id: str, kind: Literal["vendor", "document"], status: int, body: Any, vendor_id: str | None
) -> postings.Decision:
    """What the workflow does with a QuickBooks search result (vendor by name, or document by number)."""
    p = conn.execute("SELECT object_type, currency FROM accounting_postings WHERE invoice_id = %s", (invoice_id,))
    row = p.fetchone()
    if row is None:
        raise NotFound("no posting for this invoice")
    if kind == "vendor":
        return postings.vendor_decision(status, body, row["currency"])
    entity = "VendorCredit" if row["object_type"] == "vendor_credit" else "Bill"
    return postings.existing_document_decision(status, body, entity, vendor_id or "")


# Matching outcomes a person must settle (Day 3 review flow)
MATCHING_REVIEWS = {
    "ambiguous_match": (
        "payment",
        "Several bank transactions or bills fit this payment; choose the right pair.",
    ),
}
_STAGE_STEP = {"posting": "post", "matching": "match"}


def _open_review(
    conn: Conn, invoice_id: str, stage: Literal["posting", "matching"], code: str, detail: str | None
) -> None:
    """One open review item per invoice: if one is already open, it stays the only one."""
    field, message = {**REVIEW_ERRORS, **MATCHING_REVIEWS}[code]
    reason = {"code": code, "field": field, "message": message, "detail": (detail or "")[:300]}
    row = conn.execute(
        """INSERT INTO review_queue (invoice_id, reasons, stage) VALUES (%s, %s, %s)
           ON CONFLICT (invoice_id) WHERE status = 'open' DO NOTHING RETURNING id""",
        (invoice_id, Jsonb([reason]), stage),
    ).fetchone()
    if row:  # logged once, when opened (matching runs every hour)
        db.log(
            conn,
            step=_STAGE_STEP[stage],
            level="warning",
            message=f"{stage} needs review",
            invoice_id=invoice_id,
            details={"code": code, "review_id": row["id"]},
        )


def retry_posting(conn: Conn, invoice_id: str, note: str | None = None) -> Row:
    """Operator action after a fix: a failed posting gets a fresh set of attempts."""
    p = _lock_posting(conn, invoice_id)
    if p["status"] != "failed":
        raise Conflict(f"only a failed posting can be retried (status is {p['status']})")
    review = conn.execute(
        "SELECT 1 FROM review_queue WHERE invoice_id = %s AND status = 'open' AND stage = 'posting'", (invoice_id,)
    ).fetchone()
    if review:
        raise Conflict("an open posting review decides this invoice")
    conn.execute(
        "UPDATE accounting_postings SET attempts = 0, next_attempt_at = now(), claimed_until = NULL WHERE id = %s",
        (p["id"],),
    )
    db.log(
        conn,
        step="post",
        level="info",
        message="posting retry requested",
        invoice_id=invoice_id,
        details={"previous_error": (p["last_error"] or "")[:200], "note": (note or "")[:200]},
    )
    return _posting(conn, p["id"])


def _mark_file_deleted(conn: Conn, inv: Row) -> str | None:
    """Privacy: once the original is attached in the accounting system, our copy goes."""
    if inv["file_path"] is None:
        return None
    conn.execute(
        "UPDATE invoices SET file_path = NULL, file_deleted_at = now(), file_deletion_reason = 'posted' WHERE id = %s",
        (inv["id"],),
    )
    return str(inv["file_path"])


def delete_stored_file(storage_dir: Path, rel_path: str | None) -> None:
    """Called after COMMIT. If it fails, the record is still right and the retention purge cleans up."""
    if not rel_path:
        return
    path = _safe_path(storage_dir, rel_path)
    try:
        path.unlink(missing_ok=True)
    except OSError:
        log.warning("could not delete stored file %s", rel_path)


def _safe_path(storage_dir: Path, rel_path: str) -> Path:
    root = storage_dir.resolve()
    path = (root / rel_path).resolve()
    if not path.is_relative_to(root):
        raise NotFound("file outside the storage directory")
    return path


def stored_file(conn: Conn, storage_dir: Path, invoice_id: str) -> tuple[Path, str, str]:
    row = conn.execute(
        "SELECT file_path, file_mime_type, file_name FROM invoices WHERE id = %s", (invoice_id,)
    ).fetchone()
    if row is None or row["file_path"] is None:
        raise NotFound("no stored file for this invoice")
    path = _safe_path(storage_dir, row["file_path"])
    if not path.is_file():
        raise NotFound("stored file is missing")
    return path, row["file_mime_type"], row["file_name"]


def link_vendor(conn: Conn, supplier_key: str, currency: str, external_id: str, display_name: str) -> Row:
    taken = conn.execute(
        "SELECT supplier_key, currency FROM vendor_links WHERE system = 'qbo' AND external_id = %s", (external_id,)
    ).fetchone()
    if taken and (taken["supplier_key"], taken["currency"]) != (supplier_key, currency):
        # A QuickBooks vendor has exactly one currency and belongs to one supplier
        raise Conflict(f"vendor {external_id} is already linked to another supplier or currency")
    row = conn.execute(
        """INSERT INTO vendor_links (supplier_key, currency, external_id, display_name) VALUES (%s, %s, %s, %s)
           ON CONFLICT (system, supplier_key, currency) DO NOTHING RETURNING *""",
        (supplier_key, currency, external_id, display_name),
    ).fetchone()
    if row is None:
        row = db.one(
            conn,
            "SELECT * FROM vendor_links WHERE system = 'qbo' AND supplier_key = %s AND currency = %s",
            (supplier_key, currency),
        )
        if row["external_id"] != external_id:
            raise Conflict(f"supplier already linked to vendor {row['external_id']} in {currency}")
    return row


def exclude_from_posting(conn: Conn, invoice_id: str, reason: str, note: str | None = None) -> Row:
    inv = conn.execute(
        "SELECT id, posting_excluded_reason FROM invoices WHERE id = %s FOR UPDATE", (invoice_id,)
    ).fetchone()
    if inv is None:
        raise NotFound("unknown invoice")
    posted = conn.execute(
        "SELECT 1 FROM accounting_postings WHERE invoice_id = %s AND status = 'posted'", (invoice_id,)
    ).fetchone()
    if posted:
        raise Conflict("already posted; reverse it in the accounting system instead")
    if inv["posting_excluded_reason"] is None:
        conn.execute(
            "UPDATE invoices SET posting_excluded_reason = %s, posting_excluded_at = now() WHERE id = %s",
            (reason, invoice_id),
        )
        db.log(
            conn,
            step="post",
            level="info",
            message="excluded from posting",
            invoice_id=invoice_id,
            details={"reason": reason, "note": (note or "")[:200]},
        )
    return db.one(
        conn, "SELECT id, posting_excluded_reason, posting_excluded_at FROM invoices WHERE id = %s", (invoice_id,)
    )


# =====================================================================================
# Bank sync (Plaid transactions as received; positive amount = money out)
# =====================================================================================
TX_FIELDS = (
    "transaction_id",
    "account_id",
    "amount",
    "iso_currency_code",
    "unofficial_currency_code",
    "date",
    "authorized_date",
    "name",
    "merchant_name",
    "pending",
    "pending_transaction_id",
)


def register_account(conn: Conn, item_key: str, account_id: str, account_currency: str) -> Row:
    return db.one(
        conn,
        """INSERT INTO bank_sync_state (item_key, account_id, account_currency) VALUES (%s, %s, %s)
           ON CONFLICT (item_key) DO UPDATE SET account_id = EXCLUDED.account_id,
                                                account_currency = EXCLUDED.account_currency
           RETURNING item_key, account_id, account_currency, cursor, last_synced_at""",
        (item_key, account_id, account_currency),
    )


def sync_state(conn: Conn, item_key: str) -> Row:
    row = conn.execute(
        "SELECT item_key, account_id, account_currency, cursor, last_synced_at FROM bank_sync_state "
        "WHERE item_key = %s",
        (item_key,),
    ).fetchone()
    if row is None:
        raise NotFound("unknown bank item")
    return row


def store_sync_page(
    conn: Conn,
    item_key: str,
    added: list[Row],
    modified: list[Row],
    removed: list[Row],
    next_cursor: str,
    has_more: bool,
) -> Row:
    """Idempotent: a page can be stored twice (Plaid asks to restart a failed pagination loop)."""
    state = conn.execute("SELECT * FROM bank_sync_state WHERE item_key = %s FOR UPDATE", (item_key,)).fetchone()
    if state is None:
        raise NotFound("unknown bank item")
    stored = skipped = 0
    for tx in [*added, *modified]:
        if tx.get("account_id") != state["account_id"]:
            skipped += 1  # other accounts (credit card, loans) aren't kept
            continue
        row = {k: tx.get(k) for k in TX_FIELDS}
        row["amount"] = Decimal(str(row["amount"]))
        conn.execute(
            f"""INSERT INTO bank_transactions (item_key, {", ".join(TX_FIELDS)}, raw)
                VALUES (%(item_key)s, {", ".join("%(" + k + ")s" for k in TX_FIELDS)}, %(raw)s)
                ON CONFLICT (transaction_id) DO UPDATE SET
                  {", ".join(f"{k} = EXCLUDED.{k}" for k in TX_FIELDS if k != "transaction_id")},
                  raw = EXCLUDED.raw, removed_at = NULL""",
            {**row, "item_key": item_key, "raw": Jsonb(tx)},
        )
        stored += 1
    removed_ids = [r["transaction_id"] for r in removed if r.get("transaction_id")]
    if removed_ids:
        conn.execute(
            "UPDATE bank_transactions SET removed_at = now() WHERE transaction_id = ANY(%s) AND removed_at IS NULL",
            (removed_ids,),
        )
        paid = db.one(
            conn,
            "SELECT count(*) AS n FROM matches WHERE bank_transaction_id = ANY(%s) AND status = 'accepted'",
            (removed_ids,),
        )["n"]
        if paid:
            db.log(
                conn,
                step="bank",
                level="warning",
                message="matched transaction removed by the bank",
                channel="system",
                details={"count": paid},
            )
    if not has_more:  # cursor only after the last page
        conn.execute(
            "UPDATE bank_sync_state SET cursor = %s, last_synced_at = now() WHERE item_key = %s",
            (next_cursor, item_key),
        )
    return {
        "stored": stored,
        "skipped_other_accounts": skipped,
        "removed": len(removed_ids),
        "cursor_saved": not has_more,
    }


# =====================================================================================
# Matching
# =====================================================================================
def run_matching(conn: Conn, cfg: AccountingConfig, item_key: str, rules: matching.Rules) -> Row:
    db.lock(conn, f"matching:{item_key}")  # one run at a time per account
    state = sync_state(conn, item_key)
    rows = conn.execute(
        """SELECT p.id AS posting_id, p.invoice_id, p.currency, p.amount, p.payment_reason,
                  i.invoice_date, i.due_date, i.supplier_name_norm, i.invoice_number_norm
             FROM accounting_postings p JOIN invoices i ON i.id = p.invoice_id
            WHERE p.status = 'posted' AND p.object_type = 'bill' AND p.payment_status = 'unpaid'
              AND p.payment_reason <> 'payment_pending' AND i.posting_excluded_reason IS NULL"""
    ).fetchall()

    def set_reason(posting_ids: list[int], reason: str) -> None:
        if posting_ids:
            conn.execute(
                "UPDATE accounting_postings SET payment_reason = %s WHERE id = ANY(%s) "
                "AND payment_reason IS DISTINCT FROM %s",
                (reason, posting_ids, reason),
            )

    # Constraint until FX matching (Day 4): only bills in the bank account's currency
    foreign = [r for r in rows if r["currency"] != state["account_currency"]]
    set_reason([r["posting_id"] for r in foreign], "fx_match_pending")
    same = [r for r in rows if r["currency"] == state["account_currency"]]

    txs = conn.execute(
        """SELECT t.* FROM bank_transactions t
            WHERE t.item_key = %s AND t.account_id = %s AND t.amount > 0 AND NOT t.pending
              AND t.removed_at IS NULL AND t.iso_currency_code = %s
              AND NOT EXISTS (SELECT 1 FROM matches m WHERE m.bank_transaction_id = t.transaction_id
                                                         AND m.status = 'accepted')""",
        (item_key, state["account_id"], state["account_currency"]),
    ).fetchall()
    bills = [
        matching.OpenBill(
            str(r["invoice_id"]),
            r["currency"],
            r["amount"],
            r["invoice_date"],
            r["due_date"],
            r["supplier_name_norm"],
            r["invoice_number_norm"],
        )
        for r in same
    ]
    bank = [
        matching.BankTx(
            t["transaction_id"],
            t["amount"],
            t["iso_currency_code"],
            t["date"],
            t["name"],
            t["merchant_name"],
            t["pending"],
        )
        for t in txs
    ]
    decision = matching.decide(bills, bank, rules)
    by_invoice = {str(r["invoice_id"]): r for r in same}

    for bill, tx, ev in decision.accepted:
        _save_match(conn, bill, tx, ev, "accepted")
        conn.execute(
            "UPDATE matches SET status = 'dismissed' WHERE invoice_id = %s AND status = 'candidate'", (bill.invoice_id,)
        )
        set_reason([by_invoice[bill.invoice_id]["posting_id"]], "payment_pending")
        db.log(
            conn,
            step="match",
            level="info",
            message="bank transaction matched",
            invoice_id=bill.invoice_id,
            channel="system",
            details={
                "difference": str(ev.difference),
                "days": ev.days_from_invoice,
                "name_similarity": ev.name_similarity,
                "rule_version": matching.RULE_VERSION,
            },
        )
    for invoice_id, found in decision.ambiguous.items():
        bill = next(b for b in bills if b.invoice_id == invoice_id)
        for tx, ev in found:
            _save_match(conn, bill, tx, ev, "candidate")
        set_reason([by_invoice[invoice_id]["posting_id"]], "ambiguous_match")
        db.log(
            conn,
            step="match",
            level="warning",
            message="several possible bank transactions",
            invoice_id=invoice_id,
            channel="system",
            details={"candidates": len(found)},
        )
        candidates = ", ".join(tx.transaction_id for tx, _ in found)
        _open_review(conn, invoice_id, "matching", "ambiguous_match", f"candidate bank transactions: {candidates}")
    set_reason([by_invoice[i]["posting_id"] for i in decision.unmatched], "no_match_yet")

    return {
        "account_currency": state["account_currency"],
        "open_bills": len(rows),
        "fx_match_pending": len(foreign),
        "accepted": len(decision.accepted),
        "ambiguous": len(decision.ambiguous),
        "unmatched": len(decision.unmatched),
        "payments_due": payments_due(conn, cfg),
    }


def _save_match(conn: Conn, bill: matching.OpenBill, tx: matching.BankTx, ev: matching.Evaluation, status: str) -> None:
    values = (
        bill.invoice_id,
        tx.transaction_id,
        status,
        bill.amount,
        ev.bank_amount,
        ev.difference,
        bill.currency,
        ev.days_from_invoice,
        ev.name_similarity,
        ev.invoice_number_found,
        matching.RULE_VERSION,
    )
    # An accepted match upgrades an earlier candidate row; a candidate never overwrites anything
    on_conflict = "DO UPDATE SET status = 'accepted'" if status == "accepted" else "DO NOTHING"
    conn.execute(
        f"""INSERT INTO matches (invoice_id, bank_transaction_id, status, invoice_amount, bank_amount, difference,
                                 currency, days_from_invoice, name_similarity, invoice_number_found, rule_version)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (invoice_id, bank_transaction_id) {on_conflict}""",
        values,
    )


def payments_due(conn: Conn, cfg: AccountingConfig) -> list[Row]:
    """Accepted matches whose payment isn't recorded in the accounting system yet (also retries)."""
    # Leased like postings: two overlapping runs never get the same payment to record
    rows = conn.execute(
        """WITH due AS (
             SELECT m.id FROM matches m
              WHERE m.status = 'accepted' AND m.external_payment_id IS NULL AND m.payment_attempts < %(max)s
                AND (m.payment_claimed_until IS NULL OR m.payment_claimed_until < now())
              ORDER BY m.id FOR UPDATE SKIP LOCKED)
           UPDATE matches m SET payment_claimed_until = now() + make_interval(mins => %(lease)s),
                                payment_attempts = m.payment_attempts + 1
             FROM due, accounting_postings p, bank_transactions t
            WHERE m.id = due.id AND p.invoice_id = m.invoice_id AND t.transaction_id = m.bank_transaction_id
        RETURNING m.*, p.external_id AS bill_id, p.vendor_external_id, p.amount AS bill_amount, t.date AS tx_date""",
        {"max": cfg.max_attempts, "lease": cfg.lease_minutes},
    ).fetchall()
    rows.sort(key=lambda r: r["id"])
    out = []
    for m in rows:
        applied = min(m["bank_amount"], m["bill_amount"])  # never apply more than the bill amount
        out.append(
            {
                "match_id": m["id"],
                "invoice_id": str(m["invoice_id"]),
                "request_id": f"ap-billpay-{m['id']}",
                "doc_number": payment_doc_number(m["id"]),
                "doc_number_q": postings.qbo_quote(payment_doc_number(m["id"])),
                "vendor_external_id": m["vendor_external_id"],
                "bank_amount": str(m["bank_amount"]),
                "bill_amount": str(m["bill_amount"]),
                "difference": str(m["difference"]),
                "document": {
                    "VendorRef": {"value": m["vendor_external_id"]},
                    "DocNumber": payment_doc_number(m["id"]),  # found again after a crash (see lookup)
                    "PayType": "Check",
                    "CheckPayment": {"BankAccountRef": {"value": cfg.bank_account_id}, "PrintStatus": "NotSet"},
                    "CurrencyRef": {"value": m["currency"]},
                    "TotalAmt": float(applied),
                    "TxnDate": m["tx_date"].isoformat(),
                    "PrivateNote": f"AP Automation · bank transaction {m['bank_transaction_id']} · "
                    f"bank amount {m['bank_amount']} {m['currency']}",
                    "Line": [{"Amount": float(applied), "LinkedTxn": [{"TxnId": m["bill_id"], "TxnType": "Bill"}]}],
                },
            }
        )
    return out


def payment_doc_number(match_id: int) -> str:
    return f"AP-PAY-{match_id}"


def _accepted_match(conn: Conn, match_id: int) -> Row:
    m = conn.execute("SELECT * FROM matches WHERE id = %s FOR UPDATE", (match_id,)).fetchone()
    if m is None:
        raise NotFound("unknown match")
    if m["status"] != "accepted":
        raise Conflict("only an accepted match can be paid")
    return m


def payment_lookup_decision(conn: Conn, match_id: int, status: int, body: Any) -> postings.Decision:
    """A bill payment with this match's number may exist already (a run that stopped after creating it)."""
    m = _accepted_match(conn, match_id)
    vendor = _posting_of(conn, m["invoice_id"])["vendor_external_id"]
    return postings.existing_document_decision(status, body, "BillPayment", str(vendor))


def record_payment_failure(conn: Conn, cfg: AccountingConfig, match_id: int, r: Row) -> Row:
    """The payment couldn't be recorded: retried while attempts remain, unless the failure says otherwise."""
    m = _accepted_match(conn, match_id)
    code, retryable = postings.classify_failure(
        r.get("error_code"), r.get("http_status"), r.get("fault_code"), REVIEW_ERRORS
    )
    if code in REVIEW_ERRORS:
        _open_review(conn, str(m["invoice_id"]), "posting", code, r.get("error_message"))
    conn.execute(
        """UPDATE matches SET payment_claimed_until = NULL, payment_last_error = %s,
                  payment_attempts = CASE WHEN %s THEN payment_attempts ELSE %s END
            WHERE id = %s""",
        (f"{code}: {(r.get('error_message') or '')[:500]}", retryable, cfg.max_attempts, match_id),
    )
    db.log(
        conn,
        step="match",
        level="warning" if retryable else "error",
        message="payment not recorded",
        invoice_id=str(m["invoice_id"]),
        channel="system",
        execution_id=r.get("execution_id"),
        details={"code": code, "retryable": retryable, "match_id": match_id},
    )
    return db.one(conn, "SELECT * FROM matches WHERE id = %s", (match_id,))


def record_payment(conn: Conn, match_id: int, external_payment_id: str, execution_id: str | None = None) -> Row:
    m = _accepted_match(conn, match_id)
    if m["external_payment_id"]:
        if m["external_payment_id"] != external_payment_id:
            raise Conflict(f"payment already recorded as {m['external_payment_id']}")
        return _posting_of(conn, m["invoice_id"])
    conn.execute(
        "UPDATE matches SET external_payment_id = %s, payment_recorded_at = now(), "
        "payment_claimed_until = NULL, payment_last_error = NULL WHERE id = %s",
        (external_payment_id, match_id),
    )
    applied = min(m["bank_amount"], m["invoice_amount"])
    exact = m["difference"] == 0
    conn.execute(
        """UPDATE accounting_postings SET payment_status = %s, payment_reason = %s, amount_paid = %s
            WHERE invoice_id = %s""",
        (
            "paid" if exact else "paid_with_difference",
            None if exact else "payment_difference",
            applied,
            m["invoice_id"],
        ),
    )
    db.log(
        conn,
        step="match",
        level="info" if exact else "warning",
        message="payment recorded" if exact else "payment recorded with a difference",
        invoice_id=str(m["invoice_id"]),
        channel="system",
        execution_id=execution_id,
        details={"external_payment_id": external_payment_id, "difference": str(m["difference"])},
    )
    return _posting_of(conn, m["invoice_id"])
