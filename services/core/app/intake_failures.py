"""Emails that could not be processed: retry later, or stop and let a person look.

n8n reports what it saw when an email failed (the core's HTTP status and error code, or no answer at
all) and labels it AP/Error. This module decides what that means:

  temporary  (OCR service timeout or overload, core unreachable or restarting): tried again by the
             re-check, after 15 min, 1 h, 4 h and 12 h (5 attempts over about 17 hours)
  permanent  (a file the OCR service refuses, a rejected API key): given up at once, a person looks

Every 15 minutes the re-check asks which AP/Error emails are due (`claim_due`). An email with the
label but no row is due at once: it failed while the core was unreachable, so its failure was never
recorded. Rows the re-check is due to retry are handed out even if their label was lost. Each
claim counts an attempt, so a run that dies without reporting can't retry an email forever.

Processing an email again never creates a second invoice: each attachment's (channel, source_ref)
is unique and returns the stored result (processing.py). After a success, the row is `resolved`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from . import db

# Wait after attempt n (n = 1…4); attempt 5 is the last
BACKOFF = (timedelta(minutes=15), timedelta(hours=1), timedelta(hours=4), timedelta(hours=12))
MAX_ATTEMPTS = len(BACKOFF) + 1
CLAIM_LIMIT = 10  # emails per re-check run
NOT_RECORDED = "not_recorded"  # AP/Error label without a row: the core couldn't record the failure
_REF_PREFIX = {"email": "gmail"}  # log references, as the workflow writes them (gmail:<message id>)


def source_ref(channel: str, message_id: str) -> str:
    return f"{_REF_PREFIX.get(channel, channel)}:{message_id}"


@dataclass(frozen=True)
class Failure:
    code: str
    permanent: bool
    explanation: str  # plain language, for the log and the daily summary


# The core's own error codes (main.py) that n8n passes on
_PROVIDER = {
    "provider_timeout": Failure("provider_timeout", False, "The OCR service did not answer in time."),
    "provider_rate_limit": Failure("provider_rate_limit", False, "The OCR service asked to slow down."),
    "provider_server": Failure("provider_server", False, "The OCR service had an error or was unreachable."),
    "provider_auth": Failure(
        "provider_auth",
        True,
        "The OCR service rejected the API key. Fix MISTRAL_API_KEY, then retry these emails.",
    ),
    "provider_bad_request": Failure(
        "provider_bad_request",
        True,
        "The OCR service refused this file (damaged, password-protected or too large). Ask the supplier "
        "for a new copy.",
    ),
    "provider_invalid_response": Failure(
        "provider_invalid_response", True, "The OCR service gave an answer that can't be read for this file."
    ),
}
_UNREACHABLE = Failure("core_unreachable", False, "The core service did not answer (stopped, restarting or busy).")
_CORE_ERROR = Failure("core_error", False, "The core service had an internal error (e.g. its database restarting).")
_CORE_AUTH = Failure(
    "core_auth", True, "The core service refused n8n's token. Check the n8n credential 'AP core token', then retry."
)
_NOT_RECORDED = Failure(
    NOT_RECORDED, False, "Labelled AP/Error while the core was unreachable, so the failure was never recorded."
)
_FETCH = Failure("email_fetch_failed", False, "The email or its attachments could not be downloaded from Gmail.")


def classify(stage: str, status_code: int | None, error: str | None) -> Failure:
    """What a failure means. Unknown answers count as temporary: the attempts are limited anyway."""
    if stage == "fetch":
        return _FETCH
    if error in _PROVIDER and status_code in (502, 503):
        return _PROVIDER[error]
    if not status_code:
        return _UNREACHABLE
    if status_code in (401, 403):
        return _CORE_AUTH
    if 400 <= status_code < 500:
        return Failure(
            f"core_refused_{status_code}", True, f"The core service refused the request (HTTP {status_code})."
        )
    return _CORE_ERROR


def explain(code: str) -> str:
    known = {f.code: f for f in (*_PROVIDER.values(), _UNREACHABLE, _CORE_ERROR, _CORE_AUTH, _NOT_RECORDED, _FETCH)}
    return known[code].explanation if code in known else f"Failed: {code}."


def _next_wait(attempts: int) -> timedelta:
    return BACKOFF[min(max(attempts, 1), len(BACKOFF)) - 1]


def _lock_row(conn: db.Conn, channel: str, message_id: str) -> db.Row | None:
    db.lock(conn, f"intake_failure:{channel}:{message_id}")
    return conn.execute(
        "SELECT * FROM intake_failures WHERE channel = %s AND message_id = %s", (channel, message_id)
    ).fetchone()


def _code(row: db.Row) -> str:
    """The last failure's code (an intake code, never a review reason)."""
    return str(row["last_error_code"])


def _answer(row: db.Row) -> dict[str, Any]:
    return {
        "state": row["state"],
        "attempts": row["attempts"],
        "max_attempts": MAX_ATTEMPTS,
        "next_attempt_at": row["next_attempt_at"].isoformat() if row["state"] == "waiting" else None,
        "code": _code(row),
        "explanation": explain(_code(row)),
    }


def record(
    conn: db.Conn,
    channel: str,
    message_id: str,
    stage: str,
    status_code: int | None,
    error: str | None,
    execution_id: str | None,
) -> dict[str, Any]:
    """A failure reported by the workflow. One attempt per n8n run, however many attachments failed."""
    failure = classify(stage, status_code, error)
    row = _lock_row(conn, channel, message_id)
    if row is None:
        row = db.one(
            conn,
            "INSERT INTO intake_failures (channel, message_id, last_error_code) VALUES (%s, %s, %s) RETURNING *",
            (channel, message_id, failure.code),
        )
    same_run = execution_id is not None and row["counted_execution_id"] == execution_id
    attempts = row["attempts"] if same_run else row["attempts"] + 1
    give_up = row["state"] == "given_up" or failure.permanent or attempts >= MAX_ATTEMPTS
    row = db.one(
        conn,
        """UPDATE intake_failures
              SET state = %(state)s, attempts = %(attempts)s, last_stage = %(stage)s,
                  last_status_code = %(status)s, last_error_code = %(code)s, permanent = %(permanent)s,
                  counted_execution_id = %(run)s, failed_execution_id = %(run)s, last_failed_at = now(),
                  next_attempt_at = now() + %(wait)s, resolved_at = NULL,
                  given_up_at = CASE WHEN %(give_up)s THEN coalesce(given_up_at, now()) END
            WHERE id = %(id)s RETURNING *""",
        {
            "state": "given_up" if give_up else "waiting",
            "attempts": attempts,
            "stage": stage,
            "status": status_code,
            "code": failure.code,
            "permanent": failure.permanent,
            "run": execution_id,
            "wait": _next_wait(attempts),
            "give_up": give_up,
            "id": row["id"],
        },
    )
    _log_failure(conn, row, failure, execution_id)
    return _answer(row)


def _log_failure(conn: db.Conn, row: db.Row, failure: Failure, execution_id: str | None) -> None:
    given_up = row["state"] == "given_up"
    message = (
        f"email failed, given up: {failure.explanation}"
        if given_up
        else f"email failed (attempt {row['attempts']} of {MAX_ATTEMPTS}), retried later: {failure.explanation}"
    )
    db.log(
        conn,
        step="intake_retry",
        level="error" if given_up else "warning",
        message=message,
        channel=row["channel"],
        source_ref=source_ref(row["channel"], row["message_id"]),
        execution_id=execution_id,
        details={
            "code": failure.code,
            "permanent": failure.permanent,
            "attempts": row["attempts"],
            "state": row["state"],
            "status_code": row["last_status_code"],
            "stage": row["last_stage"],
        },
    )


def claim_due(conn: db.Conn, channel: str, found: list[str], execution_id: str | None) -> list[str]:
    """The emails the re-check processes now, each counted as an attempt.

    `found`: the emails Gmail lists under AP/Error. One without a row is due at once (its failure was
    never recorded). A waiting row whose time has come is due even if its label was lost. A row whose
    attempts ran out without a final report (the run died) is given up here."""
    for message_id in dict.fromkeys(found):
        conn.execute(
            """INSERT INTO intake_failures (channel, message_id, last_error_code, last_stage)
               VALUES (%s, %s, %s, 'recheck') ON CONFLICT (channel, message_id) DO NOTHING""",
            (channel, message_id[:200], NOT_RECORDED),
        )
    _give_up_exhausted(conn, channel, execution_id)
    rows = conn.execute(
        """UPDATE intake_failures f
              SET attempts = f.attempts + 1, counted_execution_id = %(run)s,
                  next_attempt_at = now() + (%(waits)s::interval[])[least(f.attempts + 1, %(n)s)]
            WHERE f.id IN (SELECT id FROM intake_failures
                            WHERE channel = %(channel)s AND state = 'waiting' AND next_attempt_at <= now()
                            ORDER BY next_attempt_at, id LIMIT %(limit)s FOR UPDATE SKIP LOCKED)
            RETURNING f.*""",
        {"run": execution_id, "waits": list(BACKOFF), "n": len(BACKOFF), "channel": channel, "limit": CLAIM_LIMIT},
    ).fetchall()
    for row in rows:
        db.log(
            conn,
            step="intake_retry",
            level="info",
            message=f"re-check: trying the email again (attempt {row['attempts']} of {MAX_ATTEMPTS})",
            channel=channel,
            source_ref=source_ref(channel, row["message_id"]),
            execution_id=execution_id,
            details={"code": _code(row), "attempts": row["attempts"]},
        )
    return [r["message_id"] for r in rows]


def _give_up_exhausted(conn: db.Conn, channel: str, execution_id: str | None) -> None:
    rows = conn.execute(
        """UPDATE intake_failures SET state = 'given_up', given_up_at = now()
            WHERE channel = %s AND state = 'waiting' AND attempts >= %s AND next_attempt_at <= now()
            RETURNING *""",
        (channel, MAX_ATTEMPTS),
    ).fetchall()
    for row in rows:
        failure = Failure(_code(row), row["permanent"], explain(_code(row)))
        _log_failure(conn, row, failure, execution_id)


def resolve(conn: db.Conn, channel: str, message_id: str | None, execution_id: str | None) -> bool:
    """The email got an answer (processed, rejected, or nothing to process). A failure reported in
    the same run wins: another attachment of this email failed, so it still needs the re-check."""
    if not message_id:
        return False
    row = _lock_row(conn, channel, message_id)
    if row is None or row["state"] == "resolved":
        return False
    if execution_id is not None and row["failed_execution_id"] == execution_id:
        return False
    conn.execute(
        "UPDATE intake_failures SET state = 'resolved', resolved_at = now(), given_up_at = NULL WHERE id = %s",
        (row["id"],),
    )
    db.log(
        conn,
        step="intake_retry",
        level="info",
        message=f"email processed after {row['attempts']} failed attempt(s)",
        channel=channel,
        source_ref=source_ref(channel, message_id),
        execution_id=execution_id,
        details={"code": _code(row), "attempts": row["attempts"]},
    )
    return True


def retry(conn: db.Conn, channel: str, message_id: str | None, error_code: str | None, note: str | None) -> list[str]:
    """Operator action after a fix (e.g. a new API key): given-up emails get a fresh set of attempts.
    By email, or every given-up email with one error code."""
    if not (message_id or error_code):
        raise ValueError("name an email or an error code")
    rows = conn.execute(
        """UPDATE intake_failures
              SET state = 'waiting', attempts = 0, next_attempt_at = now(), given_up_at = NULL,
                  counted_execution_id = NULL, operator_retries = operator_retries + 1
            WHERE channel = %s AND state = 'given_up'
              AND (%s::text IS NULL OR message_id = %s) AND (%s::text IS NULL OR last_error_code = %s)
            RETURNING message_id""",
        (channel, message_id, message_id, error_code, error_code),
    ).fetchall()
    ids = [r["message_id"] for r in rows]
    db.log(
        conn,
        step="intake_retry",
        level="info",
        message=f"operator retry: {len(ids)} email(s) will be tried again",
        channel=channel,
        details={"message_id": message_id, "error_code": error_code, "count": len(ids), "note": note},
    )
    return ids


def summary(conn: db.Conn, hours: int = 24) -> dict[str, Any]:
    """For the daily summary: what needs a person, what is still being retried, what recovered."""
    given_up = conn.execute(
        """SELECT channel, message_id, last_error_code AS code, attempts, first_failed_at, given_up_at
             FROM intake_failures WHERE state = 'given_up' ORDER BY given_up_at""",
    ).fetchall()
    counts = db.one(
        conn,
        """SELECT count(*) FILTER (WHERE state = 'waiting') AS retrying,
                  count(*) FILTER (WHERE state = 'resolved' AND resolved_at > now() - %(h)s) AS recovered
             FROM intake_failures""",
        {"h": timedelta(hours=hours)},
    )
    crashes = db.one(
        conn,
        "SELECT count(*) AS n FROM processing_log WHERE step = 'workflow_error' AND logged_at > now() - %s",
        (timedelta(hours=hours),),
    )
    return {
        "hours": hours,
        "given_up": [{**db.jsonable(r), "explanation": explain(r["code"])} for r in given_up],
        "retrying": counts["retrying"],
        "recovered": counts["recovered"],
        "workflow_errors": crashes["n"],
    }
