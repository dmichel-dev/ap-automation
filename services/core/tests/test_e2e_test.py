"""The daily end-to-end test (e2e_test.py): scope from the address, test data kept apart from live data,
the safety of every delete (database rows and QuickBooks objects), the checks, and the summary section.
Real Postgres; QuickBooks and Gmail answers are given as n8n would pass them."""

import base64
import email
from datetime import UTC, datetime
from decimal import Decimal
from email import policy

import pytest
from app import daily_summary, db, e2e_test, intake
from app.bank_details import change_wording
from app.main import app
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from .conftest import AUTH, query, set_scope
from .test_accounting import bank_page, claim, make_invoice, plaid_tx, post_ok, register_bank, run, sql
from .test_processing import FakeProvider, env, post  # noqa: F401  (fixture)

c = TestClient(app)
ALIAS = "invoices@example.test"
E2E = "invoices+e2e@example.test"
VENDOR = "77"  # the test supplier's QuickBooks vendor in these tests
REASONS = Jsonb([{"code": "total_mismatch", "field": "total", "message": "x"}])


def conn_of(cfg):
    return db.connect(cfg.database_url)


def start(cfg):
    with conn_of(cfg) as conn:
        return e2e_test.start(conn, "x1")["id"]


def scoped_invoice(cfg, scope, number="E2E-1001", **kw):
    inv = make_invoice(cfg, number=number, **kw)
    set_scope(cfg.database_url, inv, scope)
    return inv


def invoice_count(cfg, scope):
    return query(cfg.database_url, "SELECT count(*) AS n FROM ap.invoices WHERE scope = %s", scope)[0]["n"]


# =====================================================================================
# Scope: from the address, and only for a run in progress
# =====================================================================================
def screen(to, subject=""):
    return intake.screen_email([to], "", [], ALIAS, text="hello", subject=subject)


def test_mail_to_the_alias_is_live():
    s = screen(ALIAS)
    assert (s.scope, s.e2e_run) == ("live", None)


def test_mail_to_the_test_address_naming_a_run_is_test_data():
    s = screen(E2E, "[E2E run 12] Invoice E2E-1001")
    assert (s.scope, s.e2e_run) == ("e2e", 12)


def test_mail_to_the_test_address_without_a_run_is_processed_as_live():
    """A real invoice sent there is never hidden among test data."""
    assert screen(E2E, "Invoice 42").scope == "live"


def test_another_sub_address_is_not_for_the_alias():
    assert screen("invoices+other@example.test", "[E2E run 1] x").route == "not_for_alias"


def test_the_core_keeps_test_scope_only_while_the_named_run_is_in_progress(cfg):
    run_id = start(cfg)
    with conn_of(cfg) as conn:
        assert e2e_test.confirm_scope(conn, screen(E2E, f"[E2E run {run_id}] x")).scope == "e2e"
        assert e2e_test.confirm_scope(conn, screen(E2E, f"[E2E run {run_id + 1}] x")).scope == "live"
    sql(cfg, "UPDATE ap.e2e_runs SET status = 'passed', finished_at = now()")
    with conn_of(cfg) as conn:
        assert e2e_test.confirm_scope(conn, screen(E2E, f"[E2E run {run_id}] x")).scope == "live"


def test_the_screen_endpoint_answers_the_scope(cfg, monkeypatch):
    monkeypatch.setenv("INTAKE_EMAIL_ALIAS", ALIAS)
    from app.config import get_settings

    get_settings.cache_clear()
    run_id = start(cfg)
    try:
        r = c.post(
            "/intake/email/screen",
            headers=AUTH,
            json={"recipients": [E2E], "subject": f"[E2E run {run_id}] Invoice", "text": "x", "attachments": []},
        )
    finally:
        get_settings.cache_clear()
    assert r.status_code == 200, r.text
    assert (r.json()["scope"], r.json()["e2e_run"]) == ("e2e", run_id)


# =====================================================================================
# Test data is kept apart
# =====================================================================================
def test_a_test_invoice_and_everything_about_it_carry_the_test_scope(env):  # noqa: F811
    state, cfg = env
    state["provider"] = FakeProvider(total="USD 651.24")  # total mismatch: a review item too
    r = post(scope="e2e")
    assert r.status_code == 200, r.text
    inv = r.json()["invoice_id"]
    rows = query(cfg.database_url, "SELECT scope FROM ap.invoices WHERE id = %s", inv)
    assert rows == [{"scope": "e2e"}]
    assert query(cfg.database_url, "SELECT DISTINCT scope FROM ap.review_queue") == [{"scope": "e2e"}]
    assert query(cfg.database_url, "SELECT DISTINCT scope FROM ap.processing_log") == [{"scope": "e2e"}]


def test_the_same_file_is_not_a_duplicate_across_scopes(env):  # noqa: F811
    _state, _cfg = env
    assert post(scope="e2e").json()["status"] == "ready"
    live = post(source_ref="gmail:m2:a1").json()  # same bytes, live
    assert live["status"] == "ready" and not live["reasons"]  # neither duplicate_file nor possible_duplicate
    again = post(source_ref="gmail:m3:a1", scope="e2e").json()
    assert [r["code"] for r in again["reasons"]] == ["duplicate_file"]  # within the test scope it still is


def test_a_test_bill_is_never_matched_to_a_bank_payment(cfg):
    register_bank(cfg)
    inv = scoped_invoice(cfg, "e2e", number="QM-1042")
    claim(cfg)
    post_ok(cfg, inv, ext="901")
    bank_page(cfg, plaid_tx("t1", 641.24))
    assert run(cfg)["accepted"] == 0


def test_a_failed_test_email_stays_out_of_the_live_summary(cfg):
    r = c.post(
        "/intake/failures",
        headers=AUTH,
        json={"message_id": "m-e2e", "stage": "process", "status_code": 500, "scope": "e2e"},
    )
    assert r.status_code == 200, r.text
    with conn_of(cfg) as conn:
        summary = daily_summary.figures(conn)["intake"]
    assert summary["retrying"] == 0 and summary["given_up"] == []


# =====================================================================================
# Deleting test rows: scope 'e2e' only
# =====================================================================================
def test_the_delete_refuses_live_and_demo_rows_and_deletes_nothing(cfg):
    test_row = scoped_invoice(cfg, "e2e")
    for scope in ("live", "demo"):
        other = scoped_invoice(cfg, scope, number=f"{scope}-1")
        with conn_of(cfg) as conn, pytest.raises(e2e_test.NotTestData):
            e2e_test.delete_test_invoices(conn, [test_row, other])
    assert (invoice_count(cfg, "e2e"), invoice_count(cfg, "live"), invoice_count(cfg, "demo")) == (1, 1, 1)


def test_the_reset_deletes_only_test_rows(cfg):
    test_row = scoped_invoice(cfg, "e2e", status="needs_review", account=None)
    sql(cfg, "INSERT INTO ap.review_queue (invoice_id, reasons, scope) VALUES (%s, %s, 'e2e')", test_row, REASONS)
    for scope in ("live", "demo"):
        scoped_invoice(cfg, scope, number=f"{scope}-1")
    run_id = start(cfg)
    with conn_of(cfg) as conn:
        done = e2e_test.reset(conn, run_id, "x1")
    assert done.invoices == 1 and len(done.files) == 1
    assert (invoice_count(cfg, "e2e"), invoice_count(cfg, "live"), invoice_count(cfg, "demo")) == (0, 1, 1)
    assert query(cfg.database_url, "SELECT count(*) AS n FROM ap.review_queue")[0]["n"] == 0
    logged = query(cfg.database_url, "SELECT scope, details FROM ap.processing_log WHERE step = 'e2e_test'")
    assert {r["scope"] for r in logged} == {"e2e"}
    assert all(r["details"]["actor"].startswith("e2e-test") for r in logged)


def test_the_reset_waits_until_the_test_bills_are_deleted_in_quickbooks(cfg):
    inv = posted_test_bill(cfg)
    run_id = start(cfg)
    with conn_of(cfg) as conn, pytest.raises(e2e_test.Blocked, match="Bill 901"):
        e2e_test.reset(conn, run_id, "x1")
    assert invoice_count(cfg, "e2e") == 1
    delete_all(cfg, run_id)
    with conn_of(cfg) as conn:
        assert e2e_test.reset(conn, run_id, "x1").invoices == 1
    assert (
        query(cfg.database_url, "SELECT count(*) AS n FROM ap.accounting_postings WHERE invoice_id = %s", inv)[0]["n"]
        == 0
    )


def test_the_reset_needs_its_run_in_progress(cfg):
    with conn_of(cfg) as conn, pytest.raises(e2e_test.Blocked):
        e2e_test.reset(conn, 999, "x1")


# =====================================================================================
# QuickBooks clean-up: only what is provably the test's own
# =====================================================================================
def posted_test_bill(cfg, number="E2E-1001"):
    with conn_of(cfg) as conn:
        e2e_test.setup(conn, VENDOR, "20")
    inv = scoped_invoice(cfg, "e2e", number=number)
    claim(cfg)
    post_ok(cfg, inv, ext="901", vendor=VENDOR, attachment_external_id="5001")
    return inv


def bill(inv, **overrides):
    found = {
        "Id": "901",
        "SyncToken": "3",
        "VendorRef": {"value": VENDOR},
        "DocNumber": "E2E-1001",
        "PrivateNote": f"AP Automation · invoice E2E-1001 · id {inv}",
        "TotalAmt": 641.24,
    }
    return {"Bill": {**found, **overrides}}


def attachable(bill_id="901"):
    return {
        "Attachable": {
            "Id": "5001",
            "SyncToken": "0",
            "AttachableRef": [{"EntityRef": {"type": "Bill", "value": bill_id}}],
        }
    }


def objects(cfg, run_id):
    with conn_of(cfg) as conn:
        return {o["entity"]: o["id"] for o in e2e_test.cleanup_list(conn, run_id)}


def check(cfg, object_id, body, status=200):
    with conn_of(cfg) as conn:
        return e2e_test.qbo_check(conn, object_id, status, body, "x1")


def delete_all(cfg, run_id):
    for entity, object_id in objects(cfg, run_id).items():
        inv = query(cfg.database_url, "SELECT invoice_id FROM ap.e2e_qbo_objects WHERE id = %s", object_id)[0]
        answer = bill(inv["invoice_id"]) if entity == "Bill" else attachable()
        assert check(cfg, object_id, answer)["action"] == "delete"
        with conn_of(cfg) as conn:
            assert e2e_test.qbo_deleted(
                conn, object_id, 200, {entity: {"status": "Deleted", "Id": answer[entity]["Id"]}}, "x1"
            )


def test_the_clean_up_lists_attachments_before_bills(cfg):
    posted_test_bill(cfg)
    run_id = start(cfg)
    with conn_of(cfg) as conn:
        listed = e2e_test.cleanup_list(conn, run_id)
    assert [(o["entity"], o["external_id"]) for o in listed] == [("Attachable", "5001"), ("Bill", "901")]


def test_the_test_bill_is_deleted_with_the_request_the_core_writes(cfg):
    inv = posted_test_bill(cfg)
    run_id = start(cfg)
    answer = check(cfg, objects(cfg, run_id)["Bill"], bill(inv))
    assert answer == {"action": "delete", "path": "/bill", "body": {"Id": "901", "SyncToken": "3"}}


@pytest.mark.parametrize(
    ("overrides", "why"),
    [
        ({"VendorRef": {"value": "55"}}, "vendor is not the test supplier"),
        ({"DocNumber": "QM-1042"}, "number 'QM-1042' is not the test posting"),
        ({"PrivateNote": "someone else's bill"}, "note doesn't name the test invoice"),
        ({"Id": "902"}, "another bill"),
    ],
)
def test_a_bill_that_is_not_provably_the_tests_own_is_kept(cfg, overrides, why):
    inv = posted_test_bill(cfg)
    run_id = start(cfg)
    object_id = objects(cfg, run_id)["Bill"]
    answer = check(cfg, object_id, bill(inv, **overrides))
    assert answer["action"] == "keep" and why in answer["reason"]
    state = query(cfg.database_url, "SELECT state FROM ap.e2e_qbo_objects WHERE id = %s", object_id)[0]["state"]
    assert state == "refused"


def test_a_bill_of_a_live_invoice_is_never_deleted(cfg):
    inv = posted_test_bill(cfg)
    run_id = start(cfg)
    object_id = objects(cfg, run_id)["Bill"]
    sql(cfg, "UPDATE ap.invoices SET scope = 'live' WHERE id = %s", inv)  # e.g. a mistake in the data
    answer = check(cfg, object_id, bill(inv))
    assert answer["action"] == "keep" and "not test data (scope live)" in answer["reason"]


def test_an_attachment_on_another_bill_is_kept(cfg):
    posted_test_bill(cfg)
    run_id = start(cfg)
    answer = check(cfg, objects(cfg, run_id)["Attachable"], attachable(bill_id="777"))
    assert answer["action"] == "keep" and "not attached to the test bill only" in answer["reason"]


def test_an_object_already_gone_is_skipped(cfg):
    posted_test_bill(cfg)
    run_id = start(cfg)
    gone = {"Fault": {"Error": [{"code": "610", "Message": "Object Not Found"}]}}
    assert check(cfg, objects(cfg, run_id)["Bill"], gone, status=400)["action"] == "skip"


def test_a_delete_counts_only_when_quickbooks_says_deleted(cfg):
    inv = posted_test_bill(cfg)
    run_id = start(cfg)
    object_id = objects(cfg, run_id)["Bill"]
    check(cfg, object_id, bill(inv))
    with conn_of(cfg) as conn:
        assert not e2e_test.qbo_deleted(conn, object_id, 400, {"Fault": {}}, "x1")
        assert e2e_test.qbo_deleted(conn, object_id, 200, {"Bill": {"status": "Deleted", "Id": "901"}}, "x1")


# =====================================================================================
# Messages and fixtures
# =====================================================================================
def parsed(raw):
    return email.message_from_bytes(base64.urlsafe_b64decode(raw), policy=policy.default)


def test_four_messages_to_the_test_address_naming_the_run():
    out = e2e_test.messages(7, ALIAS, datetime(2026, 11, 24, tzinfo=UTC))
    assert [m["scenario"] for m in out] == ["posted", "review", "rejected", "fraud_alert"]
    for m in out:
        msg = parsed(m["raw"])
        assert msg["To"] == E2E and msg["Delivered-To"] == E2E and msg["X-AP-E2E-Run"] == "7"
        assert msg["Subject"].startswith("[E2E run 7] ")
    files = [[a.get_filename() for a in parsed(m["raw"]).iter_attachments()] for m in out]
    assert files == [["e2e-invoice-ready.pdf"], ["e2e-invoice-wrong-total.pdf"], ["e2e-delivery-note.docx"], []]


def test_the_rejected_and_fraud_emails_take_their_route_without_an_ocr_call():
    docx = intake.Attachment("attachment_0", e2e_test.MIME["docx"], "e2e-delivery-note.docx", 943)
    rejected = intake.screen_email([E2E], "", [docx], ALIAS, text=e2e_test.BY_KEY["rejected"].body)
    assert rejected.route == "no_valid_attachment"
    fraud = e2e_test.BY_KEY["fraud_alert"]
    assert intake.screen_email([E2E], "", [], ALIAS, text=fraud.subject + "\n\n" + fraud.body).route == (
        "bank_detail_notice"
    )
    assert change_wording(fraud.body)


def test_the_clean_invoice_stays_under_every_review_rule(env):  # noqa: F811
    """The fixture's content (as OCR reads it) with the test supplier set up: straight through."""
    state, cfg = env
    with conn_of(cfg) as conn:
        e2e_test.setup(conn, VENDOR, "20")
    state["provider"] = FakeProvider(
        supplier_name=e2e_test.SUPPLIER_NAME,
        supplier_tax_id="00-0000090",
        invoice_number="E2E-1001",
        invoice_date="11/24/2026",
        due_date="12/24/2026",
        line_items=[
            {
                "kind": "item",
                "description": "Copy paper, letter size (case)",
                "quantity": "4",
                "unit_price": "$38.00",
                "amount": "$152.00",
                "tax_rate": None,
            },
            {
                "kind": "item",
                "description": "Desk organizer, mesh",
                "quantity": "2",
                "unit_price": "$24.50",
                "amount": "$49.00",
                "tax_rate": None,
            },
        ],
        tax_lines=[
            {
                "tax_type": "sales_tax",
                "label": "Sales tax 8.5%",
                "rate": "8.5%",
                "taxable_base": "$201.00",
                "amount": "$17.09",
            }
        ],
        subtotal="$201.00",
        tax_total="$17.09",
        total="USD 218.09",
    )
    out = post(scope="e2e").json()
    assert (out["status"], out["reasons"]) == ("ready", [])
    assert out["summary"]["total"] == e2e_test.BY_KEY["posted"].total
    assert Decimal("201.00") < Decimal("1000")  # under cut-off materiality (no service period anyway)


# =====================================================================================
# Runs and verification
# =====================================================================================
def test_one_run_at_a_time_and_an_unfinished_one_is_closed_as_failed(cfg):
    first = start(cfg)
    with pytest.raises(e2e_test.Blocked):
        start(cfg)
    sql(cfg, "UPDATE ap.e2e_runs SET started_at = now() - interval '2 hours' WHERE id = %s", first)
    start(cfg)
    row = query(cfg.database_url, "SELECT status, failures FROM ap.e2e_runs WHERE id = %s", first)[0]
    assert row["status"] == "failed" and row["failures"][0]["message"] == "did not finish"


def seed_passing_run(cfg):
    """What a good run leaves behind: the four emails processed, the bill posted then deleted."""
    run_id = start(cfg)
    inv = posted_test_bill(cfg)
    sql(cfg, "UPDATE ap.invoices SET source_ref = 'gmail:g1:0', total = 218.09 WHERE id = %s", inv)
    delete_all(cfg, run_id)
    sql(cfg, "UPDATE ap.e2e_qbo_objects SET facts = facts || '{\"total\": \"218.09\"}' WHERE entity = 'Bill'")
    review = scoped_invoice(cfg, "e2e", number="E2E-1002", status="needs_review", account=None)
    sql(
        cfg,
        """UPDATE ap.invoices SET source_ref = 'gmail:g2:0',
                  status_reasons = '[{"code": "total_mismatch", "field": "total", "message": "x"}]' WHERE id = %s""",
        review,
    )
    sql(cfg, "INSERT INTO ap.review_queue (invoice_id, reasons, scope) VALUES (%s, %s, 'e2e')", review, REASONS)
    with conn_of(cfg) as conn:
        for inv_id, message in ((inv, "status ready"), (review, "status needs_review")):
            db.log(conn, step="validate", level="info", message=message, invoice_id=inv_id)
        db.log(conn, step="intake", level="warning", message="email without a valid invoice attachment",
               source_ref="gmail:g3", scope="e2e")  # fmt: skip
        db.log(conn, step="intake", level="warning", message="possible_bank_detail_change: email asks",
               source_ref="gmail:g4", scope="e2e")  # fmt: skip
    return run_id


SEEN = [
    {"scenario": "posted", "gmail_id": "g1", "labels": ["AP/Ready", "AP/Processed"], "trashed": True},
    {"scenario": "review", "gmail_id": "g2", "labels": ["AP/Review", "AP/Processed", "UNREAD"], "trashed": True},
    {"scenario": "rejected", "gmail_id": "g3", "labels": ["AP/Rejected", "AP/Processed"], "trashed": True},
    {"scenario": "fraud_alert", "gmail_id": "g4", "labels": ["AP/Fraud alert", "AP/Processed"], "trashed": True},
]


def verify(run_id, seen=SEEN):
    r = c.post(f"/e2e/runs/{run_id}/verify", headers=AUTH, json={"messages": seen})
    assert r.status_code == 200, r.text
    return r.json()


def test_a_good_run_passes(acfg):
    out = verify(seed_passing_run(acfg))
    assert (out["status"], out["failures"], out["alert"]) == ("passed", [], None)
    assert out["checks"] >= 20


def test_a_missing_label_fails_the_run_with_an_alert(acfg):
    seen = [dict(m) for m in SEEN]
    seen[0]["labels"] = ["AP/Error"]
    out = verify(seed_passing_run(acfg), seen)
    assert out["status"] == "failed"
    messages = [f["message"] for f in out["failures"]]
    assert "Gmail labels missing: AP/Ready, AP/Processed (has ['AP/Error'])" in messages
    assert "the email is labelled AP/Error" in messages
    assert out["alert"]["subject"].startswith("AP end-to-end test FAILED")
    assert "posted to QuickBooks: the email is labelled AP/Error" in out["alert"]["text"]
    assert "© David Michel" in out["alert"]["html"]


def test_a_full_bank_number_in_the_log_fails_the_run(acfg):
    run_id = seed_passing_run(acfg)
    with conn_of(acfg) as conn:
        db.log(conn, step="intake", level="info", message=f"pay to {e2e_test.FULL_ACCOUNT_NUMBER}", scope="e2e")
    out = verify(run_id)
    assert [f["check"] for f in out["failures"]] == ["masking"]


def test_a_test_email_logged_as_live_data_fails_the_run(acfg):
    run_id = seed_passing_run(acfg)
    with conn_of(acfg) as conn:
        db.log(conn, step="core_call", level="error", message="x", source_ref="gmail:g3")  # live by default
    out = verify(run_id)
    assert [f["check"] for f in out["failures"]] == ["isolation"]


def test_a_bill_left_in_quickbooks_fails_the_run(acfg):
    run_id = seed_passing_run(acfg)
    sql(acfg, "UPDATE ap.e2e_qbo_objects SET state = 'refused' WHERE entity = 'Bill'")
    out = verify(run_id)
    assert [(f["scenario"], f["check"]) for f in out["failures"]] == [("posted", "cleanup")]


def test_an_email_left_out_of_the_trash_fails_the_run(acfg):
    """Seen in the first live run: the trash call failed (HTTP 400) and nothing said so."""
    seen = [dict(m) for m in SEEN]
    seen[1]["trashed"] = False
    out = verify(seed_passing_run(acfg), seen)
    assert [(f["scenario"], f["check"], f["message"]) for f in out["failures"]] == [
        ("review", "cleanup", "the test email was not moved to Gmail's trash")
    ]


def test_an_aborted_run_fails_with_the_step_and_an_alert(acfg):
    run_id = start(acfg)
    r = c.post(f"/e2e/runs/{run_id}/abort", headers=AUTH, json={"step": "All in Gmail?", "message": "HTTP 403"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["status"] == "failed"
    assert "the run stopped at 'All in Gmail?': HTTP 403" in out["alert"]["text"]


# =====================================================================================
# The daily summary
# =====================================================================================
def summary(cfg):
    with conn_of(cfg) as conn:
        return daily_summary.daily(conn, ["ap@example.test"])


def test_no_section_before_the_test_ever_ran(cfg):
    out = summary(cfg)
    assert out["text"].startswith(daily_summary.NOTHING) and out["subject"].endswith("nothing to report")


def test_a_passed_run_is_one_line_and_still_nothing_to_report(acfg):
    verify(seed_passing_run(acfg))
    sql(acfg, "DELETE FROM ap.review_queue")  # the test's own review item never shows anyway (scope)
    out = summary(acfg)
    assert "End-to-end test\n- Passed at " in out["text"]
    assert "(posted to QuickBooks, sent to review, rejected, fraud alert)" in out["text"]
    assert "Received" not in out["text"]  # test invoices are not live figures
    assert out["subject"].endswith("nothing to report")


def test_the_latest_run_of_the_period_is_shown_in_paris_time(acfg):
    first = start(acfg)
    sql(
        acfg,
        """UPDATE ap.e2e_runs SET status = 'passed', finished_at = now(), checks = 36,
                  started_at = '2026-11-24 06:30:00+00' WHERE id = %s""",
        first,
    )
    second = start(acfg)
    sql(
        acfg,
        """UPDATE ap.e2e_runs SET status = 'passed', finished_at = now(), checks = 40,
                  started_at = now() - interval '1 minute' WHERE id = %s""",
        second,
    )
    with conn_of(acfg) as conn:
        f = daily_summary.figures(conn)
        latest_start = f["e2e"]["run"]["started_at"]
    assert f["e2e"]["run"]["id"] == second
    paris = latest_start.astimezone(daily_summary.TIME_ZONE)
    assert f"- Passed at {paris:%H:%M} Paris time: 40 checks" in summary(acfg)["text"]


def test_times_are_in_paris_time():
    started = datetime(2026, 11, 24, 6, 30, tzinfo=UTC)
    run = {"started_at": started, "status": "passed", "checks": 40}
    _, lines = e2e_test.describe(run, started, daily_summary.TIME_ZONE)
    assert lines[0].startswith("Passed at 07:30 Paris time: 40 checks")


def test_a_failed_run_is_in_the_subject(acfg):
    seen = [dict(m) for m in SEEN]
    seen[2]["labels"] = []
    verify(seed_passing_run(acfg), seen)
    out = summary(acfg)
    assert ": end-to-end test FAILED, " in out["subject"]
    assert "rejected: Gmail labels missing: AP/Rejected, AP/Processed (has [])" in out["text"]


def test_a_day_without_a_run_is_a_failure_once_the_test_is_set_up(cfg):
    start(cfg)
    sql(cfg, "UPDATE ap.e2e_runs SET started_at = now() - interval '25 hours', status = 'passed', finished_at = now()")
    out = summary(cfg)
    assert "No end-to-end test ran in the last 24 hours" in out["text"]
    assert "end-to-end test FAILED" in out["subject"]


def test_an_unfinished_run_is_a_failure(cfg):
    start(cfg)
    sql(cfg, "UPDATE ap.e2e_runs SET started_at = now() - interval '2 hours'")
    assert "did not finish" in summary(cfg)["text"]
