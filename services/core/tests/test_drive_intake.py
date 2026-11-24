"""Google Drive intake (drive_intake.py): which files in the Inbox folder are processed now, wait, or go
to Error; leases against overlapping runs; failures retried with the email backoff; and the same file
by email and by Drive caught as a duplicate. Real Postgres; requests as the n8n workflow sends them."""

import pytest
from app import drive_intake, files
from app.main import app
from app.providers.base import ProviderError
from fastapi.testclient import TestClient

from .conftest import AUTH, query
from .test_intake_failures import time_passes
from .test_processing import FakeProvider, env, post  # noqa: F401  (env is a fixture)

c = TestClient(app)
PDF = {"id": "f1", "name": "invoice.pdf", "mime_type": "application/pdf", "size": "52000"}


@pytest.fixture
def drive(env, cfg):  # noqa: F811  (env is a fixture)
    """Processing and core settings on the test database."""
    return env


def screen(*listed, run="1"):
    r = c.post("/intake/drive/screen", headers=AUTH, json={"files": list(listed), "execution_id": run})
    assert r.status_code == 200, r.text
    return {a["id"]: (a["route"], a["reason"]) for a in r.json()["files"]}


def expire_leases(url):
    """The runs that leased files died (or finished without moving them)."""
    query(url, "UPDATE ap.intake_leases SET leased_until = now() - interval '1 second'")


def drive_file(file_id, **fields):
    return {**PDF, "id": file_id, **fields}


def report(file_id, status=503, error="provider_timeout", run="1", stage="process"):
    body = {
        "channel": "drive",
        "message_id": file_id,
        "stage": stage,
        "status_code": status,
        "error": error,
        "execution_id": run,
    }
    r = c.post("/intake/failures", headers=AUTH, json=body)
    assert r.status_code == 200, r.text
    return r.json()


def post_drive(file_id, content=b"%PDF-1.4 invoice A", **form):
    return post(
        content=content, source_ref=drive_intake.source_ref(file_id), channel="drive", message_id=file_id, **form
    )


def test_pdf_and_image_are_processed(drive):
    answers = screen(PDF, drive_file("f2", name="scan.JPG", mime_type="application/octet-stream", size="90000"))
    assert answers == {"f1": ("process", None), "f2": ("process", None)}


def test_unsupported_and_too_large_go_to_error_and_are_logged(drive):
    _state, cfg = drive
    doc = drive_file("f2", name="notes", mime_type="application/vnd.google-apps.document", size=None)
    big = drive_file("f3", size=str(files.MAX_FILE_BYTES + 1))
    answers = screen(drive_file("f1", name="invoice.docx", mime_type="application/msword"), doc, big)
    assert answers == {
        "f1": ("error", "unsupported_type"),
        "f2": ("error", "unsupported_type"),
        "f3": ("error", "file_too_large"),
    }
    logs = query(cfg.database_url, "SELECT channel, source_ref, level, details FROM ap.processing_log ORDER BY id")
    assert [(r["channel"], r["source_ref"], r["level"]) for r in logs] == [
        ("drive", "drive:f1", "warning"),
        ("drive", "drive:f2", "warning"),
        ("drive", "drive:f3", "warning"),
    ]
    assert all("invoice" not in str(r["details"]) for r in logs)  # never the file name


def test_a_leased_file_is_not_handed_out_again(drive):
    _state, cfg = drive
    assert screen(PDF, run="1") == {"f1": ("process", None)}
    assert screen(PDF, run="2") == {"f1": ("wait", "in_progress")}  # run 1 is still processing it
    expire_leases(cfg.database_url)
    assert screen(PDF, run="3") == {"f1": ("process", None)}  # run 1 died: handed out again


def test_expired_leases_are_removed(drive):
    _state, cfg = drive
    screen(PDF, run="1")  # f1 processed and moved out of Inbox
    expire_leases(cfg.database_url)
    assert screen(run="2") == {}
    assert query(cfg.database_url, "SELECT item_id FROM ap.intake_leases") == []


def test_files_over_the_run_limit_wait_for_the_next_run(drive):
    listed = [drive_file(f"f{i}") for i in range(drive_intake.PROCESS_LIMIT + 1)]
    answers = screen(*listed, drive_file("bad", name="x.zip", mime_type="application/zip"))
    routes = [answers[f["id"]][0] for f in listed]
    assert routes == ["process"] * drive_intake.PROCESS_LIMIT + ["wait"]
    assert answers[listed[-1]["id"]] == ("wait", "next_run")
    assert answers["bad"] == ("error", "unsupported_type")  # refusals don't wait for a slot


def test_failed_file_waits_then_is_retried_and_resolved(drive):
    state, cfg = drive
    state["provider"] = FakeProvider(error=ProviderError("timeout", "slow", retryable=True))
    assert screen(PDF, run="1") == {"f1": ("process", None)}
    assert post_drive("f1", n8n_execution_id="1").status_code == 503
    assert report("f1", run="1")["state"] == "waiting"
    assert screen(PDF, run="2") == {"f1": ("wait", "retry_later")}  # backoff: stays in Inbox

    time_passes(cfg.database_url)
    expire_leases(cfg.database_url)
    assert screen(PDF, run="3") == {"f1": ("process", None)}
    row = query(cfg.database_url, "SELECT attempts, counted_execution_id FROM ap.intake_failures")[0]
    assert row == {"attempts": 2, "counted_execution_id": "3"}

    state["provider"] = FakeProvider()
    assert post_drive("f1", n8n_execution_id="3").json()["status"] == "ready"
    assert query(cfg.database_url, "SELECT state FROM ap.intake_failures")[0]["state"] == "resolved"
    expire_leases(cfg.database_url)
    assert screen(PDF, run="4") == {"f1": ("process", None)}  # e.g. the move failed: processed again…
    assert post_drive("f1", n8n_execution_id="4").json()["already_processed"]  # …never a second invoice


def test_permanent_failure_is_given_up_and_goes_to_error(drive):
    screen(PDF)
    answer = report("f1", status=502, error="provider_bad_request")
    assert answer["state"] == "given_up"
    assert screen(PDF, run="2") == {"f1": ("error", "given_up")}


def test_download_failure_is_explained_as_drive(drive):
    answer = report("f1", status=None, error=None, stage="fetch")
    assert answer["code"] == "drive_fetch_failed" and "Google Drive" in answer["explanation"]


def test_same_file_by_email_then_drive_is_a_duplicate(drive):
    state, _cfg = drive
    first = post(source_ref="gmail:m1:0").json()
    second = post_drive("f1").json()
    assert first["status"] == "ready"
    assert second["status"] == "rejected" and second["reasons"][0]["code"] == "duplicate_file"
    assert state["provider"].calls == 1


def test_same_file_by_drive_then_email_is_a_duplicate(drive):
    assert post_drive("f1").json()["status"] == "ready"
    second = post(source_ref="gmail:m1:0").json()
    assert second["status"] == "rejected" and second["reasons"][0]["code"] == "duplicate_file"
