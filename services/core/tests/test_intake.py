"""Email screening: the alias guard and which attachments are processed."""

import pytest
from app.intake import Attachment, screen_email, size_in_bytes
from app.main import app
from fastapi.testclient import TestClient

from .conftest import AUTH

ALIAS = "invoices@example.test"


def att(i: int, mime: str | None, name: str = "x.pdf", size: str | int | None = "45.2 kB") -> Attachment:
    return Attachment(f"attachment_{i}", mime, name, size)


@pytest.mark.parametrize(
    ("size", "expected"),
    [("45.2 kB", 45200), ("1.5 MB", 1500000), ("900 B", 900), (12345, 12345), (None, 0), ("big", 0)],
)
def test_size_in_bytes(size, expected):
    assert size_in_bytes(size) == expected


def test_not_for_alias():
    s = screen_email(["someone@example.test"], "other@example.test", [att(0, "application/pdf")], ALIAS)
    assert (s.route, s.reason, s.accepted) == ("not_for_alias", "not_addressed_to_alias", [])


def test_alias_found_in_delivered_to_header_and_case_insensitive():
    assert screen_email([], f"<{ALIAS.upper()}>", [att(0, "application/pdf")], ALIAS).route == "process"
    assert screen_email(["Invoices@Example.TEST"], "", [att(0, "application/pdf")], ALIAS).route == "process"


def test_attachments_sorted_and_filtered():
    s = screen_email(
        [ALIAS],
        "",
        [
            att(10, "application/pdf", size="20 kB"),
            att(2, "image/png", "logo.png", size="4 kB"),  # signature logo
            att(1, "application/octet-stream", "scan.JPG", size="300 kB"),  # type from the extension
            att(0, "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "a.docx"),
        ],
        ALIAS,
    )
    assert s.route == "process" and s.attachments == 4
    assert [(a["index"], a["mime"]) for a in s.accepted] == [(1, "image/jpeg"), (10, "application/pdf")]
    assert [x["why"] for x in s.skipped] == ["unsupported_type", "small_image"]


def test_no_valid_attachment_is_reported_not_ignored():
    s = screen_email([ALIAS], "", [att(0, "image/png", "logo.png", size="3 kB")], ALIAS)
    assert (s.route, s.reason, s.attachments, len(s.skipped)) == ("no_valid_attachment", "no_valid_attachment", 1, 1)


def test_screen_endpoint_uses_the_configured_alias(monkeypatch):
    monkeypatch.setenv("INTAKE_EMAIL_ALIAS", ALIAS)
    from app.config import get_settings

    get_settings.cache_clear()
    body = {"recipients": [ALIAS], "attachments": [{"key": "attachment_0", "mime_type": "application/pdf", "size": 1}]}
    r = TestClient(app).post("/intake/email/screen", headers=AUTH, json=body)
    get_settings.cache_clear()
    assert r.status_code == 200 and r.json()["route"] == "process"
    assert TestClient(app).post("/intake/email/screen", json=body).status_code == 401


# --- bank-detail change requests without an invoice attached -------------------------------
def test_bank_change_email_without_attachment_is_a_fraud_alert():
    s = screen_email([ALIAS], "", [], ALIAS, text="URGENT: new bank details\n\nPlease update our bank details.")
    assert (s.route, s.reason) == ("bank_detail_notice", "possible_bank_detail_change")


@pytest.mark.parametrize(
    "text",
    ["Bitte beachten Sie unsere neue Bankverbindung.", "Veuillez noter nos nouvelles coordonnées bancaires."],
)
def test_bank_change_email_in_other_languages(text):
    assert screen_email([ALIAS], "", [att(0, "image/png", size="3 kB")], ALIAS, text=text).route == "bank_detail_notice"


def test_ordinary_email_without_attachment_is_still_no_valid_attachment():
    s = screen_email([ALIAS], "", [], ALIAS, text="Hello, the invoice follows tomorrow.")
    assert s.route == "no_valid_attachment"


def test_bank_wording_does_not_stop_an_attachment_from_being_processed():
    s = screen_email([ALIAS], "", [att(0, "application/pdf")], ALIAS, text="Please update our bank details.")
    assert s.route == "process"  # the invoice itself is flagged by the processing


def test_a_bank_change_email_not_for_the_alias_is_not_processed():
    s = screen_email(["x@example.test"], "", [], ALIAS, text="Please update our bank details.")
    assert s.route == "not_for_alias"


def test_screen_endpoint_reads_subject_and_text(monkeypatch):
    monkeypatch.setenv("INTAKE_EMAIL_ALIAS", ALIAS)
    from app.config import get_settings

    get_settings.cache_clear()
    body = {"recipients": [ALIAS], "attachments": [], "text": "URGENT\n\nPlease update our bank details."}
    r = TestClient(app).post("/intake/email/screen", headers=AUTH, json=body)
    get_settings.cache_clear()
    assert r.status_code == 200 and r.json()["route"] == "bank_detail_notice"
    too_long = TestClient(app).post("/intake/email/screen", headers=AUTH, json=body | {"text": "x" * 20001})
    assert too_long.status_code == 422
