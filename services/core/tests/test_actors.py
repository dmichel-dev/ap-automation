"""Who acted (actors.py): automation calling the core API can never record a decision under a person's
name, and a person can't take an automation name. Real Postgres for the API checks."""

from typing import Any

import pytest
from app import actors, db
from app.main import app
from app.web import accounts
from fastapi.testclient import TestClient

from .conftest import AUTH, AUTOMATION, query
from .test_processing import env, post  # noqa: F401  (env is a fixture)
from .test_review import new  # noqa: F401  (a fixture)

c = TestClient(app)

PEOPLE = (
    "David Michel",
    "Rita Reviewer",
    "Rita Reviewer (review user 1)",  # exactly what the review page records for a person
    "claude-code (review user 1)",  # an automation name dressed as a person's account
    "david",
    "claude-code-bot",
    "e2e-test (two\nlines)",
    "claude-code ((nested))",
)
# Every decision the bearer-token API accepts, with a body that is otherwise valid
DECISIONS: tuple[tuple[str, dict[str, Any]], ...] = (
    ("approve", {"account_id": "20"}),
    ("verify", {"note": "Called the number on file"}),
    ("correct", {"changes": {"total": "641.24"}}),
    ("reject", {"note": "Not an invoice from this supplier"}),
    ("confirm-match", {"bank_transaction_id": "t1"}),
    ("reject-match", {}),
    ("write-off", {}),
    ("keep-open", {}),
    ("retry", {}),
)


@pytest.mark.parametrize(
    "label", [AUTOMATION, "claude-code", "e2e-test", "claude-code (test run, authorised by David Michel)"]
)
def test_automation_labels_are_accepted(label):
    assert actors.is_automation(label) and actors.require_automation(label) == label


@pytest.mark.parametrize("label", PEOPLE)
def test_a_person_s_name_is_not_an_automation_label(label):
    assert not actors.is_automation(label)
    with pytest.raises(actors.NotAutomation):
        actors.require_automation(label)


def test_a_person_label_is_never_an_automation_label():
    assert not actors.is_automation(actors.person_label("claude-code", 1))


def audit_trail(url):
    """Everything a decision could write: the review item, the log, write-offs and supplier defaults."""
    return [
        query(url, f"SELECT * FROM ap.{table} ORDER BY 1")
        for table in ("review_queue", "processing_log", "write_offs", "supplier_defaults", "invoices")
    ]


@pytest.mark.parametrize("action, body", DECISIONS)
@pytest.mark.parametrize("name", PEOPLE)
def test_the_api_never_records_a_decision_under_a_person_s_name(new, action, body, name):  # noqa: F811
    _state, cfg = new
    review_id = post().json()["review_id"]
    url = cfg.database_url
    with db.connect(url) as conn:  # a real person with that name exists
        accounts.add_user(conn, "rita@example.test", "Rita Reviewer", "reviewer", "live", "a long test passphrase 1")
        conn.commit()
    before = audit_trail(url)
    r = c.post(f"/review/items/{review_id}/{action}", headers=AUTH, json={"reviewer": name, **body})
    assert r.status_code == 422, r.text
    assert audit_trail(url) == before


def test_the_same_decision_under_an_automation_label_is_recorded_as_sent(new):  # noqa: F811
    _state, cfg = new
    review_id = post().json()["review_id"]
    label = "claude-code (test run, authorised by David Michel)"
    r = c.post(f"/review/items/{review_id}/approve", headers=AUTH, json={"reviewer": label, "account_id": "20"})
    assert r.status_code == 200, r.text
    assert query(cfg.database_url, "SELECT reviewer FROM ap.review_queue WHERE id = %s", review_id) == [
        {"reviewer": label}
    ]


@pytest.mark.parametrize("name", ["claude-code", "Claude-Code (helper)", "e2e-test runner"])
def test_a_person_cannot_take_an_automation_name(cfg, name):
    with db.connect(cfg.database_url) as conn, pytest.raises(accounts.UserError):
        accounts.add_user(conn, "x@example.test", name, "reviewer", "live", "a long test passphrase 1")
