"""Review-page accounts, sign-in (lockout, per-address limit) and server-side sessions.

Which roles may sign in depends on the listener (settings.py):
  internal  reviewer, admin, demo
  public    demo only. Other accounts are treated as unknown there: same answer, same timing,
            and no failed-attempt count (an internet visitor can't lock out a reviewer).
"""

import math
from dataclasses import dataclass
from datetime import timedelta
from typing import Literal

from .. import db
from ..review import Scope
from .security import (
    ATTEMPTS_KEPT,
    DECISIONS_PER_MINUTE,
    IP_MAX_ATTEMPTS,
    IP_WINDOW,
    LOCK_FIRST,
    LOCK_MAX,
    MAX_FAILED_LOGINS,
    SESSION_IDLE,
    SESSION_MAX,
    check_password_policy,
    email_digest,
    hash_password,
    needs_rehash,
    new_token,
    normalize_email,
    token_digest,
    verify_password,
)
from .settings import Listener

Role = Literal["reviewer", "admin", "demo"]
Outcome = Literal["success", "bad_credentials", "locked", "inactive", "role_not_allowed_here"]
ROLES_BY_LISTENER: dict[Listener, tuple[Role, ...]] = {
    "internal": ("reviewer", "admin", "demo"),
    "public": ("demo",),
}
SCOPES_BY_ROLE: dict[Role, tuple[Scope, ...]] = {
    "reviewer": ("live", "e2e"),
    "admin": ("live", "e2e"),
    "demo": ("demo",),
}


class UserError(ValueError):
    """A user-management request that can't be done (unknown user, bad role/scope, weak password)."""


@dataclass(frozen=True)
class Viewer:
    """The signed-in person, from the session. Every read and decision is filtered by its scope."""

    user_id: int
    email: str
    display_name: str
    role: Role
    scope: Scope
    csrf: str

    @property
    def reviewer(self) -> str:
        """How decisions are recorded in the audit trail: the name, and the account that acted."""
        return f"{self.display_name} (review user {self.user_id})"


@dataclass(frozen=True)
class SignIn:
    outcome: Outcome
    token: str | None = None  # the new session's cookie value, on success


# =====================================================================================
# Sign-in
# =====================================================================================
def retry_after(conn: db.Conn, client_ip: str) -> int | None:
    """Seconds to wait if this address used up its sign-in attempts, else None."""
    row = db.one(
        conn,
        """SELECT count(*) AS n, extract(epoch FROM min(at) + %s - now()) AS wait
             FROM login_attempts WHERE client_ip = %s AND at > now() - %s""",
        (IP_WINDOW, client_ip, IP_WINDOW),
    )
    if row["n"] < IP_MAX_ATTEMPTS:
        return None
    wait: float = row["wait"]
    return max(1, math.ceil(wait))


def sign_in(
    conn: db.Conn, listener: Listener, email: str, password: str, client_ip: str, user_agent: str | None
) -> SignIn:
    """Checks the password (always, for uniform timing), records the attempt, counts failures.
    The caller commits even when the outcome is a refusal (the failure count must be kept)."""
    address = normalize_email(email)
    user = conn.execute(
        """SELECT *, coalesce(locked_until > now(), false) AS locked
             FROM review_users WHERE email = %s FOR UPDATE""",
        (address,),
    ).fetchone()
    allowed = user is not None and user["role"] in ROLES_BY_LISTENER[listener]
    matches = verify_password(user["password_hash"] if user and allowed else None, password)
    outcome = _outcome(user, allowed, matches)
    conn.execute(
        "INSERT INTO login_attempts (client_ip, email_hash, listener, outcome) VALUES (%s, %s, %s, %s)",
        (client_ip, email_digest(address), listener, outcome),
    )
    if user is not None and outcome == "bad_credentials":
        _count_failure(conn, user)
    if user is None or outcome != "success":
        return SignIn(outcome)
    _signed_in(conn, user, password)
    return SignIn("success", create_session(conn, user["id"], listener, client_ip, user_agent))


def _outcome(user: db.Row | None, allowed: bool, matches: bool) -> Outcome:
    if user is None:
        return "bad_credentials"
    if not allowed:
        return "role_not_allowed_here"
    if not user["active"]:
        return "inactive"
    if user["locked"]:
        return "locked"  # even with the right password, until the lock ends
    return "success" if matches else "bad_credentials"


def lock_duration(lock_count: int) -> timedelta:
    """15 min for the first lock, doubling at each new one, at most 4 h."""
    return min(LOCK_FIRST * (1 << min(lock_count, 10)), LOCK_MAX)


def _count_failure(conn: db.Conn, user: db.Row) -> None:
    failures = user["failed_logins"] + 1
    if failures < MAX_FAILED_LOGINS:
        conn.execute("UPDATE review_users SET failed_logins = %s WHERE id = %s", (failures, user["id"]))
        return
    conn.execute(
        """UPDATE review_users SET failed_logins = 0, lock_count = lock_count + 1, locked_until = now() + %s
            WHERE id = %s""",
        (lock_duration(user["lock_count"]), user["id"]),
    )


def _signed_in(conn: db.Conn, user: db.Row, password: str) -> None:
    rehashed = hash_password(password) if needs_rehash(user["password_hash"]) else user["password_hash"]
    conn.execute(
        """UPDATE review_users SET failed_logins = 0, lock_count = 0, locked_until = NULL, last_login_at = now(),
                  password_hash = %s WHERE id = %s""",
        (rehashed, user["id"]),
    )


# =====================================================================================
# Sessions
# =====================================================================================
def create_session(conn: db.Conn, user_id: int, listener: Listener, client_ip: str, user_agent: str | None) -> str:
    """A new random token (the cookie value); the database keeps only its digest."""
    token = new_token()
    conn.execute(
        """INSERT INTO review_sessions (token_hash, user_id, listener, csrf_token, expires_at, client_ip, user_agent)
           VALUES (%s, %s, %s, %s, now() + %s, %s, %s)""",
        (token_digest(token), user_id, listener, new_token(), SESSION_MAX, client_ip, (user_agent or "")[:200]),
    )
    _purge(conn)
    return token


def _purge(conn: db.Conn) -> None:
    conn.execute("DELETE FROM review_sessions WHERE expires_at <= now() OR last_seen_at <= now() - %s", (SESSION_IDLE,))
    conn.execute("DELETE FROM login_attempts WHERE at <= now() - %s", (ATTEMPTS_KEPT,))


def session_viewer(conn: db.Conn, token: str | None, listener: Listener) -> Viewer | None:
    """The viewer of a valid session on this listener (and marks it used), else None. A session
    ends when idle too long, at its absolute limit, if the account is deactivated, or if the account's
    role may no longer use this listener."""
    if not token:
        return None
    row = conn.execute(
        """UPDATE review_sessions s SET last_seen_at = now()
             FROM review_users u
            WHERE s.token_hash = %s AND u.id = s.user_id AND s.listener = %s
              AND s.expires_at > now() AND s.last_seen_at > now() - %s
              AND u.active AND u.role = ANY(%s)
        RETURNING u.id, u.email, u.display_name, u.role, u.scope, s.csrf_token""",
        (token_digest(token), listener, SESSION_IDLE, list(ROLES_BY_LISTENER[listener])),
    ).fetchone()
    if row is None:
        return None
    return Viewer(row["id"], row["email"], row["display_name"], row["role"], row["scope"], row["csrf_token"])


def end_session(conn: db.Conn, token: str | None) -> None:
    if token:
        conn.execute("DELETE FROM review_sessions WHERE token_hash = %s", (token_digest(token),))


def count_decision(conn: db.Conn, token: str) -> int:
    """Decisions made in this session's current one-minute window, this one included."""
    row = db.one(
        conn,
        """UPDATE review_sessions SET
                  window_count = CASE WHEN window_start <= now() - interval '1 minute' THEN 1 ELSE window_count + 1 END,
                  window_start = CASE WHEN window_start <= now() - interval '1 minute' THEN now() ELSE window_start END
            WHERE token_hash = %s RETURNING window_count""",
        (token_digest(token),),
    )
    count: int = row["window_count"]
    return count


def decisions_exceeded(count: int) -> bool:
    return count > DECISIONS_PER_MINUTE


# =====================================================================================
# User management (the CLI in users.py; there is no self-registration)
# =====================================================================================
def add_user(conn: db.Conn, email: str, display_name: str, role: Role, scope: Scope, password: str) -> int:
    address = normalize_email(email)
    if scope not in SCOPES_BY_ROLE[role]:
        raise UserError(f"a {role} account works on {' or '.join(SCOPES_BY_ROLE[role])} data, not {scope}")
    if not 1 <= len(display_name.strip()) <= 100:
        raise UserError("the display name must have 1 to 100 characters")
    check_password_policy(password, address)
    if conn.execute("SELECT 1 FROM review_users WHERE email = %s", (address,)).fetchone():
        raise UserError(f"{address} already has an account")
    row = db.one(
        conn,
        """INSERT INTO review_users (email, display_name, password_hash, role, scope)
           VALUES (%s, %s, %s, %s, %s) RETURNING id""",
        (address, display_name.strip(), hash_password(password), role, scope),
    )
    user_id: int = row["id"]
    return user_id


def _user_id(conn: db.Conn, email: str) -> int:
    row = conn.execute("SELECT id FROM review_users WHERE email = %s", (normalize_email(email),)).fetchone()
    if row is None:
        raise UserError("no account with this email")
    user_id: int = row["id"]
    return user_id


def set_password(conn: db.Conn, email: str, password: str) -> None:
    """New password; the account's sessions end (a changed password signs out everywhere)."""
    check_password_policy(password, email)
    user_id = _user_id(conn, email)
    conn.execute(
        "UPDATE review_users SET password_hash = %s, failed_logins = 0, locked_until = NULL WHERE id = %s",
        (hash_password(password), user_id),
    )
    conn.execute("DELETE FROM review_sessions WHERE user_id = %s", (user_id,))


def set_active(conn: db.Conn, email: str, active: bool) -> None:
    user_id = _user_id(conn, email)
    conn.execute("UPDATE review_users SET active = %s WHERE id = %s", (active, user_id))
    if not active:
        conn.execute("DELETE FROM review_sessions WHERE user_id = %s", (user_id,))


def unlock(conn: db.Conn, email: str) -> None:
    conn.execute(
        "UPDATE review_users SET failed_logins = 0, lock_count = 0, locked_until = NULL WHERE id = %s",
        (_user_id(conn, email),),
    )


def list_users(conn: db.Conn) -> list[db.Row]:
    return conn.execute(
        """SELECT id, email, display_name, role, scope, active, locked_until, last_login_at,
                  (SELECT count(*) FROM review_sessions s WHERE s.user_id = u.id) AS sessions
             FROM review_users u ORDER BY id"""
    ).fetchall()
