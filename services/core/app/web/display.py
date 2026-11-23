"""How the review page presents stored values: the confidence badge, an item's age, its status colour.
Display only: nothing here changes a stored value or a decision."""

from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Literal, NamedTuple

Level = Literal["high", "medium", "low", "unknown"]
Tone = Literal["review", "fraud", "ready", "closed"]
SCOPE_LABELS = {"live": "live data", "e2e": "end-to-end test data", "demo": "demo data"}
LEVEL_LABELS: dict[Level, str] = {
    "high": "high",
    "medium": "close to the threshold",
    "low": "below the threshold",
    "unknown": "not available",
}
STATUS_LABELS = {
    "open": "Needs review",
    "approved": "Approved",
    "corrected": "Corrected",
    "verified": "Verified",
    "rejected": "Rejected",
    "written_off": "Written off",
}


class ConfidenceBadge(NamedTuple):
    percent: str  # "97.75%", or "–" when there is no score
    level: Level
    label: str  # says in words what the colour says
    threshold: str  # the review threshold, e.g. "85.00%"
    high_from: str


def percent(value: Any) -> str:
    """0.9775 → "97.75%" (two decimals, halves rounded up)."""
    return f"{(Decimal(str(value)) * 100).quantize(Decimal('0.01'), ROUND_HALF_UP)}%"


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


def age(created_at: str, now: datetime | None = None) -> str:
    """How long ago an item was opened, in days: "today", "1 day", "12 days"."""
    opened = datetime.fromisoformat(created_at)
    days = ((now or datetime.now(UTC)) - opened).days
    if days <= 0:
        return "today"
    return "1 day" if days == 1 else f"{days} days"


def tone(item: dict[str, Any]) -> Tone:
    """The status colour: amber needs review, red possible fraud (only a fraud item can be verified) or a
    QuickBooks call that failed, grey while waiting for QuickBooks, green approved or verified."""
    if item["status"] == "open":
        if item.get("waiting_for"):
            return "closed"
        return "fraud" if "verify" in item["actions"] or item.get("effect_error") else "review"
    return "ready" if item["status"] in ("approved", "verified") else "closed"


WAITING_LABELS = {
    "posting": "Waiting for QuickBooks: bill posting",
    "payment": "Waiting for QuickBooks: bill payment",
    "write_off": "Waiting for QuickBooks: write-off",
}


def status_label(item: dict[str, Any]) -> str:
    if item["status"] == "open" and item.get("waiting_for"):
        return WAITING_LABELS.get(str(item["waiting_for"]), "Waiting for QuickBooks")
    if item["status"] == "open" and item.get("effect_error"):
        return "QuickBooks refused"
    if item["status"] == "open" and "verify" in item["actions"]:
        return "Possible fraud"
    status = str(item["status"])
    return STATUS_LABELS.get(status, status.replace("_", " ").capitalize())
