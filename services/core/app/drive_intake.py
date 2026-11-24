"""Google Drive intake: which files in the Inbox folder go to processing now.

The Inbox folder is the queue. n8n lists it every minute and moves each file out once it has an
answer: to Processed (any status) or Error (nothing a person can process, or given up). n8n sends
the files' metadata only (ID, name, type, size), never their content. Per file, the core answers:

  process  a new file, or a failed one whose retry is due (counted as an attempt); leased for
           LEASE so a run that starts while it is still being processed leaves it alone
  wait     leased by a running run, waiting for its retry (backoff of intake_failures.py), or over
           this run's limit: it stays in Inbox
  error    not a PDF or image, too large, or given up after failures: moved to Error

A file processed again never creates a second invoice: its (channel, source_ref) returns the stored
result, and the same bytes from another channel are an exact duplicate (processing.py).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import timedelta

from . import db, files, intake, intake_failures

CHANNEL = "drive"
LEASE = timedelta(minutes=15)
PROCESS_LIMIT = 5  # files per run: each one waits for the OCR service


@dataclass(frozen=True)
class DriveFile:
    id: str
    name: str | None
    mime_type: str | None
    size: int | None  # bytes; Google's own documents have none


@dataclass(frozen=True)
class FileAnswer:
    id: str
    route: str  # process | wait | error
    reason: str | None = None
    mime: str | None = None


def source_ref(file_id: str) -> str:
    return f"{CHANNEL}:{file_id}"


def refusal(f: DriveFile) -> tuple[str | None, str | None]:
    """(type to process, None), or (None, why the core can't process this file)."""
    mime = intake.file_mime(f.mime_type, f.name)
    if mime is None:
        return None, "unsupported_type"
    if (f.size or 0) > files.MAX_FILE_BYTES:
        return None, "file_too_large"
    return mime, None


def _lease(conn: db.Conn, file_id: str, execution_id: str | None) -> bool:
    """True if this run now holds the file's lease (none, or an expired one)."""
    row = conn.execute(
        """INSERT INTO intake_leases (channel, item_id, leased_until, execution_id)
           VALUES (%(ch)s, %(id)s, now() + %(lease)s, %(run)s)
           ON CONFLICT (channel, item_id) DO UPDATE
              SET leased_until = EXCLUDED.leased_until, execution_id = EXCLUDED.execution_id
            WHERE intake_leases.leased_until <= now()
           RETURNING item_id""",
        {"ch": CHANNEL, "id": file_id, "lease": LEASE, "run": execution_id},
    ).fetchone()
    return row is not None


def _log_refused(conn: db.Conn, f: DriveFile, why: str, execution_id: str | None) -> None:
    db.log(
        conn,
        step="intake",
        level="warning",
        message=f"Drive file not processed: {why}",
        channel=CHANNEL,
        source_ref=source_ref(f.id),
        execution_id=execution_id,
        details={"type": f.mime_type or "unknown", "size": f.size, "why": why},  # never the file name
    )


def _answer(conn: db.Conn, f: DriveFile, state: str, execution_id: str | None) -> FileAnswer:
    mime, why = refusal(f)
    if why:
        _log_refused(conn, f, why, execution_id)
        return FileAnswer(f.id, "error", why)
    if state == "given_up":
        return FileAnswer(f.id, "error", "given_up")
    if state == "waiting":
        return FileAnswer(f.id, "wait", "retry_later")
    if not _lease(conn, f.id, execution_id):
        return FileAnswer(f.id, "wait", "in_progress")
    return FileAnswer(f.id, "process", None, mime)


def screen_files(conn: db.Conn, listed: list[DriveFile], execution_id: str | None) -> list[dict[str, str | None]]:
    """One answer per listed file, in the listed order (oldest first)."""
    listed = list({f.id: f for f in listed}.values())
    # An expired lease means the same as none: files that left the Inbox leave nothing behind
    conn.execute("DELETE FROM intake_leases WHERE channel = %s AND leased_until <= now()", (CHANNEL,))
    states = intake_failures.listed_states(conn, CHANNEL, [f.id for f in listed], execution_id)
    answers: list[FileAnswer] = []
    for f in listed:
        processing = sum(a.route == "process" for a in answers)
        if processing >= PROCESS_LIMIT and refusal(f)[1] is None:
            answers.append(FileAnswer(f.id, "wait", "next_run"))
            continue
        answers.append(_answer(conn, f, states[f.id], execution_id))
    return [asdict(a) for a in answers]
