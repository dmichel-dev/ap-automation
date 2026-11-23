"""Email intake screening (pure): which emails and which attachments go to processing.

The n8n workflow sends the email's metadata (recipients, attachment types, names and sizes,
never the content) and splits the email according to the answer. Deterministic code only.
"""

import re
from dataclasses import dataclass, field

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
_SIZE_UNITS = {"b": 1, "kb": 1000, "mb": 1000**2, "gb": 1000**3}


@dataclass(frozen=True)
class Attachment:
    key: str  # n8n binary field, e.g. attachment_0
    mime_type: str | None
    file_name: str | None
    size: str | int | None  # n8n stores a human-readable size ("45.2 kB")


@dataclass
class Screening:
    route: str  # process | no_valid_attachment | not_for_alias
    reason: str | None = None
    accepted: list[dict[str, str | int]] = field(default_factory=list)  # key, index, mime, size
    skipped: list[dict[str, str | int]] = field(default_factory=list)  # type, size, why
    attachments: int = 0


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


def screen_email(recipients: list[str], delivered_to: str, attachments: list[Attachment], alias: str) -> Screening:
    """Second guard after the Gmail search (the alias must be a recipient), then the attachments
    worth processing. An email without one is never silently ignored: it gets a route and a reason."""
    alias = alias.lower()
    if alias not in [r.lower() for r in recipients] and alias not in delivered_to.lower():
        return Screening("not_for_alias", "not_addressed_to_alias")
    files = sorted((a for a in attachments if a.key.startswith("attachment_")), key=lambda a: _index(a.key))
    screening = Screening("process", attachments=len(files))
    for a in files:
        mime, size = _mime(a), size_in_bytes(a.size)
        if not mime:
            screening.skipped.append({"type": a.mime_type or "unknown", "size": size, "why": "unsupported_type"})
        elif mime.startswith("image/") and size < MIN_IMAGE_BYTES:
            screening.skipped.append({"type": mime, "size": size, "why": "small_image"})
        else:
            screening.accepted.append({"key": a.key, "index": _index(a.key), "mime": mime, "size": size})
    if not screening.accepted:
        screening.route, screening.reason = "no_valid_attachment", "no_valid_attachment"
    return screening
