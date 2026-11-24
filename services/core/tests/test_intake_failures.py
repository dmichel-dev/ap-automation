"""Failed emails (intake_failures.py): temporary or permanent, the re-check's attempts and backoff, and
emails labelled AP/Error that the core never heard of (it was unreachable when they failed).
Real Postgres; the workflow's reports are sent as n8n sends them."""

from datetime import timedelta

import pytest
from app import intake_failures
from app.main import app
from fastapi.testclient import TestClient

from .conftest import AUTH, query

c = TestClient(app)
MINUTE = timedelta(minutes=1)


def report(message="m1", stage="process", status=503, error="provider_timeout", run="1"):
    body = {"message_id": message, "stage": stage, "status_code": status, "error": error, "execution_id": run}
    r = c.post("/intake/failures", headers=AUTH, json=body)
    assert r.status_code == 200, r.text
    return r.json()


def due(*found, run="recheck"):
    r = c.post("/intake/failures/due", headers=AUTH, json={"found": list(found), "execution_id": run})
    assert r.status_code == 200, r.text
    return r.json()["due"]


def failure(url, message="m1"):
    return query(url, "SELECT * FROM ap.intake_failures WHERE message_id = %s", message)[0]


def time_passes(url):
    """Every waiting email's next attempt is now due."""
    query(url, "UPDATE ap.intake_failures SET next_attempt_at = now() - interval '1 second' WHERE state = 'waiting'")


def wait_after(row):
    return row["next_attempt_at"] - row["last_failed_at"]


@pytest.mark.parametrize(
    "stage, status, error, code, permanent",
    [
        ("process", 503, "provider_timeout", "provider_timeout", False),
        ("process", 503, "provider_rate_limit", "provider_rate_limit", False),
        ("process", 503, "provider_server", "provider_server", False),
        ("process", 502, "provider_auth", "provider_auth", True),
        ("process", 502, "provider_bad_request", "provider_bad_request", True),
        ("process", 502, "provider_invalid_response", "provider_invalid_response", True),
        ("process", None, None, "core_unreachable", False),  # no answer: stopped, restarting, timed out
        ("screen", 0, None, "core_unreachable", False),
        ("process", 500, None, "core_error", False),  # e.g. its database restarting
        ("screen", 503, None, "core_error", False),
        ("process", 200, None, "core_error", False),  # an answer the workflow could not route
        ("process", 500, "provider_auth", "core_error", False),  # a provider code counts only with 502/503
        ("process", 401, None, "core_auth", True),  # n8n's token for the core is wrong
        ("process", 422, None, "core_refused_422", True),
        ("fetch", None, None, "email_fetch_failed", False),
    ],
)
def test_classify(stage, status, error, code, permanent):
    f = intake_failures.classify(stage, status, error)
    assert (f.code, f.permanent) == (code, permanent) and f.explanation


def test_a_temporary_failure_is_retried_after_15_minutes(cfg):
    answer = report()
    assert answer == {**answer, "state": "waiting", "attempts": 1, "max_attempts": 5, "code": "provider_timeout"}
    row = failure(cfg.database_url)
    assert 14 * MINUTE < wait_after(row) <= 15 * MINUTE
    assert due("m1") == []  # not before its time
    log = query(cfg.database_url, "SELECT * FROM ap.processing_log WHERE step = 'intake_retry'")[-1]
    assert log["level"] == "warning" and log["source_ref"] == "gmail:m1" and "attempt 1 of 5" in log["message"]


def test_several_attachments_failing_in_one_run_count_as_one_attempt(cfg):
    report(run="1")
    assert report(run="1")["attempts"] == 1
    assert report(run="2")["attempts"] == 2


@pytest.mark.parametrize("error", ["provider_auth", "provider_bad_request"])
def test_a_permanent_failure_is_given_up_at_once(cfg, error):
    answer = report(status=502, error=error)
    assert answer["state"] == "given_up" and answer["next_attempt_at"] is None
    time_passes(cfg.database_url)
    assert due("m1") == []  # still labelled AP/Error, never tried again on its own
    log = query(cfg.database_url, "SELECT level, message FROM ap.processing_log WHERE step = 'intake_retry'")[-1]
    assert log["level"] == "error" and "given up" in log["message"]


def test_five_attempts_over_about_17_hours_then_given_up(cfg):
    url, waits = cfg.database_url, []
    report(run="1")  # the first attempt: the new-email trigger
    for attempt in range(2, 6):
        waits.append(wait_after(failure(url)))
        time_passes(url)
        assert due("m1", run=str(attempt)) == ["m1"]
        answer = report(run=str(attempt))
    assert answer == {**answer, "state": "given_up", "attempts": 5}
    rounded = [round(w / MINUTE) for w in waits]
    assert rounded == [15, 60, 240, 720]  # 15 min, 1 h, 4 h, 12 h: about 17 h in all
    time_passes(url)
    assert due("m1") == []


def test_an_ap_error_email_without_a_row_is_due_at_once(cfg):
    """The core was unreachable when the email failed, so nothing was recorded; only the label says so."""
    assert due("m9") == ["m9"]
    row = failure(cfg.database_url, "m9")
    assert (row["state"], row["attempts"], row["last_error_code"]) == ("waiting", 1, "not_recorded")
    assert due("m9") == []  # counted: the next try waits like any other
    logged = query(cfg.database_url, "SELECT message FROM ap.processing_log WHERE source_ref = 'gmail:m9'")
    assert "attempt 1 of 5" in logged[-1]["message"]


def test_a_waiting_email_whose_label_was_lost_is_still_retried(cfg):
    report()
    time_passes(cfg.database_url)
    assert due() == ["m1"]  # Gmail no longer lists it under AP/Error: the core's row is what counts


def test_a_run_that_dies_without_reporting_cannot_retry_forever(cfg):
    for _ in range(5):
        assert due("m1") == ["m1"]
        time_passes(cfg.database_url)
    assert due("m1") == []
    assert failure(cfg.database_url)["state"] == "given_up"


def test_given_up_and_resolved_emails_are_not_due(cfg):
    report("m1", status=502, error="provider_auth")
    report("m2")
    query(cfg.database_url, "UPDATE ap.intake_failures SET state = 'resolved' WHERE message_id = 'm2'")
    time_passes(cfg.database_url)
    assert due("m1", "m2") == []


def test_operator_retry_after_a_fix(cfg):
    report("m1", status=502, error="provider_auth")
    report("m2", status=502, error="provider_auth")
    report("m3", status=502, error="provider_bad_request")
    r = c.post("/intake/failures/retry", headers=AUTH, json={"error_code": "provider_auth", "note": "new API key"})
    assert r.status_code == 200 and sorted(r.json()["retried"]) == ["m1", "m2"]
    assert failure(cfg.database_url)["attempts"] == 0 and failure(cfg.database_url)["operator_retries"] == 1
    assert sorted(due()) == ["m1", "m2"]
    assert c.post("/intake/failures/retry", headers=AUTH, json={}).status_code == 422


def test_an_email_with_nothing_to_process_ends_its_recheck(cfg):
    report()
    body = {"recipients": ["invoices@davidmichel.dev"], "attachments": [], "message_id": "m1", "execution_id": "2"}
    assert c.post("/intake/email/screen", headers=AUTH, json=body).json()["route"] == "no_valid_attachment"
    assert failure(cfg.database_url)["state"] == "resolved"


def test_summary_for_the_daily_report(cfg):
    report("m1", status=502, error="provider_bad_request")
    report("m2")
    report("m3")
    query(
        cfg.database_url,
        "UPDATE ap.intake_failures SET state = 'resolved', resolved_at = now() WHERE message_id = 'm3'",
    )
    query(
        cfg.database_url,
        "INSERT INTO ap.processing_log (step, level, message) VALUES ('workflow_error', 'error', 'workflow failed')",
    )
    s = c.get("/intake/failures/summary", headers=AUTH).json()
    assert [(g["message_id"], g["code"]) for g in s["given_up"]] == [("m1", "provider_bad_request")]
    assert "new copy" in s["given_up"][0]["explanation"]
    assert (s["retrying"], s["recovered"], s["workflow_errors"]) == (1, 1, 1)
