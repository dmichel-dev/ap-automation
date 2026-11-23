"""End-to-end check: compares what the pipeline stored with test-data/invoices/*.expected.json.

Run with scripts/run_e2e.sh (it passes the database URLs from .env). Read-only.
  --since   ISO timestamp of the start of the run (only rows created after it count)
  --timeout seconds to wait for all results (emails are polled once a minute)

Matching: by the attachment's SHA-256, because the Gmail message ID differs between the
sending and the receiving mailbox. 'occurrence' picks the n-th row with the same file
(used by the duplicate test).
"""

import argparse
import hashlib
import json
import os
import sys
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

TEST_DIR = Path(os.environ.get("TEST_DATA_DIR", "/test-data/invoices"))
WORKFLOW_ID = "apEmailIntake001"


def load_expectations() -> list[dict]:
    items = []
    for f in sorted(TEST_DIR.glob("*.expected.json")):
        e = json.loads(f.read_text())
        e["sha256"] = hashlib.sha256((TEST_DIR / e["file"]).read_bytes()).hexdigest()
        items.append(e)
    return items


def fetch_row(conn, e: dict, since: datetime) -> dict | None:
    rows = conn.execute(
        """SELECT i.*, (SELECT count(*) FROM ap.invoice_lines l WHERE l.invoice_id = i.id) AS line_count
             FROM ap.invoices i WHERE file_sha256 = %s AND created_at >= %s ORDER BY created_at""",
        (e["sha256"], since),
    ).fetchall()
    n = e["expect"].get("occurrence", 1)
    return rows[n - 1] if len(rows) >= n else None


def fetch_log(conn, message: str, since: datetime) -> int:
    return conn.execute(
        "SELECT count(*) AS n FROM ap.processing_log WHERE message = %s AND logged_at >= %s", (message, since)
    ).fetchone()["n"]


TEXT_KEYS = ("document_type", "invoice_number", "currency", "supplier_country", "related_invoice_number")
DATE_KEYS = ("invoice_date", "due_date", "service_period_start", "service_period_end")


def _iso(value):
    return value.isoformat() if value else None


def actual_values(row: dict) -> dict:
    """Every value an expected result can name, read from the invoice row in comparable form."""
    out = {key: (row[key] or "").strip() or None if isinstance(row[key], str) else row[key] for key in TEXT_KEYS}
    out.update({key: _iso(row[key]) for key in DATE_KEYS})
    out["status"] = row["status"]
    out["reasons"] = sorted(r["code"] for r in row["status_reasons"])
    # information-only flags (posted anyway), e.g. cutoff_prior_period
    out["info"] = sorted(i["code"] for i in (row["checks"] or {}).get("info", []))
    out["total"] = row["total"]
    out["suggested order"] = row["date_order_suggested"]
    out["suggested invoice date"] = _iso(row["invoice_date_suggested"])
    return out


def expected_values(exp: dict) -> dict:
    out = {k: v for k, v in exp.items() if k in (*TEXT_KEYS, *DATE_KEYS, "status")}
    out["reasons"] = sorted(exp["reasons"])
    if "info" in exp:
        out["info"] = sorted(exp["info"])
    if "total" in exp:
        out["total"] = Decimal(exp["total"]) if exp["total"] is not None else None
    if "suggested_dates" in exp:
        out["suggested order"] = exp["suggested_dates"]["order"]
        out["suggested invoice date"] = exp["suggested_dates"]["invoice_date"]
    return out


def compare(e: dict, row: dict) -> list[str]:
    exp, actual = e["expect"], actual_values(row)
    diffs = [
        f"{name}: expected {expected!r}, got {actual[name]!r}"
        for name, expected in expected_values(exp).items()
        if actual[name] != expected
    ]
    if "min_lines" in exp and row["line_count"] < exp["min_lines"]:
        diffs.append(f"lines: expected at least {exp['min_lines']}, got {row['line_count']}")
    return diffs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", required=True, help="ISO timestamp of the start of the run")
    ap.add_argument("--timeout", type=int, default=600)
    ap.add_argument("--json", help="write the results to this file")
    args = ap.parse_args()
    since = datetime.fromisoformat(args.since)
    if since.tzinfo is None:
        since = since.replace(tzinfo=UTC)

    expectations = load_expectations()
    deadline = time.monotonic() + args.timeout
    with psycopg.connect(os.environ["AP_E2E_DATABASE_URL"], row_factory=dict_row, autocommit=True) as ap_conn:
        # Wait until every expected result is there (or the timeout)
        while True:
            missing = [
                e["id"]
                for e in expectations
                if (
                    fetch_log(ap_conn, e["expect"]["log_message"], since) == 0
                    if e["expect"].get("no_invoice_row")
                    else fetch_row(ap_conn, e, since) is None
                )
            ]
            if not missing or time.monotonic() > deadline:
                break
            print(f"waiting for: {', '.join(missing)}", flush=True)
            time.sleep(15)

        results = []
        for e in expectations:
            exp = e["expect"]
            if exp.get("no_invoice_row"):
                logged = fetch_log(ap_conn, exp["log_message"], since)
                row = fetch_row(ap_conn, e, since)
                diffs = ([] if logged else ["no log entry"]) + (["an invoice row was created"] if row else [])
                results.append({"id": e["id"], "passed": not diffs, "diffs": diffs, "actual": {"log_entries": logged}})
                continue
            row = fetch_row(ap_conn, e, since)
            if row is None:
                results.append({"id": e["id"], "passed": False, "diffs": ["no result (timeout)"], "actual": None})
                continue
            diffs = compare(e, row)
            results.append(
                {
                    "id": e["id"],
                    "passed": not diffs,
                    "diffs": diffs,
                    "actual": {
                        "status": row["status"],
                        "reasons": [r["code"] for r in row["status_reasons"]],
                        "total": str(row["total"]) if row["total"] is not None else None,
                        "currency": row["currency"],
                        "confidence": float(row["confidence"]) if row["confidence"] is not None else None,
                        "ocr": float(row["ocr_confidence_avg"]) if row["ocr_confidence_avg"] is not None else None,
                        "tax_mode": row["tax_mode"],
                        "lines": row["line_count"],
                    },
                }
            )

    with psycopg.connect(os.environ["N8N_E2E_DATABASE_URL"], row_factory=dict_row) as n8n_conn:
        executions = n8n_conn.execute(
            """SELECT status, count(*) AS n FROM public.execution_entity
                WHERE "workflowId" = %s AND "startedAt" >= %s GROUP BY status""",
            (WORKFLOW_ID, since),
        ).fetchall()
    exec_summary = {r["status"]: r["n"] for r in executions}
    exec_ok = bool(exec_summary) and set(exec_summary) == {"success"}

    width = max(len(r["id"]) for r in results)
    for r in results:
        a = r["actual"] or {}
        extra = (
            f"  status={a.get('status')} conf={a.get('confidence')} reasons={a.get('reasons')}" if "status" in a else ""
        )
        print(f"{'PASS' if r['passed'] else 'FAIL'}  {r['id']:<{width}}{extra}")
        for d in r["diffs"]:
            print(f"      - {d}")
    print(f"{'PASS' if exec_ok else 'FAIL'}  n8n executions since start: {exec_summary or 'none'}")
    passed = sum(r["passed"] for r in results)
    print(f"\n{passed}/{len(results)} documents passed, executions {'ok' if exec_ok else 'NOT ok'}")

    if args.json:
        Path(args.json).write_text(
            json.dumps(
                {"since": since.isoformat(), "results": results, "executions": exec_summary},
                indent=2,
                ensure_ascii=False,
            )
            + "\n"
        )
    return 0 if passed == len(results) and exec_ok else 1


if __name__ == "__main__":
    sys.exit(main())
