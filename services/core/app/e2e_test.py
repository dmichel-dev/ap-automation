"""The daily end-to-end test: four emails go through the whole pipeline, then every result is checked.

The n8n workflow 'AP – Daily e2e test' runs it every morning before the daily summary. The core decides
everything here: the messages, what each must produce, which QuickBooks objects and which database rows
may be deleted, pass or fail.

  1. start         a run row (an unfinished earlier run is closed as failed)
  2. clean up      QuickBooks objects created by earlier test postings: each is read back, checked and
                   deleted only if it is provably the test's own (qbo_check)
  3. reset         the earlier runs' invoices are deleted (scope 'e2e' only, never live or demo rows),
                   then the four messages are handed out
  4. n8n puts them in the mailbox (Gmail API messages.insert); the email workflow processes them
  5. clean up again (QuickBooks, and the test emails to Gmail's trash), then verify: status, Gmail labels,
     log entries, QuickBooks objects, the clean-up, isolation, no full bank account number in the log

Scenarios (two OCR calls per run): a clean invoice posted straight through, an invoice with a wrong total
(review), an email with only a .docx (rejected), an email asking to change bank details (fraud alert).
Every action of the test is logged with scope 'e2e' under the automation label 'e2e-test'.
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, tzinfo
from decimal import Decimal, InvalidOperation
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid
from pathlib import Path
from typing import Any

from psycopg.types.json import Jsonb

from . import accounting, db, intake
from .normalize import normalize_tax_id

ACTOR = "e2e-test (daily end-to-end test)"
FIXTURES = Path(__file__).parent / "e2e_fixtures"
SUPPLIER_NAME = "Ostlewick Test Supplies LLC"
SUPPLIER_KEY = normalize_tax_id("00-0000090") or ""  # the fixtures' supplier tax ID: its supplier_key
SENDER = f"{SUPPLIER_NAME} <billing@example.com>"
DOC_PREFIX = "E2E-"  # every test invoice number, so every test bill in QuickBooks, starts with it
CURRENCY = "USD"
STALE_AFTER = timedelta(hours=1)  # a run still 'running' after this did not finish
# The fraud scenario's account number: fictional, and it must never reach the log in full
FULL_ACCOUNT_NUMBER = "0000-FAKE-9021"
MIME = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}
QBO_NOT_FOUND = "610"  # QuickBooks Fault code: Object Not Found (already deleted)
PROCESSED = "AP/Processed"
ERROR = "AP/Error"


class Blocked(Exception):
    """The test can't go on safely (a run in progress, QuickBooks objects not deleted yet): HTTP 409."""


class NotTestData(Exception):
    """A delete was asked for rows that aren't the end-to-end test's own (live or demo data)."""


@dataclass(frozen=True)
class Scenario:
    key: str
    title: str  # plain words, for the summary
    subject: str
    body: str
    attachment: str | None  # file in e2e_fixtures
    labels: tuple[str, ...]  # Gmail labels the email must carry (and never AP/Error)
    log: tuple[str, str]  # (step, start of the message) that must be logged for it
    status: str | None = None  # the invoice's status; None: no invoice
    reasons: tuple[str, ...] = ()
    total: str | None = None
    posted: bool = False


SCENARIOS = (
    Scenario(
        "posted",
        "posted to QuickBooks",
        "Invoice E2E-1001 – Ostlewick Test Supplies LLC",
        "Hello,\n\nplease find attached invoice E2E-1001 for USD 218.09, due 12/24/2026.\n\n"
        "Best regards,\nOstlewick Test Supplies LLC (fictional test supplier)",
        "e2e-invoice-ready.pdf",
        ("AP/Ready", PROCESSED),
        ("validate", "status ready"),
        status="ready",
        total="218.09",
        posted=True,
    ),
    Scenario(
        "review",
        "sent to review",
        "Invoice E2E-1002 – Ostlewick Test Supplies LLC",
        "Hello,\n\nplease find attached invoice E2E-1002.\n\n"
        "Best regards,\nOstlewick Test Supplies LLC (fictional test supplier)",
        "e2e-invoice-wrong-total.pdf",
        ("AP/Review", PROCESSED),
        ("validate", "status needs_review"),
        status="needs_review",
        reasons=("total_mismatch",),
    ),
    Scenario(
        "rejected",
        "rejected",
        "Delivery note DN-1003 – Ostlewick Test Supplies LLC",
        "Hello,\n\nattached is the delivery note for your last order.\n\n"
        "Best regards,\nOstlewick Test Supplies LLC (fictional test supplier)",
        "e2e-delivery-note.docx",
        ("AP/Rejected", PROCESSED),
        ("intake", "email without a valid invoice attachment"),
    ),
    Scenario(
        "fraud_alert",
        "fraud alert",
        "Change of bank details – Ostlewick Test Supplies LLC",
        "Hello,\n\nplease note that our bank details have changed. From today, please pay all invoices to "
        "our new account:\n\n    Bank: Fictional Savings Bank (test)\n    Routing number: 000000000\n"
        f"    Account number: {FULL_ACCOUNT_NUMBER}\n\n"
        "Best regards,\nOstlewick Test Supplies LLC (fictional test supplier)",
        None,
        ("AP/Fraud alert", PROCESSED),
        ("intake", "possible_bank_detail_change:"),
    ),
)
BY_KEY = {s.key: s for s in SCENARIOS}


# =====================================================================================
# Messages
# =====================================================================================
def marker(run_id: int) -> str:
    return f"[E2E run {run_id}]"


def build_message(run_id: int, s: Scenario, to: str, now: datetime) -> bytes:
    """One RFC 822 message, as it would arrive: the run named in the subject and a header."""
    msg = EmailMessage()
    msg["From"] = SENDER
    msg["To"] = to
    msg["Delivered-To"] = to
    msg["Subject"] = f"{marker(run_id)} {s.subject}"
    msg["Date"] = format_datetime(now)
    msg["Message-ID"] = make_msgid("e2e", domain="example.com")
    msg["X-AP-E2E-Run"] = str(run_id)
    msg.set_content(s.body)
    if s.attachment:
        maintype, subtype = MIME[s.attachment.rsplit(".", 1)[1]].split("/", 1)
        data = (FIXTURES / s.attachment).read_bytes()
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=s.attachment)
    return msg.as_bytes()


def messages(run_id: int, alias: str, now: datetime) -> list[dict[str, str]]:
    """The run's messages for Gmail's messages.insert (raw = base64url of the RFC 822 bytes)."""
    to = intake.sub_address(alias, intake.E2E_TAG)
    return [
        {"scenario": s.key, "raw": base64.urlsafe_b64encode(build_message(run_id, s, to, now)).decode()}
        for s in SCENARIOS
    ]


def confirm_scope(conn: db.Conn, screening: intake.Screening) -> intake.Screening:
    """An email to the test address is test data only while the run its subject names is in progress."""
    if screening.scope == "e2e" and not _in_progress(conn, screening.e2e_run):
        screening.scope, screening.e2e_run = "live", None
    return screening


def _in_progress(conn: db.Conn, run_id: int | None) -> bool:
    row = conn.execute(
        "SELECT 1 FROM e2e_runs WHERE id = %s AND status = 'running' AND started_at > now() - %s",
        (run_id, STALE_AFTER),
    ).fetchone()
    return row is not None


# =====================================================================================
# Runs
# =====================================================================================
def _log(conn: db.Conn, level: str, message: str, execution_id: str | None, **details: Any) -> None:
    db.log(
        conn,
        step="e2e_test",
        level=level,
        message=message,
        channel="system",
        execution_id=execution_id,
        scope="e2e",
        details={"actor": ACTOR, **details},
    )


def start(conn: db.Conn, execution_id: str | None) -> db.Row:
    """A new run. One at a time; a run left unfinished (the workflow died) is closed as failed."""
    db.lock(conn, "e2e_test")
    if conn.execute(
        "SELECT 1 FROM e2e_runs WHERE status = 'running' AND started_at > now() - %s", (STALE_AFTER,)
    ).fetchone():
        raise Blocked("an end-to-end run is already in progress")
    stale = conn.execute(
        """UPDATE e2e_runs SET status = 'failed', finished_at = now(),
                  failures = failures || '[{"scenario": "run", "check": "finished", "message": "did not finish"}]'
            WHERE status = 'running' RETURNING id"""
    ).fetchall()
    for r in stale:
        _log(conn, "error", f"run {r['id']} did not finish", execution_id, run_id=r["id"])
    run = db.one(conn, "INSERT INTO e2e_runs (execution_id) VALUES (%s) RETURNING *", (execution_id,))
    _log(conn, "info", f"run {run['id']} started", execution_id, run_id=run["id"])
    return run


def record_sent(conn: db.Conn, run_id: int, sent: list[dict[str, str]]) -> None:
    """The Gmail IDs of the run's messages, as n8n inserted them."""
    unknown = {m["scenario"] for m in sent} - set(BY_KEY)
    if unknown:
        raise ValueError(f"unknown scenarios: {sorted(unknown)}")
    updated = conn.execute(
        "UPDATE e2e_runs SET messages = %s WHERE id = %s AND status = 'running' RETURNING id",
        (Jsonb(sent), run_id),
    ).fetchone()
    if updated is None:
        raise Blocked(f"run {run_id} is not in progress")


# =====================================================================================
# QuickBooks clean-up: only objects that are provably the test's own are deleted
# =====================================================================================
def e2e_vendor_id(conn: db.Conn) -> str | None:
    """The QuickBooks vendor of the test supplier (set once by the setup)."""
    row = conn.execute(
        "SELECT external_id FROM vendor_links WHERE supplier_key = %s AND currency = %s", (SUPPLIER_KEY, CURRENCY)
    ).fetchone()
    return row["external_id"] if row else None


def cleanup_list(conn: db.Conn, run_id: int) -> list[db.Row]:
    """Every QuickBooks object of a test posting not deleted yet (attachments before bills). Objects
    of a posting are recorded here first, so the record outlives the posting row."""
    conn.execute(
        """INSERT INTO e2e_qbo_objects (run_id, invoice_id, entity, external_id, facts)
           SELECT %(run)s, p.invoice_id, o.entity, o.external_id,
                  jsonb_build_object('doc_number', p.doc_number, 'bill_id', p.external_id)
             FROM accounting_postings p JOIN invoices i ON i.id = p.invoice_id
                  CROSS JOIN LATERAL (VALUES ('Bill', p.external_id), ('Attachable', p.attachment_external_id))
                    AS o (entity, external_id)
            WHERE i.scope = 'e2e' AND p.object_type = 'bill' AND o.external_id IS NOT NULL
           ON CONFLICT (entity, external_id) DO NOTHING""",
        {"run": run_id},
    )
    return conn.execute(
        """SELECT id, entity, external_id, invoice_id FROM e2e_qbo_objects
            WHERE state IN ('pending', 'checked') ORDER BY entity = 'Bill', id"""
    ).fetchall()


def _object(conn: db.Conn, object_id: int) -> db.Row:
    row = conn.execute("SELECT * FROM e2e_qbo_objects WHERE id = %s FOR UPDATE", (object_id,)).fetchone()
    if row is None:
        raise accounting.NotFound(f"unknown test object {object_id}")
    if row["state"] not in ("pending", "checked"):
        raise accounting.Conflict(f"test object {object_id} is {row['state']}")
    return row


def _fault_code(body: Any) -> str | None:
    errors = ((body or {}).get("Fault") or {}).get("Error") or [] if isinstance(body, dict) else []
    return str(errors[0].get("code")) if errors else None


def bill_problems(obj: db.Row, bill: dict[str, Any], vendor_id: str | None, invoice_scope: str | None) -> list[str]:
    """Why a QuickBooks bill read back is NOT provably the test's own (empty: it may be deleted)."""
    problems = []
    if invoice_scope != "e2e":
        problems.append(f"its invoice is not test data (scope {invoice_scope})")
    if str(bill.get("Id")) != obj["external_id"]:
        problems.append("QuickBooks returned another bill")
    if not vendor_id or str((bill.get("VendorRef") or {}).get("value")) != vendor_id:
        problems.append("its vendor is not the test supplier's")
    number = str(bill.get("DocNumber") or "")
    if not number.startswith(DOC_PREFIX) or number != obj["facts"].get("doc_number"):
        problems.append(f"its number {number!r} is not the test posting's")
    if f"id {obj['invoice_id']}" not in str(bill.get("PrivateNote") or ""):
        problems.append("its note doesn't name the test invoice")
    return problems


def attachable_problems(obj: db.Row, attachable: dict[str, Any], invoice_scope: str | None) -> list[str]:
    """Why a QuickBooks attachment read back is NOT provably the test's own."""
    problems = []
    if invoice_scope != "e2e":
        problems.append(f"its invoice is not test data (scope {invoice_scope})")
    if str(attachable.get("Id")) != obj["external_id"]:
        problems.append("QuickBooks returned another attachment")
    refs = [r.get("EntityRef") or {} for r in attachable.get("AttachableRef") or []]
    if not refs or any(r.get("type") != "Bill" or str(r.get("value")) != obj["facts"].get("bill_id") for r in refs):
        problems.append("it is not attached to the test bill only")
    return problems


def _invoice_scope(conn: db.Conn, invoice_id: Any) -> str | None:
    row = conn.execute("SELECT scope FROM invoices WHERE id = %s", (invoice_id,)).fetchone()
    return row["scope"] if row else None


def qbo_check(conn: db.Conn, object_id: int, status_code: int, body: Any, execution_id: str | None) -> dict[str, Any]:
    """The object as read back from QuickBooks → delete it (with the request to send), skip it (already
    gone), or keep it (not provably the test's own: refused, a person looks)."""
    obj = _object(conn, object_id)
    if status_code in (400, 404) and _fault_code(body) == QBO_NOT_FOUND:
        conn.execute("UPDATE e2e_qbo_objects SET state = 'gone' WHERE id = %s", (object_id,))
        return {"action": "skip", "reason": "already deleted in QuickBooks"}
    if status_code != 200 or not isinstance(body, dict) or not isinstance(body.get(obj["entity"]), dict):
        return {"action": "keep", "reason": f"QuickBooks answered {status_code}; tried again on the next run"}
    found = body[obj["entity"]]
    scope = _invoice_scope(conn, obj["invoice_id"])
    if obj["entity"] == "Bill":
        problems = bill_problems(obj, found, e2e_vendor_id(conn), scope)
    else:
        problems = attachable_problems(obj, found, scope)
    if problems:
        reason = "; ".join(problems)
        conn.execute("UPDATE e2e_qbo_objects SET state = 'refused', reason = %s WHERE id = %s", (reason, object_id))
        _log(conn, "error", f"QuickBooks {obj['entity']} {obj['external_id']} NOT deleted: {reason}", execution_id)
        return {"action": "keep", "reason": reason}
    facts = {"total": str(found.get("TotalAmt")), "sync_token": str(found.get("SyncToken"))}
    conn.execute(
        "UPDATE e2e_qbo_objects SET state = 'checked', facts = facts || %s WHERE id = %s", (Jsonb(facts), object_id)
    )
    return {
        "action": "delete",
        "path": f"/{obj['entity'].lower()}",
        "body": {"Id": obj["external_id"], "SyncToken": facts["sync_token"]},
    }


def qbo_deleted(conn: db.Conn, object_id: int, status_code: int, body: Any, execution_id: str | None) -> bool:
    """QuickBooks' answer to the delete: recorded only when it says the object is deleted."""
    obj = _object(conn, object_id)
    answer = body.get(obj["entity"]) if isinstance(body, dict) else None
    deleted = (
        status_code == 200
        and isinstance(answer, dict)
        and answer.get("status") == "Deleted"
        and str(answer.get("Id")) == obj["external_id"]
    )
    if deleted:
        conn.execute("UPDATE e2e_qbo_objects SET state = 'deleted' WHERE id = %s", (object_id,))
        _log(conn, "info", f"QuickBooks {obj['entity']} {obj['external_id']} deleted", execution_id)
    else:
        _log(
            conn,
            "warning",
            f"QuickBooks {obj['entity']} {obj['external_id']} not deleted ({status_code})",
            execution_id,
        )
    return deleted


# =====================================================================================
# Reset: the earlier runs' invoices (scope 'e2e' only)
# =====================================================================================
def delete_test_invoices(conn: db.Conn, invoice_ids: list[Any]) -> int:
    """Deletes test invoices with their lines, tax lines, review items and postings. Refuses the whole
    request if any of them is not scope 'e2e': live and demo rows are never deleted here."""
    if not invoice_ids:
        return 0
    others = conn.execute(
        "SELECT id, scope FROM invoices WHERE id = ANY(%s::uuid[]) AND scope <> 'e2e'", (invoice_ids,)
    ).fetchall()
    if others:
        raise NotTestData(f"{len(others)} of these invoices are not end-to-end test data: nothing deleted")
    conn.execute(
        """DELETE FROM accounting_postings p USING invoices i
            WHERE p.invoice_id = i.id AND i.id = ANY(%s::uuid[]) AND i.scope = 'e2e'""",
        (invoice_ids,),
    )
    deleted = conn.execute("DELETE FROM invoices WHERE id = ANY(%s::uuid[]) AND scope = 'e2e'", (invoice_ids,)).rowcount
    return deleted


def _undeleted_objects(conn: db.Conn) -> list[db.Row]:
    """QuickBooks objects of test postings not known to be deleted: the rows can't go before them."""
    return conn.execute(
        """SELECT o.entity, o.external_id, p.invoice_id
             FROM accounting_postings p JOIN invoices i ON i.id = p.invoice_id
                  CROSS JOIN LATERAL (VALUES ('Bill', p.external_id), ('Attachable', p.attachment_external_id))
                    AS o (entity, external_id)
            WHERE i.scope = 'e2e' AND o.external_id IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM e2e_qbo_objects q WHERE q.entity = o.entity
                                 AND q.external_id = o.external_id AND q.state IN ('deleted', 'gone'))
           UNION ALL
           SELECT 'posting in progress', p.id::text, p.invoice_id
             FROM accounting_postings p JOIN invoices i ON i.id = p.invoice_id
            WHERE i.scope = 'e2e' AND p.status = 'pending' AND p.claimed_until > now()"""
    ).fetchall()


@dataclass
class Reset:
    invoices: int
    files: list[str] = field(default_factory=list)  # stored originals to delete after COMMIT


def reset(conn: db.Conn, run_id: int, execution_id: str | None) -> Reset:
    """Before the run's emails: the earlier runs' test data goes, so the same files are new again."""
    db.lock(conn, "e2e_test")
    if not _in_progress(conn, run_id):
        raise Blocked(f"run {run_id} is not in progress")
    if blocking := _undeleted_objects(conn):
        listed = ", ".join(f"{b['entity']} {b['external_id']}" for b in blocking)
        raise Blocked(f"test objects still in QuickBooks, nothing deleted: {listed}")
    rows = conn.execute("SELECT id, file_path FROM invoices WHERE scope = 'e2e' FOR UPDATE").fetchall()
    count = delete_test_invoices(conn, [r["id"] for r in rows])
    failures = conn.execute("DELETE FROM intake_failures WHERE scope = 'e2e'").rowcount
    _log(conn, "info", f"earlier test data deleted: {count} invoices", execution_id, run_id=run_id, failures=failures)
    return Reset(count, [r["file_path"] for r in rows if r["file_path"]])


# =====================================================================================
# Verification
# =====================================================================================
@dataclass
class Check:
    """The checks of one run: each passed one counted, each failed one described."""

    passed: int = 0
    failures: list[dict[str, str]] = field(default_factory=list)

    def expect(self, ok: bool, scenario: str, check: str, message: str) -> bool:
        if ok:
            self.passed += 1
        else:
            self.failures.append({"scenario": scenario, "check": check, "message": message})
        return ok


def _gmail_ref(gmail_id: str) -> str:
    return f"gmail:{gmail_id}"


def _check_gmail(c: Check, s: Scenario, seen: dict[str, Any]) -> None:
    labels = list(seen.get("labels") or [])
    missing = [label for label in s.labels if label not in labels]
    c.expect(not missing, s.key, "gmail_labels", f"Gmail labels missing: {', '.join(missing)} (has {labels})")
    c.expect(ERROR not in labels, s.key, "gmail_labels", "the email is labelled AP/Error")
    c.expect(seen.get("trashed") is True, s.key, "cleanup", "the test email was not moved to Gmail's trash")


def _check_log(conn: db.Conn, c: Check, s: Scenario, run: db.Row, gmail_id: str) -> None:
    step, prefix = s.log
    row = conn.execute(
        """SELECT 1 FROM processing_log l
            WHERE l.logged_at >= %(start)s AND l.step = %(step)s AND l.scope = 'e2e'
              AND starts_with(l.message, %(prefix)s)
              AND (l.source_ref = %(ref)s OR starts_with(l.source_ref, %(ref)s || ':')
                   OR l.invoice_id IN (SELECT id FROM invoices WHERE starts_with(source_ref, %(ref)s || ':')))""",
        {"start": run["started_at"], "step": step, "prefix": prefix, "ref": _gmail_ref(gmail_id)},
    ).fetchone()
    c.expect(row is not None, s.key, "log", f"no '{prefix}' entry ({step}) in the log")


def _check_invoice(conn: db.Conn, c: Check, s: Scenario, gmail_id: str) -> None:
    rows = conn.execute(
        """SELECT i.id, i.scope, i.status, i.status_reasons, i.total::text AS total,
                  EXISTS (SELECT 1 FROM review_queue r WHERE r.invoice_id = i.id AND r.status = 'open'
                                                         AND r.scope = 'e2e') AS in_review
             FROM invoices i WHERE starts_with(i.source_ref, %s)""",
        (_gmail_ref(gmail_id) + ":",),
    ).fetchall()
    if s.status is None:
        c.expect(not rows, s.key, "status", f"{len(rows)} invoice(s) stored, expected none")
        return
    if not c.expect(len(rows) == 1, s.key, "status", f"{len(rows)} invoices stored, expected 1"):
        return
    inv = rows[0]
    c.expect(inv["scope"] == "e2e", s.key, "isolation", f"stored as {inv['scope']} data")
    c.expect(inv["status"] == s.status, s.key, "status", f"status {inv['status']}, expected {s.status}")
    codes = {r["code"] for r in inv["status_reasons"]}
    missing = [r for r in s.reasons if r not in codes]
    c.expect(not missing, s.key, "status", f"reasons {sorted(codes)}, expected {list(s.reasons)}")
    if s.total is not None:
        c.expect(_same_amount(inv["total"], s.total), s.key, "status", f"total {inv['total']}, expected {s.total}")
    if s.status == "needs_review":
        c.expect(inv["in_review"], s.key, "review", "no open review item for it")
    if s.posted:
        _check_quickbooks(conn, c, s, inv["id"])


def _check_quickbooks(conn: db.Conn, c: Check, s: Scenario, invoice_id: Any) -> None:
    objects = {
        r["entity"]: r
        for r in conn.execute("SELECT * FROM e2e_qbo_objects WHERE invoice_id = %s", (invoice_id,)).fetchall()
    }
    bill, attachment = objects.get("Bill"), objects.get("Attachable")
    if not c.expect(bill is not None, s.key, "quickbooks", "no bill posted to QuickBooks"):
        return
    assert bill is not None
    total = bill["facts"].get("total")
    c.expect(_same_amount(total, s.total), s.key, "quickbooks", f"bill total {total}, expected {s.total}")
    c.expect(attachment is not None, s.key, "quickbooks", "no PDF attached to the bill")
    for o in (bill, attachment):
        if o is not None:
            c.expect(o["state"] == "deleted", s.key, "cleanup", f"{o['entity']} {o['external_id']} is {o['state']}")


def _same_amount(a: Any, b: str | None) -> bool:
    try:
        return b is not None and Decimal(str(a)) == Decimal(b)
    except InvalidOperation:
        return False


def _check_isolation(conn: db.Conn, c: Check, run: db.Row, refs: list[str]) -> None:
    """Nothing of the run's emails is recorded as live (or demo) data."""
    row = db.one(
        conn,
        """SELECT (SELECT count(*) FROM processing_log l, unnest(%(refs)s::text[]) r(ref)
                    WHERE l.logged_at >= %(start)s AND l.scope <> 'e2e'
                      AND (l.source_ref = r.ref OR starts_with(l.source_ref, r.ref || ':'))) AS log,
                  (SELECT count(*) FROM intake_failures f, unnest(%(ids)s::text[]) m(id)
                    WHERE f.message_id = m.id AND f.scope <> 'e2e') AS failures""",
        {"refs": refs, "ids": [r.removeprefix("gmail:") for r in refs], "start": run["started_at"]},
    )
    c.expect(row["log"] == 0, "all", "isolation", f"{row['log']} log entries of test emails recorded as live data")
    c.expect(row["failures"] == 0, "all", "isolation", "a test email's failure was recorded as live data")


def _check_no_full_account(conn: db.Conn, c: Check, run: db.Row) -> None:
    forms = [FULL_ACCOUNT_NUMBER, re.sub(r"\W", "", FULL_ACCOUNT_NUMBER)]
    row = db.one(
        conn,
        """SELECT count(*) AS n FROM processing_log l
            WHERE l.logged_at >= %s
              AND EXISTS (SELECT 1 FROM unnest(%s::text[]) f(form)
                           WHERE strpos(concat_ws(' ', l.message, l.source_ref, l.details::text), f.form) > 0)""",
        (run["started_at"], forms),
    )
    c.expect(row["n"] == 0, "fraud_alert", "masking", f"the full bank account number is in {row['n']} log entries")


def verify(conn: db.Conn, run_id: int, seen: list[dict[str, Any]], execution_id: str | None) -> db.Row:
    """Every check of the run, from the database and what n8n read in Gmail; the run is then finished."""
    run = conn.execute("SELECT * FROM e2e_runs WHERE id = %s FOR UPDATE", (run_id,)).fetchone()
    if run is None or run["status"] != "running":
        raise Blocked(f"run {run_id} is not in progress")
    by_scenario = {m["scenario"]: m for m in seen}
    c = Check()
    for s in SCENARIOS:
        m = by_scenario.get(s.key)
        if not c.expect(m is not None and bool(m.get("gmail_id")), s.key, "sent", "the email was not put in Gmail"):
            continue
        assert m is not None
        _check_gmail(c, s, m)
        _check_log(conn, c, s, run, m["gmail_id"])
        _check_invoice(conn, c, s, m["gmail_id"])
    _check_isolation(conn, c, run, [_gmail_ref(m["gmail_id"]) for m in seen if m.get("gmail_id")])
    _check_no_full_account(conn, c, run)
    status = "failed" if c.failures else "passed"
    row = db.one(
        conn,
        """UPDATE e2e_runs SET status = %s, finished_at = now(), checks = %s, failures = %s
            WHERE id = %s RETURNING *""",
        (status, c.passed, Jsonb(c.failures), run_id),
    )
    level = "error" if c.failures else "info"
    _log(conn, level, f"run {run_id} {status}", execution_id, run_id=run_id, checks=c.passed, failed=len(c.failures))
    return row


def abort(conn: db.Conn, run_id: int, step: str, message: str, execution_id: str | None) -> db.Row:
    """The workflow could not finish the run (a call failed): the run fails, naming the step."""
    run = conn.execute("SELECT * FROM e2e_runs WHERE id = %s FOR UPDATE", (run_id,)).fetchone()
    if run is None or run["status"] != "running":
        raise Blocked(f"run {run_id} is not in progress")
    failure = {"scenario": "run", "check": step[:100], "message": f"the run stopped at '{step}': {message}"[:500]}
    row = db.one(
        conn,
        """UPDATE e2e_runs SET status = 'failed', finished_at = now(), failures = failures || %s
            WHERE id = %s RETURNING *""",
        (Jsonb([failure]), run_id),
    )
    _log(conn, "error", f"run {run_id} failed: stopped at '{step[:100]}'", execution_id, run_id=run_id)
    return row


def latest(conn: db.Conn, hours: int) -> dict[str, Any]:
    """For the daily summary: the last run started in the period, and whether the test ever ran (before
    its first run there is nothing to report; after it, a day without a run is a failure)."""
    run = conn.execute(
        "SELECT * FROM e2e_runs WHERE started_at > now() - %s ORDER BY started_at DESC, id DESC LIMIT 1",
        (timedelta(hours=hours),),
    ).fetchone()
    ever = run is not None or conn.execute("SELECT 1 FROM e2e_runs LIMIT 1").fetchone() is not None
    return {"run": run, "set_up": ever}


def describe(run: db.Row | None, now: datetime, tz: tzinfo = UTC) -> tuple[bool, list[str]]:
    """(passed, lines) for the summary and the alert, times in `tz`. A missing or unfinished run is a failure."""
    if run is None:
        return False, ["No end-to-end test ran in the last 24 hours (workflow AP – Daily e2e test)."]
    at = f"{run['started_at'].astimezone(tz):%H:%M} {_zone_label(tz)}"
    if run["status"] == "running":
        if now - run["started_at"] < STALE_AFTER:
            return True, [f"Started at {at}, still running."]
        return False, [f"The run started at {at} did not finish (see n8n's executions list)."]
    if run["status"] == "passed":
        covered = ", ".join(s.title for s in SCENARIOS)
        return True, [f"Passed at {at}: {run['checks']} checks ({covered})."]
    lines = [f"FAILED at {at} ({len(run['failures'])} of {run['checks'] + len(run['failures'])} checks failed):"]
    lines += [f"{_scenario_title(f['scenario'])}: {f['message']}" for f in run["failures"]]
    return False, lines


def _zone_label(tz: tzinfo) -> str:
    """Europe/Paris → "Paris time"; UTC → "UTC"."""
    key = getattr(tz, "key", None)
    return f"{key.rsplit('/', 1)[-1].replace('_', ' ')} time" if key else str(tz)


def _scenario_title(key: str) -> str:
    return BY_KEY[key].title if key in BY_KEY else key


# =====================================================================================
# Setup (once): the test supplier's vendor link and default account
# =====================================================================================
def setup(conn: db.Conn, vendor_id: str, account_id: str) -> None:
    """Links the test supplier to its QuickBooks vendor and sets its default account, so the clean
    invoice goes straight through. Safe to run twice."""
    conn.execute(
        """INSERT INTO vendor_links (supplier_key, currency, external_id, display_name) VALUES (%s, %s, %s, %s)
           ON CONFLICT (system, supplier_key, currency) DO UPDATE SET external_id = EXCLUDED.external_id,
                       display_name = EXCLUDED.display_name""",
        (SUPPLIER_KEY, CURRENCY, vendor_id, SUPPLIER_NAME),
    )
    conn.execute(
        """INSERT INTO supplier_defaults (supplier_key, expense_account_id, source, set_by)
           VALUES (%s, %s, 'test_setup', %s)
           ON CONFLICT (supplier_key) DO UPDATE SET expense_account_id = EXCLUDED.expense_account_id""",
        (SUPPLIER_KEY, account_id, ACTOR),
    )
    _log(conn, "info", "test supplier set up", None, vendor_id=vendor_id, account_id=account_id)


def main() -> None:
    """CLI, in the core container: python -m app.e2e_test setup --vendor-id … --account-id …"""
    import argparse

    from .config import get_settings

    parser = argparse.ArgumentParser(prog="python -m app.e2e_test")
    sub = parser.add_subparsers(dest="command", required=True)
    s = sub.add_parser("setup", help="link the test supplier to its QuickBooks vendor and default account")
    s.add_argument("--vendor-id", required=True)
    s.add_argument("--account-id", required=True)
    args = parser.parse_args()
    with db.connect(get_settings().ap_database_url.get_secret_value()) as conn:
        setup(conn, args.vendor_id, args.account_id)
        conn.commit()
    print(f"test supplier {SUPPLIER_NAME} ({SUPPLIER_KEY}): vendor {args.vendor_id}, account {args.account_id}")


if __name__ == "__main__":
    main()
