"""The daily summary email: what happened in the last 24 hours and what still needs a person.

The core computes every figure and writes the wording; the n8n workflow only triggers it and sends
the email. Live data only (scope 'live'): test and demo data never reach it.

Touchless rate (docs/operations.md#daily-summary): of the documents received in the period that
were real invoices or credit notes (not "not an invoice", not the same file sent twice, not excluded
from posting), the share posted to QuickBooks without any validation or posting review item.
Unreadable documents count, as not touchless. Matching reviews don't count: payments come later.

Bank details appear only as their stored masked labels (last 4 characters), as everywhere else.

End-to-end test: the result of the morning's run (e2e_test.py). A run that failed, never finished or
didn't happen is a failure, named in the subject. The same wording is the alert sent when a run fails.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from html import escape
from typing import Any
from zoneinfo import ZoneInfo

from . import db, e2e_test, intake_failures
from .web.display import age
from .web.reasons import explain

REVIEW_LIST_LIMIT = 10  # open items listed one by one; the rest are counted
NOTHING = "Nothing to report in the last 24 hours."
FOOTER = "© David Michel · https://davidmichel.dev"
E2E_TITLE = "End-to-end test"
# Times in the email: the workflows' time zone (their schedules, e.g. 07:30 and 08:00, are Paris time)
TIME_ZONE = ZoneInfo("Europe/Paris")
# Left out of the touchless rate: nothing to process (a person could do nothing with them either)
NOT_PROCESSABLE = ("not_invoice", "duplicate_file")


@dataclass(frozen=True)
class Section:
    title: str
    lines: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Email:
    subject: str
    text: str
    html: str


# =====================================================================================
# Figures
# =====================================================================================
def figures(conn: db.Conn, hours: int = 24) -> dict[str, Any]:
    """Every figure of the summary, for the period ending now (the transaction's clock)."""
    end: datetime = db.one(conn, "SELECT now() AS t")["t"]
    period = {"start": end - timedelta(hours=hours), "end": end}
    return {
        "hours": hours,
        "end": end,
        "invoices": _invoice_counts(conn, period),
        "posted": _posted(conn, period),
        "payments": _payments(conn, period),
        "write_offs": _write_offs(conn, period),
        "open_reviews": _open_reviews(conn),
        "fraud_invoices": _invoices_with(conn, period, "status_reasons", "possible_bank_detail_change"),
        "fraud_emails": _fraud_emails(conn, period),
        "first_seen": _invoices_with(conn, period, "checks->'info'", "bank_details_first_seen"),
        "intake": intake_failures.summary(conn, hours),
        "e2e": e2e_test.latest(conn, hours),
    }


def _invoice_counts(conn: db.Conn, period: dict[str, datetime]) -> dict[str, int]:
    """Received in the period, by what intake decided, and the two parts of the touchless rate."""
    return dict(
        db.one(
            conn,
            """WITH received AS (
                 SELECT i.status,
                        EXISTS (SELECT 1 FROM review_queue r WHERE r.invoice_id = i.id AND r.stage = 'validation')
                          AS sent_to_review,
                        EXISTS (SELECT 1 FROM review_queue r
                                 WHERE r.invoice_id = i.id AND r.stage IN ('validation', 'posting')) AS touched,
                        EXISTS (SELECT 1 FROM accounting_postings p WHERE p.invoice_id = i.id AND p.status = 'posted')
                          AS posted,
                        i.posting_excluded_reason IS NULL
                          AND NOT EXISTS (SELECT 1 FROM jsonb_array_elements(i.status_reasons) x
                                           WHERE x->>'code' = ANY(%(left_out)s)) AS processable
                   FROM invoices i
                  WHERE i.scope = 'live' AND i.received_at > %(start)s AND i.received_at <= %(end)s)
               SELECT count(*) AS received,
                      count(*) FILTER (WHERE sent_to_review) AS to_review,
                      count(*) FILTER (WHERE status = 'rejected' AND NOT sent_to_review) AS rejected,
                      count(*) FILTER (WHERE processable) AS touchless_base,
                      count(*) FILTER (WHERE processable AND posted AND NOT touched) AS touchless
                 FROM received""",
            {**period, "left_out": list(NOT_PROCESSABLE)},
        )
    )


def _posted(conn: db.Conn, period: dict[str, datetime]) -> dict[str, int]:
    return dict(
        db.one(
            conn,
            """SELECT count(*) FILTER (WHERE p.object_type = 'bill') AS bills,
                      count(*) FILTER (WHERE p.object_type = 'vendor_credit') AS credits,
                      count(*) FILTER (WHERE i.received_at <= %(start)s) AS received_earlier
                 FROM accounting_postings p JOIN invoices i ON i.id = p.invoice_id
                WHERE i.scope = 'live' AND p.status = 'posted'
                  AND p.posted_at > %(start)s AND p.posted_at <= %(end)s""",
            period,
        )
    )


def _payments(conn: db.Conn, period: dict[str, datetime]) -> int:
    row = db.one(
        conn,
        """SELECT count(*) AS n FROM matches m JOIN invoices i ON i.id = m.invoice_id
            WHERE i.scope = 'live' AND m.external_payment_id IS NOT NULL
              AND m.payment_recorded_at > %(start)s AND m.payment_recorded_at <= %(end)s""",
        period,
    )
    return int(row["n"])


def _write_offs(conn: db.Conn, period: dict[str, datetime]) -> list[dict[str, Any]]:
    """Recorded write-offs per currency (never summed across currencies)."""
    rows = conn.execute(
        """SELECT w.currency, count(*) AS count, sum(w.amount) AS amount
             FROM write_offs w JOIN invoices i ON i.id = w.invoice_id
            WHERE i.scope = 'live' AND w.status = 'recorded'
              AND w.recorded_at > %(start)s AND w.recorded_at <= %(end)s
            GROUP BY w.currency ORDER BY w.currency""",
        period,
    ).fetchall()
    return [dict(r) for r in rows]


def _open_reviews(conn: db.Conn) -> list[dict[str, Any]]:
    """Every open live review item, oldest first (whenever it was opened)."""
    rows = conn.execute(
        """SELECT r.id, r.stage, r.reasons, r.created_at, r.waiting_for, i.supplier_name, i.invoice_number
             FROM review_queue r JOIN invoices i ON i.id = r.invoice_id
            WHERE r.scope = 'live' AND r.status = 'open'
            ORDER BY r.created_at, r.id""",
    ).fetchall()
    return [dict(r) for r in rows]


def _invoices_with(conn: db.Conn, period: dict[str, datetime], column: str, code: str) -> list[dict[str, Any]]:
    """Invoices received in the period whose reasons (or info notices) include the code, with their
    masked bank labels. `column` is one of two fixed expressions, never user input."""
    assert column in ("status_reasons", "checks->'info'")
    rows = conn.execute(
        f"""SELECT supplier_name, invoice_number,
                   ARRAY(SELECT a->>'label' FROM jsonb_array_elements(bank_accounts) a) AS accounts
              FROM invoices
             WHERE scope = 'live' AND received_at > %(start)s AND received_at <= %(end)s
               AND COALESCE({column}, '[]'::jsonb) @> jsonb_build_array(jsonb_build_object('code', %(code)s::text))
             ORDER BY received_at""",
        {**period, "code": code},
    ).fetchall()
    return [dict(r) for r in rows]


def _fraud_emails(conn: db.Conn, period: dict[str, datetime]) -> int:
    """Emails asking to change bank details with no invoice (labelled AP/Fraud alert, logged by n8n)."""
    row = db.one(
        conn,
        """SELECT count(*) AS n FROM processing_log
            WHERE scope = 'live' AND step = 'intake' AND message LIKE 'possible\\_bank\\_detail\\_change:%%'
              AND logged_at > %(start)s AND logged_at <= %(end)s""",
        period,
    )
    return int(row["n"])


# =====================================================================================
# Sections: one function each; a section with nothing to say returns None and is left out
# =====================================================================================
def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def fraud_section(f: dict[str, Any]) -> Section | None:
    lines = [
        f"{i['supplier_name'] or 'Unknown supplier'} {i['invoice_number'] or ''}".strip()
        + f": bank details changed ({', '.join(i['accounts']) or 'no account read'}). Check with the supplier"
        " by phone, using a number you already had."
        for i in f["fraud_invoices"]
    ]
    if f["fraud_emails"]:
        lines.append(
            f"{_plural(f['fraud_emails'], 'email')} asked to change bank details, with no invoice"
            " (Gmail label AP/Fraud alert)."
        )
    return Section("Fraud alerts", lines) if lines else None


def invoices_section(f: dict[str, Any]) -> Section | None:
    inv, posted = f["invoices"], f["posted"]
    lines = []
    if inv["received"]:
        ready = inv["received"] - inv["to_review"] - inv["rejected"]
        lines.append(
            f"Received: {inv['received']} ({ready} straight through, {inv['to_review']} sent to review,"
            f" {inv['rejected']} rejected)"
        )
    if posted["bills"] or posted["credits"]:
        kinds = [(posted["bills"], "bill"), (posted["credits"], "credit note")]
        earlier = f", incl. {posted['received_earlier']} received earlier" if posted["received_earlier"] else ""
        lines.append("Posted to QuickBooks: " + ", ".join(_plural(n, word) for n, word in kinds if n) + earlier)
    if f["payments"]:
        lines.append(f"Payments matched and recorded: {f['payments']}")
    for w in f["write_offs"]:
        lines.append(f"Written off: {w['currency']} {_money(w['amount'])} ({_plural(w['count'], 'bill')})")
    if inv["touchless_base"]:
        rate = round(100 * inv["touchless"] / inv["touchless_base"])
        lines.append(
            f"Touchless: {rate}% ({inv['touchless']} of {_plural(inv['touchless_base'], 'invoice')} received"
            " went to QuickBooks with no review)"
        )
    return Section("Invoices", lines) if lines else None


def review_section(f: dict[str, Any]) -> Section | None:
    items = f["open_reviews"]
    if not items:
        return None
    now = f["end"]
    lines = [_review_line(r, now) for r in items[:REVIEW_LIST_LIMIT]]
    if len(items) > REVIEW_LIST_LIMIT:
        lines.append(f"… and {len(items) - REVIEW_LIST_LIMIT} more on the review page.")
    return Section(f"Waiting for review ({len(items)}, oldest first)", lines)


def _review_line(r: dict[str, Any], now: datetime) -> str:
    labels = list(dict.fromkeys(explain(x["code"]).label for x in r["reasons"]))
    who = f"{r['supplier_name'] or 'Unknown supplier'} {r['invoice_number'] or ''}".strip()
    waiting = " (decided, waiting for QuickBooks)" if r["waiting_for"] else ""
    return f"{age(r['created_at'].isoformat(), now)}: {who}: {', '.join(labels)}{waiting}"


def first_seen_section(f: dict[str, Any]) -> Section | None:
    lines = [
        f"{i['supplier_name'] or 'Unknown supplier'}: {', '.join(i['accounts'])} (read from the invoice, not verified)"
        for i in f["first_seen"]
    ]
    return Section("Bank details seen for the first time", lines) if lines else None


def intake_section(f: dict[str, Any]) -> Section | None:
    s = f["intake"]
    lines = [
        f"Given up after {_plural(g['attempts'], 'attempt')}: email {g['message_id']}: {g['explanation']}"
        for g in s["given_up"]
    ]
    if s["retrying"]:
        lines.append(f"Still retrying: {_plural(s['retrying'], 'email')}")
    if s["recovered"]:
        lines.append(f"Recovered after a retry: {_plural(s['recovered'], 'email')}")
    if s["workflow_errors"]:
        lines.append(f"Failed workflow runs: {s['workflow_errors']} (see n8n's executions list)")
    return Section("Email intake problems", lines) if lines else None


def e2e_section(f: dict[str, Any]) -> Section | None:
    if not f["e2e"]["set_up"]:
        return None
    return Section(E2E_TITLE, e2e_test.describe(f["e2e"]["run"], f["end"], TIME_ZONE)[1])


def _e2e_failed(f: dict[str, Any]) -> bool:
    return bool(f["e2e"]["set_up"]) and not e2e_test.describe(f["e2e"]["run"], f["end"])[0]


# In reading order. A new part of the summary is one more function here.
SECTIONS = (fraud_section, invoices_section, review_section, first_seen_section, intake_section, e2e_section)


def _money(amount: Decimal) -> str:
    return f"{amount:,.2f}"


# =====================================================================================
# The email
# =====================================================================================
def sections(f: dict[str, Any]) -> list[Section]:
    return [s for build in SECTIONS if (s := build(f)) is not None]


def subject(f: dict[str, Any], parts: list[Section]) -> str:
    day = f["end"].strftime("%-d %b %Y")
    failed = _e2e_failed(f)
    if not failed and all(s.title == E2E_TITLE for s in parts):
        return f"AP daily summary, {day}: nothing to report"
    alerts = len(f["fraud_invoices"]) + f["fraud_emails"]
    bits = [f"{f['invoices']['received']} received", f"{len(f['open_reviews'])} waiting for review"]
    if alerts:
        bits.insert(0, _plural(alerts, "fraud alert"))
    if failed:
        bits.insert(0, "end-to-end test FAILED")
    return f"AP daily summary, {day}: {', '.join(bits)}"


def render(f: dict[str, Any]) -> Email:
    parts = sections(f)
    return Email(subject(f, parts), _text(parts), _html(parts))


def _text(parts: list[Section]) -> str:
    blocks = [s.title + "\n" + "\n".join(f"- {line}" for line in s.lines) for s in parts] or [NOTHING]
    return "\n\n".join([*blocks, FOOTER]) + "\n"


def _html(parts: list[Section]) -> str:
    body = (
        "".join(
            f'<h2 style="font-size:16px;margin:20px 0 6px">{escape(s.title)}</h2>'
            '<ul style="margin:0;padding-left:20px">'
            + "".join(f'<li style="margin:4px 0">{escape(line)}</li>' for line in s.lines)
            + "</ul>"
            for s in parts
        )
        or f"<p>{escape(NOTHING)}</p>"
    )
    footer = (
        '<p style="margin-top:28px;color:#666;font-size:13px"><a href="https://davidmichel.dev">© David Michel</a></p>'
    )
    return (
        '<div style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;font-size:15px;line-height:1.45;'
        f'max-width:600px;color:#1a1a1a">{body}{footer}</div>'
    )


def e2e_alert(run: db.Row, recipients: list[str], now: datetime) -> dict[str, str]:
    """The email sent at once when a run fails: the summary's end-to-end section on its own."""
    parts = [Section(E2E_TITLE, e2e_test.describe(run, now, TIME_ZONE)[1])]
    return {
        "to": ", ".join(recipients),
        "subject": f"AP end-to-end test FAILED (run {run['id']})",
        "text": _text(parts),
        "html": _html(parts),
    }


def daily(conn: db.Conn, recipients: list[str], hours: int = 24) -> dict[str, Any]:
    """What the workflow sends: recipients, subject, plain text and HTML, plus the figures."""
    f = figures(conn, hours)
    email = render(f)
    return {
        "to": ", ".join(recipients),
        "subject": email.subject,
        "text": email.text,
        "html": email.html,
        "figures": db.jsonable(f),
    }
