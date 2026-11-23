"""'Run now' requests: a workflow started by hand through its n8n webhook instead of waiting for its
schedule. The webhook passes the request's timestamp, signature and target here; the core decides and
records every request, allowed or refused.

The token never travels: n8n stores every webhook request, headers included, so the caller signs the
request instead (scripts/run_now.py):

    signature = hex(HMAC-SHA256(RUN_NOW_TOKEN, "<unix timestamp>.<target>"))

  - the signature is compared in constant time (no token configured: every request is refused, so a
    missing setting can only make it stricter);
  - it is valid for MAX_AGE around its timestamp, and only once (the SHA-256 of each signature that
    started a run is kept): a stored or captured request can't be replayed;
  - after MAX_REFUSED refused requests within REFUSED_WINDOW, every request is refused (429) until the
    window has passed, without even checking the signature (no guessing at speed);
  - each target runs at most MAX_RUNS times per RUN_WINDOW (429 with Retry-After).
"""

import hashlib
import hmac
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Literal

from . import db

TARGETS = ("posting", "bank")  # posting: post approved bills; bank: bank sync, matching, payments, write-offs
MAX_AGE = timedelta(minutes=5)
MAX_REFUSED = 10
REFUSED_WINDOW = timedelta(minutes=10)
MAX_RUNS = 2
RUN_WINDOW = timedelta(minutes=1)
Outcome = Literal["run", "bad_token", "expired", "replayed", "unknown_target", "rate_limited", "not_configured"]
REFUSED: tuple[Outcome, ...] = ("bad_token", "replayed", "unknown_target")  # counted for the lockout
STATUS: dict[Outcome, int] = {
    "run": 202,
    "bad_token": 401,
    "expired": 401,
    "replayed": 401,
    "unknown_target": 400,
    "rate_limited": 429,
    "not_configured": 503,
}
MESSAGES: dict[Outcome, str] = {
    "run": "started",
    "bad_token": "missing or wrong signature",
    "expired": "the request's timestamp is too old or in the future: check the clock, then sign it again",
    "replayed": "this signed request was already used: sign a new one",
    "unknown_target": f"unknown target: use one of {', '.join(TARGETS)}",
    "rate_limited": "too many requests: try again later",
    "not_configured": "run-now requests are not enabled",
}


@dataclass(frozen=True)
class Signed:
    timestamp: str
    signature: str
    target: str


@dataclass(frozen=True)
class Answer:
    outcome: Outcome
    target: str
    status: int
    message: str
    retry_after: int | None = None  # seconds, for 429


def sign(token: str, timestamp: str, target: str) -> str:
    return hmac.new(token.encode(), f"{timestamp}.{target}".encode(), hashlib.sha256).hexdigest()


def same_secret(given: str, expected: str) -> bool:
    """Constant time, whatever the lengths: both sides are hashed to the same size first."""
    a = hashlib.sha256(given.encode()).digest()
    b = hashlib.sha256(expected.encode()).digest()
    return hmac.compare_digest(a, b) and bool(expected)


def _fresh(timestamp: str, now: float) -> bool:
    return timestamp.isdigit() and abs(now - int(timestamp)) <= MAX_AGE.total_seconds()


def _count(conn: db.Conn, outcomes: tuple[str, ...], window: timedelta, target: str | None = None) -> int:
    row = db.one(
        conn,
        """SELECT count(*) AS n FROM run_requests
            WHERE outcome = ANY(%s) AND at > now() - %s AND (%s::text IS NULL OR target = %s)""",
        (list(outcomes), window, target, target),
    )
    return int(row["n"])


def _used(conn: db.Conn, digest: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM run_requests WHERE signature_sha256 = %s AND outcome = 'run'", (digest,)
    ).fetchone()
    return row is not None


def _decide(conn: db.Conn, token: str, req: Signed, digest: str) -> tuple[Outcome, int | None]:
    if not token:
        return "not_configured", None
    if _count(conn, REFUSED, REFUSED_WINDOW) >= MAX_REFUSED:
        return "rate_limited", int(REFUSED_WINDOW.total_seconds())
    if not same_secret(req.signature.lower(), sign(token, req.timestamp, req.target)):
        return "bad_token", None
    if not _fresh(req.timestamp, time.time()):
        return "expired", None
    if req.target not in TARGETS:
        return "unknown_target", None
    if _used(conn, digest):
        return "replayed", None
    if _count(conn, ("run",), RUN_WINDOW, req.target) >= MAX_RUNS:
        return "rate_limited", int(RUN_WINDOW.total_seconds())
    return "run", None


def authorize(conn: db.Conn, token: str, req: Signed) -> Answer:
    db.lock(conn, "run-now")  # one decision at a time: the counts can't be raced
    digest = hashlib.sha256(req.signature.lower().encode()).hexdigest()
    outcome, retry_after = _decide(conn, token, req, digest)
    shown = req.target[:40]
    conn.execute(
        "INSERT INTO run_requests (target, outcome, signature_sha256) VALUES (%s, %s, %s)",
        (shown, outcome, digest if outcome == "run" else None),
    )
    db.log(
        conn,
        step="run_now",
        level="info" if outcome == "run" else "warning",
        message=f"run-now request: {outcome}",
        channel="system",
        details={"target": shown},
    )
    return Answer(outcome, shown, STATUS[outcome], MESSAGES[outcome], retry_after)
