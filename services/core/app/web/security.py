"""Passwords, tokens and the security policy of the review page (documented in docs/security.md)."""

import hashlib
import hmac
import secrets
import threading
from datetime import timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

# --- policy ---------------------------------------------------------------------------
SESSION_IDLE = timedelta(minutes=30)
SESSION_MAX = timedelta(hours=8)
MAX_FAILED_LOGINS = 5  # then the account is locked
LOCK_FIRST = timedelta(minutes=15)  # doubles at each new lock, up to LOCK_MAX
LOCK_MAX = timedelta(hours=4)
IP_WINDOW = timedelta(minutes=10)
IP_MAX_ATTEMPTS = 20  # sign-in attempts per client address and window
DECISIONS_PER_MINUTE = 60  # per session: stops a runaway script, never reached by a person
PASSWORD_MIN, PASSWORD_MAX = 12, 128
ATTEMPTS_KEPT = timedelta(days=30)

# Argon2id with OWASP's minimum parameters (19 MiB, 2 passes): memory stays small in a lean
# container. At most two hashes run at once, so a burst of sign-ins can't exhaust memory.
_hasher = PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1)
_slots = threading.BoundedSemaphore(2)
# Compared against when the account doesn't exist (or can't sign in here), so the response
# takes the same time and doesn't reveal which addresses have an account
_DUMMY_HASH = _hasher.hash(secrets.token_urlsafe(24))


class PasswordPolicyError(ValueError):
    pass


def check_password_policy(password: str, email: str) -> None:
    if not PASSWORD_MIN <= len(password) <= PASSWORD_MAX:
        raise PasswordPolicyError(f"the password must have {PASSWORD_MIN} to {PASSWORD_MAX} characters")
    if password.strip().lower() == email.strip().lower():
        raise PasswordPolicyError("the password can't be the email address")


def hash_password(password: str) -> str:
    with _slots:
        return _hasher.hash(password)


def verify_password(stored_hash: str | None, password: str) -> bool:
    """True only for a real hash that matches. Always does the full computation."""
    with _slots:
        try:
            _hasher.verify(stored_hash or _DUMMY_HASH, password[:PASSWORD_MAX])
        except (VerificationError, InvalidHashError):
            return False
    return stored_hash is not None


def needs_rehash(stored_hash: str) -> bool:
    return _hasher.check_needs_rehash(stored_hash)


def new_token() -> str:
    """256 random bits, URL-safe."""
    return secrets.token_urlsafe(32)


def token_digest(token: str) -> bytes:
    """What the database stores for a session token: a stolen copy of the table can't be used as a cookie."""
    return hashlib.sha256(token.encode()).digest()


def same_token(a: str | None, b: str | None) -> bool:
    return bool(a) and bool(b) and hmac.compare_digest(str(a).encode(), str(b).encode())


def email_digest(email: str) -> str:
    """Sign-in attempts store this instead of the typed address (it may be someone else's)."""
    return hashlib.sha256(normalize_email(email).encode()).hexdigest()[:32]


def normalize_email(email: str) -> str:
    return email.strip().lower()[:254]
