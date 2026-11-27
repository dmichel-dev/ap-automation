"""The public demo's sessions and limits (docs/demo.md). Deterministic rules only.

A visitor starts a session with one click: no account, no sign-in, nothing typed. The browser gets a
random token (cookie; only its SHA-256 is stored) and the page shows the session's code, which names
it in the email address invoices+CODE@ and in the page URL. The code alone never opens the page: it
needs the cookie too (session_for_page).

Limits, all from settings (Limits):
  - session starts per client address, per hour and per day; sessions active at once;
  - files per session, all channels together; file size; pages per file;
  - OCR pages per day and per month for visitors' own files (stored sample results cost nothing):
    reserved before the OCR call, handed back if it fails;
  - emails per sender and day (keyed hash of the address, never the address).

Expired sessions are deleted with their invoices (purge_expired). What they left outside the database
(Gmail messages, Drive files, the n8n runs that handled their items) goes on the deletion list, which
the clean-up workflow works through: deleted permanently, never moved to a trash. A refused email has
no session to wait for: it goes on the list at once.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import secrets
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from psycopg.types.json import Jsonb

from . import db, demo_samples, files, review
from .web import render
from .web.security import new_token, token_digest

log = logging.getLogger(__name__)

CODE_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"  # no i, l, o, 0, 1: read aloud or typed without doubt
CODE_LENGTH = 8
CODE_PATTERN = f"[{CODE_ALPHABET}]{{{CODE_LENGTH}}}"  # also in the email address (intake.demo_code)
KEEP_STARTS = timedelta(days=2)  # longer than the daily limit's window
KEEP_SENDERS_DAYS = 2
KEEP_DAILY_BUDGET_DAYS = 40  # the month's figure is its own row
STALE_UPLOAD = timedelta(minutes=1)  # still queued after this: its processing call was missed
GONE = frozenset({200, 204, 404})  # a delete request's answers that mean the item is gone


@dataclass(frozen=True)
class Limits:
    enabled: bool = False
    hash_key: bytes = b""  # keys the client-address and sender digests (DEMO_HASH_KEY)
    session_minutes: int = 60
    starts_per_hour: int = 3
    starts_per_day: int = 10
    active_sessions: int = 30
    files_per_session: int = 5
    max_file_bytes: int = 5 * 1024 * 1024
    max_pages: int = 3
    pages_per_day: int = 200
    pages_per_month: int = 2000
    emails_per_sender_day: int = 10

    @property
    def open(self) -> bool:
        """Switched on and configured: without a hash key nothing starts (a missing setting only stops it)."""
        return self.enabled and len(self.hash_key) >= 16


class Refused(Exception):
    """A demo limit or rule said no. `code` is shown to the visitor in plain words (web/reasons.py)."""

    def __init__(self, code: str, retry_after: int | None = None):
        super().__init__(code)
        self.code = code  # demo_paused | too_many_starts | demo_full | session_expired | file_limit |
        # file_too_large | too_many_pages | unreadable_file | budget_reached | sender_limit
        self.retry_after = retry_after  # seconds, when waiting helps


# What each refusal means, in plain words (the demo page shows them; a rejected invoice's reason)
REFUSALS = {
    "demo_paused": "The demo is paused for now.",
    "too_many_starts": "Too many demo sessions from this network. Please try again later.",
    "demo_full": "The demo is busy right now: all session slots are in use. Try again in a few minutes.",
    "session_expired": "This demo session has ended.",
    "file_limit": "This demo session has used all its files.",
    "file_too_large": "The file is larger than the demo accepts.",
    "too_many_pages": "The file has more pages than the demo accepts.",
    "unreadable_file": "The file can't be read: it may be damaged or protected.",
    "unsupported_type": "Only PDF, PNG, JPEG, WebP and TIFF files are accepted.",
    "empty_file": "The file is empty.",
    "budget_reached": "The demo's OCR budget is used up for now, but the samples still work.",
    "sender_limit": "Too many demo emails from this sender today.",
}


@dataclass(frozen=True)
class Session:
    id: str
    code: str
    expires_at: datetime
    files_used: int


@dataclass(frozen=True)
class Started:
    session: Session
    token: str  # goes to the browser once, as the cookie; never stored


def _session(row: db.Row) -> Session:
    return Session(str(row["id"]), row["code"], row["expires_at"], row["files_used"])


def keyed_digest(limits: Limits, value: str) -> str:
    """HMAC-SHA256 (hex): an address can't be recovered by hashing every possible one."""
    return hmac.new(limits.hash_key, value.strip().lower().encode(), hashlib.sha256).hexdigest()


def new_code() -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))


# =====================================================================================
# Sessions
# =====================================================================================
def start_session(conn: db.Conn, limits: Limits, client: str, now: datetime) -> Started:
    """One click → a session, unless a limit says no (Refused). The caller commits."""
    if not limits.open:
        raise Refused("demo_paused")
    db.lock(conn, "demo:start")  # starts one at a time: the counts below can't race
    client_digest = keyed_digest(limits, client)
    _check_starts(conn, limits, client_digest, now)
    active = db.one(conn, "SELECT count(*) AS n FROM demo_sessions WHERE expires_at > %s", (now,))["n"]
    if active >= limits.active_sessions:
        raise Refused("demo_full", retry_after=_seconds_until_next_expiry(conn, now))
    token = new_token()
    row = _insert_session(conn, token, client_digest, now, now + timedelta(minutes=limits.session_minutes))
    conn.execute("INSERT INTO demo_starts (client_digest, started_at) VALUES (%s, %s)", (client_digest, now))
    db.log(conn, step="demo", level="info", message="demo session started", scope="demo")
    return Started(_session(row), token)


def _check_starts(conn: db.Conn, limits: Limits, client_digest: str, now: datetime) -> None:
    for window, limit in ((timedelta(hours=1), limits.starts_per_hour), (timedelta(days=1), limits.starts_per_day)):
        rows = conn.execute(
            "SELECT started_at FROM demo_starts WHERE client_digest = %s AND started_at > %s "
            "ORDER BY started_at DESC LIMIT %s",
            (client_digest, now - window, limit),
        ).fetchall()
        if len(rows) >= limit:
            oldest_counted = rows[-1]["started_at"]
            raise Refused("too_many_starts", retry_after=_seconds(oldest_counted + window - now))


def _seconds_until_next_expiry(conn: db.Conn, now: datetime) -> int:
    row = db.one(conn, "SELECT min(expires_at) AS t FROM demo_sessions WHERE expires_at > %s", (now,))
    return _seconds(row["t"] - now) if row["t"] else 60


def _seconds(delta: timedelta) -> int:
    return max(1, int(delta.total_seconds()) + 1)


def _insert_session(conn: db.Conn, token: str, client_digest: str, now: datetime, expires: datetime) -> db.Row:
    for _ in range(5):  # a code already in use: draw again (31^8 codes, very unlikely)
        row = conn.execute(
            """INSERT INTO demo_sessions (code, token_digest, client_digest, created_at, expires_at)
               VALUES (%s, %s, %s, %s, %s) ON CONFLICT (code) DO NOTHING RETURNING *""",
            (new_code(), token_digest(token), client_digest, now, expires),
        ).fetchone()
        if row:
            return row
    raise RuntimeError("could not draw an unused session code")


def session_for_token(conn: db.Conn, token: str | None, now: datetime) -> Session | None:
    """The live session this browser's cookie holds, if any (the start page sends it back there)."""
    if not token:
        return None
    row = conn.execute(
        "SELECT * FROM demo_sessions WHERE token_digest = %s AND expires_at > %s", (token_digest(token), now)
    ).fetchone()
    return _session(row) if row else None


def session_for_page(conn: db.Conn, token: str | None, code: str | None, now: datetime) -> Session | None:
    """The session whose page this is: the cookie's token AND the code in the URL must belong to the
    same live session. A code alone (a shared or guessed link) opens nothing."""
    session = session_for_token(conn, token, now)
    if session is None or not code or not re.fullmatch(CODE_PATTERN, code):
        return None
    return session if hmac.compare_digest(session.code, code) else None


def session_by_code(conn: db.Conn, code: str, now: datetime) -> Session | None:
    """For mail to invoices+CODE@: the live session with that code, if any."""
    row = conn.execute("SELECT * FROM demo_sessions WHERE code = %s AND expires_at > %s", (code, now)).fetchone()
    return _session(row) if row else None


# =====================================================================================
# Files and pages
# =====================================================================================
def check_file(content: bytes, limits: Limits) -> int:
    """Size, type (from the bytes) and page count of a visitor's file → its pages, or Refused.
    The same check for every channel, before anything is queued or sent to the OCR service."""
    if len(content) > limits.max_file_bytes:
        raise Refused("file_too_large")
    try:
        prepared = files.prepare(content)
    except files.UnsupportedFile as exc:
        raise Refused(exc.code) from exc
    pages = page_count(prepared)
    if pages > limits.max_pages:
        raise Refused("too_many_pages")
    return pages


def page_count(prepared: files.PreparedFile) -> int:
    """Counted in the renderer's sandboxed child process (the file is untrusted)."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "file"
        path.write_bytes(prepared.content)
        try:
            return render.page_count(path, prepared.mime_type)
        except render.RenderError as exc:
            raise Refused("unreadable_file") from exc


def take_file_slot(conn: db.Conn, limits: Limits, session_id: str, now: datetime) -> None:
    """One more file for the session, or Refused (expired, or its files used up)."""
    row = conn.execute(
        """UPDATE demo_sessions SET files_used = files_used + 1
            WHERE id = %s AND expires_at > %s AND files_used < %s RETURNING files_used""",
        (session_id, now, limits.files_per_session),
    ).fetchone()
    if row is None:
        live = conn.execute("SELECT 1 FROM demo_sessions WHERE id = %s AND expires_at > %s", (session_id, now))
        raise Refused("file_limit" if live.fetchone() else "session_expired")


def _periods(day: date) -> tuple[tuple[str, date], tuple[str, date]]:
    return ("day", day), ("month", day.replace(day=1))


def reserve_pages(conn: db.Conn, limits: Limits, pages: int, day: date) -> None:
    """Takes `pages` from today's and this month's OCR budget, or Refused if either would go over.
    The caller commits on success and rolls back on Refused."""
    db.lock(conn, "demo:budget")
    caps = {"day": limits.pages_per_day, "month": limits.pages_per_month}
    for period, start in _periods(day):
        conn.execute(
            "INSERT INTO demo_budget (period, period_start) VALUES (%s, %s) ON CONFLICT DO NOTHING", (period, start)
        )
        used = db.one(conn, "SELECT pages FROM demo_budget WHERE period = %s AND period_start = %s", (period, start))[
            "pages"
        ]
        if used + pages > caps[period]:
            raise Refused("budget_reached")
        conn.execute(
            "UPDATE demo_budget SET pages = pages + %s WHERE period = %s AND period_start = %s", (pages, period, start)
        )


def release_pages(conn: db.Conn, pages: int, day: date) -> None:
    """The OCR call failed: its pages go back to the budget they were taken from."""
    for period, start in _periods(day):
        conn.execute(
            "UPDATE demo_budget SET pages = greatest(pages - %s, 0) WHERE period = %s AND period_start = %s",
            (pages, period, start),
        )


def budget_left(conn: db.Conn, limits: Limits, day: date) -> int:
    """Pages a visitor's file may still use today (for the page: uploads paused at 0)."""
    used = {
        r["period"]: r["pages"]
        for r in conn.execute(
            "SELECT period, pages FROM demo_budget WHERE (period, period_start) IN ((%s, %s), (%s, %s))",
            [v for p in _periods(day) for v in p],
        ).fetchall()
    }
    left = min(limits.pages_per_day - int(used.get("day", 0)), limits.pages_per_month - int(used.get("month", 0)))
    return max(0, left)


# =====================================================================================
# Email
# =====================================================================================
def admit_email(conn: db.Conn, limits: Limits, session: Session, sender: str, message_id: str | None) -> None:
    """A demo email: counted once per message for its sender (Refused over the daily limit) and
    remembered for the clean-up. A message seen before (re-check) isn't counted again."""
    if message_id:
        new = conn.execute(
            "INSERT INTO demo_external_items (session_id, channel, item_id) VALUES (%s, 'email', %s) "
            "ON CONFLICT DO NOTHING RETURNING 1",
            (session.id, message_id),
        ).fetchone()
        if new is None:
            return
    digest = keyed_digest(limits, sender or "unknown sender")
    row = conn.execute(
        """INSERT INTO demo_senders AS s (sender_digest, day, emails) VALUES (%s, current_date, 1)
           ON CONFLICT (sender_digest, day) DO UPDATE SET emails = s.emails + 1 WHERE s.emails < %s
           RETURNING emails""",
        (digest, limits.emails_per_sender_day),
    ).fetchone()
    if row is None:
        raise Refused("sender_limit")


@dataclass(frozen=True)
class EmailRun:
    """One n8n run's look at one email to invoices+CODE@."""

    code: str
    sender: str
    message_id: str | None
    execution_id: str | None


def email_session(conn: db.Conn, limits: Limits, email: EmailRun, now: datetime) -> Session:
    """The live session an email to invoices+CODE@ belongs to, once its sender is within the limit;
    the run that read it goes with the session. A refused email (and its run) is deleted at once:
    there is no session to wait for."""
    try:
        session = _admitted(conn, limits, email, now)
    except Refused:
        delete_soon(conn, "email", email.message_id)
        delete_soon(conn, "n8n", email.execution_id)
        raise
    note_run(conn, session.id, email.execution_id)
    return session


def _admitted(conn: db.Conn, limits: Limits, email: EmailRun, now: datetime) -> Session:
    if not limits.open:
        raise Refused("demo_paused")
    session = session_by_code(conn, email.code, now)
    if session is None:
        raise Refused("session_expired")
    admit_email(conn, limits, session, email.sender, email.message_id)
    return session


# =====================================================================================
# Uploads and samples (queued by the demo page, processed through n8n)
# =====================================================================================
@dataclass(frozen=True)
class Upload:
    id: int
    session_id: str
    file_name: str
    content: bytes


def queue_file(
    conn: db.Conn,
    limits: Limits,
    session: Session,
    file_name: str,
    content: bytes,
    now: datetime,
    sample: str | None = None,
) -> int:
    """A sample click or an upload → queued for processing (Refused if a limit says no). Samples are
    known files: only the session's file count applies."""
    if sample is None:
        check_file(content, limits)
    live = session_by_code(conn, session.code, now)
    if live is None:
        raise Refused("session_expired")
    if live.files_used + files_pending(conn, session.id) >= limits.files_per_session:
        raise Refused("file_limit")
    row = db.one(
        conn,
        """INSERT INTO demo_uploads (session_id, kind, sample, file_name, size, content)
           VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
        (session.id, "sample" if sample else "upload", sample, file_name[:255] or "file", len(content), content),
    )
    return int(row["id"])


def claim_upload(conn: db.Conn, upload_id: int, execution_id: str | None = None) -> Upload | None:
    """The queued file, now marked as processing (None if unknown or already taken); the n8n run that
    processes it goes with its session."""
    row = conn.execute(
        """UPDATE demo_uploads SET status = 'processing' WHERE id = %s AND status = 'queued'
           RETURNING id, session_id, file_name, content""",
        (upload_id,),
    ).fetchone()
    if row is None:
        return None
    note_run(conn, str(row["session_id"]), execution_id)
    return Upload(row["id"], str(row["session_id"]), row["file_name"], bytes(row["content"]))


def stale_uploads(conn: db.Conn, now: datetime) -> list[int]:
    """Files still queued well after they were sent: their processing call never reached n8n."""
    rows = conn.execute(
        "SELECT id FROM demo_uploads WHERE status = 'queued' AND created_at <= %s ORDER BY id",
        (now - STALE_UPLOAD,),
    ).fetchall()
    return [int(r["id"]) for r in rows]


def finish_upload(conn: db.Conn, upload_id: int, invoice_id: str | None, error: str | None = None) -> None:
    """Processed (with its invoice) or failed (with an error code); the bytes are dropped either way."""
    conn.execute(
        "UPDATE demo_uploads SET status = %s, invoice_id = %s, error = %s, content = NULL WHERE id = %s",
        ("failed" if error else "done", invoice_id, error, upload_id),
    )


# =====================================================================================
# What the session page shows
# =====================================================================================
def session_items(conn: db.Conn, session_id: str) -> list[db.Row]:
    """The session's files, newest first: those still queued, being processed or failed (kind
    'upload'), and every invoice (kind 'invoice': emailed ones have no upload row). The page reads
    no document content: only the processing results (view demo_invoice_results)."""
    uploads = conn.execute(
        """SELECT 'upload' AS kind, id::text AS id, sample, file_name, status, error, created_at
             FROM demo_uploads WHERE session_id = %s AND status <> 'done'""",
        (session_id,),
    ).fetchall()
    invoices = conn.execute(
        """SELECT 'invoice' AS kind, i.id::text AS id, u.sample, i.file_name, i.channel, i.status,
                  i.status_reasons AS reasons, i.supplier_name, i.invoice_number, i.document_type,
                  i.total, i.currency, i.confidence, i.created_at, i.lines,
                  p.preview IS NOT NULL AS has_preview, p.preview -> 'account_names' AS account_names
             FROM demo_invoice_results i
             LEFT JOIN demo_uploads u ON u.invoice_id = i.id
             LEFT JOIN demo_bill_previews p ON p.invoice_id = i.id
            WHERE i.demo_session_id = %s""",
        (session_id,),
    ).fetchall()
    return sorted([*uploads, *invoices], key=lambda r: r["created_at"], reverse=True)


def samples_sent(conn: db.Conn, session_id: str) -> set[str]:
    rows = conn.execute(
        "SELECT DISTINCT sample FROM demo_uploads WHERE session_id = %s AND sample IS NOT NULL", (session_id,)
    ).fetchall()
    return {r["sample"] for r in rows}


def files_pending(conn: db.Conn, session_id: str) -> int:
    """Files still waiting in the queue. A file being processed already took its slot (files_used,
    take_file_slot), so it isn't counted again; processing enforces the limit itself."""
    return int(
        db.one(
            conn, "SELECT count(*) AS n FROM demo_uploads WHERE session_id = %s AND status = 'queued'", (session_id,)
        )["n"]
    )


def session_bill(conn: db.Conn, session_id: str, invoice_id: str) -> db.Row | None:
    """A ready invoice of this session with its bill preview; None for anything else (another
    session's invoice included)."""
    return conn.execute(
        """SELECT i.id::text AS id, i.supplier_name, i.invoice_number, i.total, i.currency, p.preview
             FROM demo_invoice_results i JOIN demo_bill_previews p ON p.invoice_id = i.id
            WHERE i.id = %s::uuid AND i.demo_session_id = %s::uuid""",
        (invoice_id, session_id),
    ).fetchone()


def session_review(conn: db.Conn, session_id: str, invoice_id: str) -> dict[str, Any] | None:
    """A needs-review invoice of this session as the read-only review page shows it, read through the
    views of migration 021 only (no bank details, no file, no OCR text); None for anything else (another
    session's invoice, a ready or rejected one, a live one). `sample` names the sample it came from."""
    row = conn.execute(
        """SELECT r.*, u.sample
             FROM demo_review_items r LEFT JOIN demo_uploads u ON u.invoice_id = r.id
            WHERE r.id = %s::uuid AND r.demo_session_id = %s::uuid
            ORDER BY r.review_id DESC LIMIT 1""",
        (invoice_id, session_id),
    ).fetchone()
    if row is None:
        return None
    lines = conn.execute(
        """SELECT line_no, kind, tax_stage, description, quantity, unit_price, amount, tax_rate
             FROM demo_review_lines WHERE invoice_id = %s ORDER BY line_no""",
        (row["id"],),
    ).fetchall()
    taxes = conn.execute(
        """SELECT tax_type, label, rate, taxable_base, amount
             FROM demo_review_tax_lines WHERE invoice_id = %s ORDER BY id""",
        (row["id"],),
    ).fetchall()
    item = {
        "id": row["review_id"], "invoice_id": row["id"], "stage": row["review_stage"], "status": row["review_status"],
        "scope": "demo", "reasons": row["review_reasons"], "created_at": row["review_created_at"],
        # A demo item is never decided, corrected or sent to QuickBooks
        "corrections": [], "decision": None, "reviewer": None, "resolved_at": None, "waiting_for": None,
        "effect_error": None,
    }  # fmt: skip
    inv = {k: v for k, v in row.items() if not k.startswith("review_")} | {"file_path": None, "bank_accounts": []}
    default = demo_samples.supplier_default(inv["supplier_key"])  # the demo's own, never a live default
    data = review.page_data(item, inv, lines, taxes, supplier_default=default, payment=None)
    return {"data": data, "sample": row["sample"]}


# =====================================================================================
# Bill preview and clean-up
# =====================================================================================
def store_preview(conn: db.Conn, invoice_id: str, preview: dict[str, Any]) -> None:
    conn.execute(
        "INSERT INTO demo_bill_previews (invoice_id, preview) VALUES (%s, %s) "
        "ON CONFLICT (invoice_id) DO UPDATE SET preview = EXCLUDED.preview",
        (invoice_id, Jsonb(preview)),
    )


@dataclass(frozen=True)
class Purged:
    sessions: int
    invoices: int
    files: list[str]  # stored originals to delete after COMMIT


def purge_expired(conn: db.Conn, now: datetime) -> Purged:
    """Expired sessions go with their invoices, uploads and previews. What they left outside the
    database goes on the deletion list. Old counters go too. The caller commits, then deletes `files`."""
    db.lock(conn, "demo:purge")
    ids = [r["id"] for r in conn.execute("SELECT id FROM demo_sessions WHERE expires_at <= %s", (now,)).fetchall()]
    rows = conn.execute("SELECT id, file_path FROM invoices WHERE demo_session_id = ANY(%s::uuid[])", (ids,)).fetchall()
    conn.execute(
        "INSERT INTO demo_deletions (channel, item_id) SELECT channel, item_id FROM demo_external_items "
        "WHERE session_id = ANY(%s::uuid[]) ON CONFLICT DO NOTHING",
        (ids,),
    )
    conn.execute("DELETE FROM demo_sessions WHERE id = ANY(%s::uuid[])", (ids,))
    conn.execute("DELETE FROM demo_starts WHERE started_at < %s", (now - KEEP_STARTS,))
    conn.execute("DELETE FROM demo_senders WHERE day < %s", (now.date() - timedelta(days=KEEP_SENDERS_DAYS),))
    conn.execute(
        "DELETE FROM demo_budget WHERE period = 'day' AND period_start < %s",
        (now.date() - timedelta(days=KEEP_DAILY_BUDGET_DAYS),),
    )
    if ids:
        db.log(
            conn,
            step="demo",
            level="info",
            message=f"expired demo sessions deleted: {len(ids)}",
            details={"sessions": len(ids), "invoices": len(rows)},
            scope="demo",
        )
    return Purged(len(ids), len(rows), [r["file_path"] for r in rows if r["file_path"]])


# =====================================================================================
# Outside the database: n8n runs, Gmail messages and Drive files, deleted permanently
# =====================================================================================
def note_run(conn: db.Conn, session_id: str, execution_id: str | None) -> None:
    """An n8n run handled one of the session's items: its record goes when the session ends. A run
    shared by two sessions is noted once and goes with the first that ends."""
    if execution_id:
        conn.execute(
            "INSERT INTO demo_external_items (session_id, channel, item_id) VALUES (%s, 'n8n', %s) "
            "ON CONFLICT DO NOTHING",
            (session_id, execution_id),
        )


def delete_soon(conn: db.Conn, channel: str, item_id: str | None) -> None:
    """On the deletion list now, with no session to wait for."""
    if item_id:
        conn.execute(
            "INSERT INTO demo_deletions (channel, item_id) VALUES (%s, %s) ON CONFLICT DO NOTHING", (channel, item_id)
        )


def deletions_due(conn: db.Conn, limit: int = 50) -> list[db.Row]:
    return conn.execute(
        "SELECT channel, item_id FROM demo_deletions ORDER BY queued_at, channel, item_id LIMIT %s", (limit,)
    ).fetchall()


@dataclass(frozen=True)
class DeletionResult:
    item_id: str
    status_code: int  # the service's answer to the delete request; 0: none (network error)


def deletions_done(conn: db.Conn, channel: str, results: list[DeletionResult]) -> dict[str, int]:
    """Deleted now, or already gone (404): off the list. Any other answer: kept for the next
    clean-up, and logged."""
    gone = [r.item_id for r in results if r.status_code in GONE]
    failed = sorted({r.status_code for r in results if r.status_code not in GONE})
    removed = conn.execute(
        "DELETE FROM demo_deletions WHERE channel = %s AND item_id = ANY(%s)", (channel, gone)
    ).rowcount
    kept = len(results) - len(gone)
    if kept:
        db.log(
            conn,
            step="demo",
            level="warning",
            message="demo deletion failed; the next clean-up tries again",
            details={"channel": channel, "items": kept, "status_codes": failed},
            scope="demo",
        )
    return {"removed": removed, "kept": kept}
