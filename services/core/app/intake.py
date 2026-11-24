"""Email intake screening (pure): which emails and which attachments go to processing, and whose data
they are (scope).

The n8n workflow sends the email's metadata (recipients, attachment types, names and sizes, never the
files) and its text (subject + body, checked for bank-change wording, never stored), and splits the
email according to the answer. Deterministic code only.

Scope: mail to the alias is live. Mail to its "+e2e" sub-address is the daily end-to-end test's only
if its subject names a run ("[E2E run 12]"), and the core then checks that this run is in progress
(e2e_test.confirm_scope). Anything else sent there is processed as live, so a real invoice sent to
that address is never hidden among test data.
"""

import re
from dataclasses import dataclass, field

from .bank_details import change_wording

SUPPORTED_MIME = frozenset({"application/pdf", "image/png", "image/jpeg", "image/webp", "image/tiff"})
MIME_BY_EXTENSION = {
    "pdf": "application/pdf",
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
    "tif": "image/tiff",
    "tiff": "image/tiff",
}
MIN_IMAGE_BYTES = 15000  # smaller images are almost always signature logos
EMAIL_TEXT_MAX = 20000  # characters of subject + body that n8n sends
_SIZE_UNITS = {"b": 1, "kb": 1000, "mb": 1000**2, "gb": 1000**3}
E2E_TAG = "e2e"  # invoices+e2e@…: the daily end-to-end test's address
E2E_MARKER = re.compile(r"\[E2E run (\d{1,12})\]")


@dataclass(frozen=True)
class Attachment:
    key: str  # n8n binary field, e.g. attachment_0
    mime_type: str | None
    file_name: str | None
    size: str | int | None  # n8n stores a human-readable size ("45.2 kB")


@dataclass
class Screening:
    route: str  # process | no_valid_attachment | bank_detail_notice | not_for_alias
    reason: str | None = None
    accepted: list[dict[str, str | int]] = field(default_factory=list)  # key, index, mime, size
    skipped: list[dict[str, str | int]] = field(default_factory=list)  # type, size, why
    attachments: int = 0
    scope: str = "live"  # live | e2e
    e2e_run: int | None = None  # the end-to-end run the subject names (confirmed by the core)


def size_in_bytes(size: str | int | None) -> int:
    """'45.2 kB' → 45200; an int is already in bytes; anything unreadable → 0."""
    if isinstance(size, int):
        return size
    m = re.search(r"([\d.]+)\s*(B|kB|MB|GB)", str(size or ""), re.IGNORECASE)
    return round(float(m.group(1)) * _SIZE_UNITS[m.group(2).lower()]) if m else 0


def _index(key: str) -> int:
    return int(key.split("_")[1])


def _mime(a: Attachment) -> str | None:
    """The declared type if supported, otherwise the type of the file extension (if supported)."""
    if a.mime_type in SUPPORTED_MIME:
        return a.mime_type
    ext = (a.file_name or "").rsplit(".", 1)[-1].lower()
    return MIME_BY_EXTENSION.get(ext)


def sub_address(alias: str, tag: str) -> str:
    """invoices@example.com + e2e → invoices+e2e@example.com"""
    local, domain = alias.lower().split("@", 1)
    return f"{local}+{tag}@{domain}"


def _addressed_to(address: str, recipients: list[str], delivered_to: str) -> bool:
    return address in [r.lower() for r in recipients] or address in delivered_to.lower()


def addressed_scope(
    recipients: list[str], delivered_to: str, alias: str, subject: str
) -> tuple[str, int | None] | None:
    """(scope, end-to-end run named in the subject), or None when the email isn't for the alias."""
    if _addressed_to(alias.lower(), recipients, delivered_to):
        return "live", None
    if not _addressed_to(sub_address(alias, E2E_TAG), recipients, delivered_to):
        return None
    marker = E2E_MARKER.search(subject)
    return ("e2e", int(marker.group(1))) if marker else ("live", None)


def screen_email(
    recipients: list[str],
    delivered_to: str,
    attachments: list[Attachment],
    alias: str,
    text: str = "",
    subject: str = "",
) -> Screening:
    """Second guard after the Gmail search (the alias must be a recipient), then the attachments
    worth processing. An email without one is never silently ignored: it gets a route and a reason.
    Without one, an email asking to change bank details is a fraud alert (with one, the processing
    flags each invoice)."""
    addressed = addressed_scope(recipients, delivered_to, alias, subject)
    if addressed is None:
        return Screening("not_for_alias", "not_addressed_to_alias")
    files = sorted((a for a in attachments if a.key.startswith("attachment_")), key=lambda a: _index(a.key))
    screening = Screening("process", attachments=len(files), scope=addressed[0], e2e_run=addressed[1])
    for a in files:
        mime, size = _mime(a), size_in_bytes(a.size)
        if not mime:
            screening.skipped.append({"type": a.mime_type or "unknown", "size": size, "why": "unsupported_type"})
        elif mime.startswith("image/") and size < MIN_IMAGE_BYTES:
            screening.skipped.append({"type": mime, "size": size, "why": "small_image"})
        else:
            screening.accepted.append({"key": a.key, "index": _index(a.key), "mime": mime, "size": size})
    if not screening.accepted and change_wording(text):
        screening.route, screening.reason = "bank_detail_notice", "possible_bank_detail_change"
    elif not screening.accepted:
        screening.route, screening.reason = "no_valid_attachment", "no_valid_attachment"
    return screening
