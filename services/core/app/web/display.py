"""How the pages present stored values: the confidence badge, an item's age, each status's words and
colour, reason messages written before the current wording. Display only: nothing here changes a stored
value or a decision. Number and date formats are the core's (formatting.py)."""

import re
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal, NamedTuple

from ..formatting import below_threshold, day, mismatch, percent

Level = Literal["high", "medium", "low", "unknown"]
Tone = Literal["review", "fraud", "ready", "closed"]
SCOPE_LABELS = {"live": "live data", "e2e": "end-to-end test data", "demo": "demo data"}
# How an invoice came in, after "Received" / "received", on every page (review page, demo cards)
CHANNELS = {"upload": "by upload", "email": "by email", "drive": "from Google Drive"}
LEVEL_LABELS: dict[Level, str] = {
    "high": "high",
    "medium": "close to the threshold",
    "low": "below the threshold",
    "unknown": "not available",
}


class Status(NamedTuple):
    label: str
    tone: Tone  # green "ready", amber "review", red "fraud", grey "closed"


# One wording and one colour per status, on every page (review page and list, demo pages)
STATUSES: dict[str, Status] = {
    # What the checks decided for an invoice
    "ready": Status("Ready to post", "ready"),
    "needs_review": Status("Needs review", "review"),
    "rejected": Status("Rejected", "closed"),
    # A review item
    "open": Status("Needs review", "review"),
    "fraud": Status("Possible fraud", "fraud"),
    "refused": Status("QuickBooks refused", "fraud"),
    "waiting": Status("Waiting for QuickBooks", "closed"),
    "approved": Status("Approved", "ready"),
    "verified": Status("Verified", "ready"),
    "corrected": Status("Corrected", "closed"),
    "written_off": Status("Written off", "closed"),
    # A bill in QuickBooks, a file on the demo page
    "posted": Status("Posted", "ready"),
    "in_progress": Status("In progress", "closed"),
    "not_processed": Status("Not processed", "closed"),
}


class ConfidenceBadge(NamedTuple):
    percent: str  # "97%", or "–" when there is no score
    level: Level
    label: str  # says in words what the colour says
    threshold: str  # the review threshold, e.g. "85%"
    high_from: str


def confidence_badge(score: Any, threshold: float, high: float) -> ConfidenceBadge:
    """Green from `high`, orange from the review threshold up to `high`, red below the threshold (the
    same threshold the core routes on). Compared as decimals, so 0.95 is exactly the green boundary."""
    level: Level
    if score is None:
        level = "unknown"
    elif Decimal(str(score)) >= Decimal(str(high)):
        level = "high"
    elif Decimal(str(score)) >= Decimal(str(threshold)):
        level = "medium"
    else:
        level = "low"
    shown = "–" if score is None else percent(score)
    return ConfidenceBadge(shown, level, LEVEL_LABELS[level], percent(threshold), percent(high))


def data_label(environment: str, scope: str) -> tuple[str, str]:
    """(words, css class) for the header: which data the signed-in person is looking at. A test
    environment says "test data" whatever the account's scope, so test data never reads as live."""
    if environment == "test":
        return "test data", "data-test"
    return SCOPE_LABELS.get(scope, f"{scope} data"), f"data-{scope}"


def channel(code: str | None) -> str:
    """ "by email" for "email"; an unknown channel is named as stored."""
    return CHANNELS.get(code or "", f"by {code}" if code else "")


def age(created_at: str, now: datetime | None = None) -> str:
    """How long ago an item was opened, in days: "today", "1 day", "12 days"."""
    opened = datetime.fromisoformat(created_at)
    days = ((now or datetime.now(UTC)) - opened).days
    if days <= 0:
        return "today"
    return "1 day" if days == 1 else f"{days} days"


def item_status(item: dict[str, Any]) -> str:
    """A review item's status in STATUSES: an open item is waiting for QuickBooks, refused by it, a
    possible fraud (only a fraud item can be verified) or plainly needs review."""
    if item["status"] != "open":
        return str(item["status"])
    if item.get("waiting_for"):
        return "waiting"
    if item.get("effect_error"):
        return "refused"
    return "fraud" if "verify" in item["actions"] else "open"


def tone(item: dict[str, Any]) -> Tone:
    return STATUSES.get(item_status(item), STATUSES["rejected"]).tone


WAITING_LABELS = {
    "posting": "Waiting for QuickBooks: bill posting",
    "payment": "Waiting for QuickBooks: bill payment",
    "write_off": "Waiting for QuickBooks: write-off",
}


def status_label(item: dict[str, Any]) -> str:
    status = item_status(item)
    if status == "waiting":
        return WAITING_LABELS.get(str(item["waiting_for"]), STATUSES["waiting"].label)
    return label(status)


def label(status: str) -> str:
    known = STATUSES.get(status)
    return known.label if known else status.replace("_", " ").capitalize()


# =====================================================================================
# Reason messages stored before the current wording, shown in it (the stored text is left as it was)
# =====================================================================================
_OLD_MISMATCH = re.compile(
    r"^(subtotal|tax total|total) (-?\d+(?:\.\d+)?) ≠ (?:sum of lines|sum of tax lines|lines \+ adjustments \+ tax) "
    r"(-?\d+(?:\.\d+)?)$"
)
_OLD_MISMATCH_KIND = {"subtotal": "lines", "tax total": "tax", "total": "total"}
_OLD_CONFIDENCE = re.compile(r"^confidence (\d+(?:\.\d+)?) below threshold (\d+(?:\.\d+)?)$")
_ISO_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")


def reason_detail(message: str | None, currency: str | None = None) -> str:
    """A reason's stored message as shown: an older "total 875.13 ≠ … 865.13" or "confidence 0.71 below
    threshold 0.85" in today's sentence, and any ISO date as 26 Nov 2026."""
    if not message:
        return ""
    if found := _OLD_MISMATCH.match(message):
        return mismatch(_OLD_MISMATCH_KIND[found[1]], Decimal(found[2]), Decimal(found[3]), currency)
    if found := _OLD_CONFIDENCE.match(message):
        return below_threshold(found[1], found[2])
    return _ISO_DATE.sub(lambda d: day(d[0]), message)
