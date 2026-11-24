"""Review decisions that take effect in QuickBooks: bank matches, payment differences, retries.

  confirm_match  ambiguous match: the reviewer picks the bank transaction that pays the bill; the bill
                 payment is then recorded by the bank workflow (as for an automatic match)
  reject_match   none of the candidate transactions pays the bill: they are never proposed again
  write_off      under-payment: the open part is written off (a vendor credit on the payment-differences
                 account, applied to the bill by a bill payment of 0: no money moves)
  keep_open      under-payment: the open part stays open on the bill
  retry          after a failed QuickBooks call: the next run tries again

A decision that needs QuickBooks (confirm, write-off, retry) is recorded at once (reviewer, decision,
audit log), and the item stays open, waiting, until the workflow reports the effect recorded; it then
closes (accounting.effect_recorded). A failure that leaves no automatic retry shows on the item, which
then offers a retry. Every QuickBooks document carries a fixed number, searched before it is created,
so a retry never records anything twice.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, Literal

from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field

from . import db, matching, postings, review
from .accounting import AccountingConfig, Conflict, NotFound, Waiting, effect_recorded, quickbooks_failed

Closes = Literal["approved", "rejected", "written_off"]


class Settlement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reviewer: str = Field(min_length=1, max_length=100)
    note: str | None = Field(default=None, max_length=1000)


class MatchChoice(Settlement):
    bank_transaction_id: str = Field(min_length=1, max_length=100)


# =====================================================================================
# Decisions
# =====================================================================================
def decide(conn: db.Conn, review_id: int, action: str, body: Settlement, scope: review.Scope | None = None) -> db.Row:
    """Applies one decision on a posting or matching item (the core checks it is allowed now)."""
    if action not in review.SETTLEMENT_ACTIONS:
        raise Conflict(f"'{action}' is not a decision on a posting or matching item")
    item = review._open_item(conn, review_id, action, scope)  # type: ignore[arg-type]
    if action == "confirm_match":
        if not isinstance(body, MatchChoice):
            raise Conflict("choose the bank transaction that pays this bill")
        _confirm_match(conn, item, body)
    elif action == "reject_match":
        _reject_match(conn, item, body)
    elif action == "write_off":
        _write_off(conn, item, body)
    elif action == "keep_open":
        _keep_open(conn, item, body)
    else:
        _retry(conn, item, body)
    return review._item(conn, review_id)


def _record(
    conn: db.Conn,
    item: db.Row,
    body: Settlement,
    decision: dict[str, Any],
    closes_as: Closes,
    waiting_for: Waiting | None = None,
) -> None:
    """Records who decided what; closes the item now, or leaves it open waiting for QuickBooks."""
    decision = {**decision, "closes_as": closes_as}
    note = (body.note or "").strip() or None
    if waiting_for:
        conn.execute(
            """UPDATE review_queue SET reviewer = %s, decision_note = %s, decision = %s, waiting_for = %s,
                      effect_error = NULL, updated_at = now() WHERE id = %s""",
            (body.reviewer, note, Jsonb(decision), waiting_for, item["id"]),
        )
    else:
        conn.execute(
            """UPDATE review_queue SET status = %s, reviewer = %s, decision_note = %s, decision = %s,
                      waiting_for = NULL, effect_error = NULL, resolved_at = now(), updated_at = now()
                WHERE id = %s""",
            (closes_as, body.reviewer, note, Jsonb(decision), item["id"]),
        )
    state = f"waiting for QuickBooks ({waiting_for})" if waiting_for else closes_as
    review._log(conn, item, f"review {decision['action']}: {state}", {**decision, "reviewer": body.reviewer})


def _posting(conn: db.Conn, invoice_id: Any) -> db.Row:
    row = conn.execute("SELECT * FROM accounting_postings WHERE invoice_id = %s FOR UPDATE", (invoice_id,)).fetchone()
    if row is None:
        raise NotFound("this invoice has no posting")
    return row


def _confirm_match(conn: db.Conn, item: db.Row, choice: MatchChoice) -> None:
    tx = conn.execute(
        "SELECT item_key FROM bank_transactions WHERE transaction_id = %s", (choice.bank_transaction_id,)
    ).fetchone()
    if tx is None:
        raise Conflict("unknown bank transaction")
    db.lock(conn, f"matching:{tx['item_key']}")  # never at the same time as a matching run
    invoice_id = item["invoice_id"]
    m = conn.execute(
        "SELECT * FROM matches WHERE invoice_id = %s AND bank_transaction_id = %s FOR UPDATE",
        (invoice_id, choice.bank_transaction_id),
    ).fetchone()
    if m is None or m["status"] != "candidate":
        raise Conflict("this bank transaction is not a candidate for this bill")
    taken = conn.execute(
        "SELECT invoice_id FROM matches WHERE bank_transaction_id = %s AND status = 'accepted'",
        (choice.bank_transaction_id,),
    ).fetchone()
    if taken:
        raise Conflict("this bank transaction already pays another bill")
    conn.execute("UPDATE matches SET status = 'accepted' WHERE id = %s", (m["id"],))
    conn.execute(
        "UPDATE matches SET status = 'dismissed' WHERE invoice_id = %s AND status = 'candidate'", (invoice_id,)
    )
    others = conn.execute(
        """UPDATE matches SET status = 'dismissed' WHERE bank_transaction_id = %s AND status = 'candidate'
        RETURNING invoice_id""",
        (choice.bank_transaction_id,),
    ).fetchall()
    _posting(conn, invoice_id)
    conn.execute(
        "UPDATE accounting_postings SET payment_reason = 'payment_pending' WHERE invoice_id = %s", (invoice_id,)
    )
    decision = {
        "action": "confirm_match",
        "match_id": m["id"],
        "bank_transaction_id": choice.bank_transaction_id,
        "bank_amount": str(m["bank_amount"]),
        "difference": None if m["difference"] is None else str(m["difference"]),
    }
    _record(conn, item, choice, decision, "approved", waiting_for="payment")
    for other in {o["invoice_id"] for o in others}:
        _close_if_no_candidate_left(conn, other, item, choice)


def _close_if_no_candidate_left(conn: db.Conn, invoice_id: Any, decided: db.Row, choice: MatchChoice) -> None:
    """Another bill's matching item waited on the same transaction: settled by this decision when it has
    no candidate left (its bill stays unpaid and is matched again on later runs)."""
    left = conn.execute(
        "SELECT 1 FROM matches WHERE invoice_id = %s AND status = 'candidate'", (invoice_id,)
    ).fetchone()
    if left:
        return
    conn.execute(
        """UPDATE accounting_postings SET payment_reason = 'no_match_yet'
            WHERE invoice_id = %s AND payment_reason = ANY(%s)""",
        (invoice_id, list(matching.CANDIDATE_REVIEW_CODES)),
    )
    other = conn.execute(
        """SELECT * FROM review_queue WHERE invoice_id = %s AND status = 'open' AND stage = 'matching'
           FOR UPDATE""",
        (invoice_id,),
    ).fetchone()
    if other is None:
        return
    decision = {
        "action": "closed_by_other_decision",
        "review_id": decided["id"],
        "bank_transaction_id": choice.bank_transaction_id,
    }
    body = Settlement(reviewer=choice.reviewer, note=f"The transaction pays another bill (review {decided['id']}).")
    _record(conn, other, body, decision, "rejected")


def _reject_match(conn: db.Conn, item: db.Row, body: Settlement) -> None:
    if len((body.note or "").strip()) < review.MIN_REJECT_NOTE:
        raise Conflict("say why none of these transactions pays the bill")
    dismissed = conn.execute(
        """UPDATE matches SET status = 'dismissed' WHERE invoice_id = %s AND status = 'candidate'
        RETURNING bank_transaction_id""",
        (item["invoice_id"],),
    ).fetchall()
    _posting(conn, item["invoice_id"])
    conn.execute(
        "UPDATE accounting_postings SET payment_reason = 'no_match_yet' WHERE invoice_id = %s", (item["invoice_id"],)
    )
    decision = {"action": "reject_match", "dismissed": [d["bank_transaction_id"] for d in dismissed]}
    _record(conn, item, body, decision, "rejected")


def _open_part(posting: db.Row) -> Decimal:
    if posting["payment_status"] != "paid_with_difference":
        raise Conflict(f"no payment difference to settle (payment status {posting['payment_status']})")
    open_part: Decimal = posting["amount"] - posting["amount_paid"]
    if open_part <= 0:
        raise Conflict("the bill is not under-paid: nothing to write off")
    return open_part


def _write_off(conn: db.Conn, item: db.Row, body: Settlement) -> None:
    posting = _posting(conn, item["invoice_id"])
    amount = _open_part(posting)
    m = db.one(
        conn,
        """SELECT id FROM matches WHERE invoice_id = %s AND status = 'accepted' AND external_payment_id IS NOT NULL""",
        (item["invoice_id"],),
    )
    wo = db.one(
        conn,
        """INSERT INTO write_offs (match_id, invoice_id, review_id, amount, currency, decided_by)
           VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
        (m["id"], item["invoice_id"], item["id"], amount, posting["currency"], body.reviewer),
    )
    decision = {"action": "write_off", "write_off_id": wo["id"], "amount": str(amount), "currency": posting["currency"]}
    _record(conn, item, body, decision, "written_off", waiting_for="write_off")


def _keep_open(conn: db.Conn, item: db.Row, body: Settlement) -> None:
    posting = _posting(conn, item["invoice_id"])
    decision = {"action": "keep_open", "open_amount": str(_open_part(posting)), "currency": posting["currency"]}
    _record(conn, item, body, decision, "approved")


def _retry_target(conn: db.Conn, invoice_id: Any) -> Waiting:
    """What failed and is tried again: a write-off, a bill payment, or the bill posting."""
    wo = conn.execute(
        """UPDATE write_offs SET attempts = 0, claimed_until = NULL
            WHERE invoice_id = %s AND status = 'due' RETURNING id""",
        (invoice_id,),
    ).fetchone()
    if wo:
        return "write_off"
    payment = conn.execute(
        """UPDATE matches SET payment_attempts = 0, payment_claimed_until = NULL
            WHERE invoice_id = %s AND status = 'accepted' AND external_payment_id IS NULL RETURNING id""",
        (invoice_id,),
    ).fetchone()
    if payment:
        return "payment"
    posting = conn.execute(
        """UPDATE accounting_postings SET attempts = 0, next_attempt_at = now(), claimed_until = NULL
            WHERE invoice_id = %s AND (status = 'failed' OR (status = 'posted' AND attachment_external_id IS NULL))
        RETURNING id""",
        (invoice_id,),
    ).fetchone()
    if posting:
        return "posting"
    raise Conflict("nothing to retry: no failed posting, payment or write-off for this invoice")


def _retry(conn: db.Conn, item: db.Row, body: Settlement) -> None:
    target = _retry_target(conn, item["invoice_id"])
    earlier = item["decision"] or {}
    if earlier.get("closes_as"):  # a confirmed match or write-off: its decision stands, only retried
        conn.execute(
            """UPDATE review_queue SET waiting_for = %s, effect_error = NULL, updated_at = now(),
                      decision = decision || %s WHERE id = %s""",
            (target, Jsonb({"retries": int(earlier.get("retries", 0)) + 1}), item["id"]),
        )
        review._log(conn, item, f"review retry: waiting for QuickBooks ({target})", {"reviewer": body.reviewer})
        return
    decision = {"action": "retry", "retried": target, "error": item["effect_error"] or _reason_detail(item)}
    _record(conn, item, body, decision, "approved", waiting_for=target)


def _reason_detail(item: db.Row) -> str:
    return "; ".join(str(r.get("detail") or r.get("message") or r["code"]) for r in item["reasons"])[:500]


# =====================================================================================
# Write-offs in QuickBooks (worked through by the bank workflow)
# =====================================================================================
def write_off_number(write_off_id: int) -> str:
    return f"AP-WO-{write_off_id}"


def write_offs_due(conn: db.Conn, cfg: AccountingConfig) -> list[dict[str, Any]]:
    """Write-offs to record now, each leased (two overlapping runs never get the same one)."""
    rows = conn.execute(
        """WITH due AS (
             SELECT w.id FROM write_offs w
              WHERE w.status = 'due' AND w.attempts < %(max)s
                AND (w.claimed_until IS NULL OR w.claimed_until < now())
              ORDER BY w.id FOR UPDATE SKIP LOCKED)
           UPDATE write_offs w SET claimed_until = now() + make_interval(mins => %(lease)s), attempts = w.attempts + 1
             FROM due, accounting_postings p
            WHERE w.id = due.id AND p.invoice_id = w.invoice_id
        RETURNING w.*, p.external_id AS bill_id, p.vendor_external_id, p.doc_number""",
        {"max": cfg.max_attempts, "lease": cfg.lease_minutes},
    ).fetchall()
    return [_write_off_work(cfg, w) for w in sorted(rows, key=lambda r: r["id"])]


def _write_off_work(cfg: AccountingConfig, w: db.Row) -> dict[str, Any]:
    number = write_off_number(w["id"])
    return {
        "write_off_id": w["id"],
        "invoice_id": str(w["invoice_id"]),
        "doc_number": number,
        "doc_number_q": postings.qbo_quote(number),
        "credit_request_id": f"ap-wo-credit-{w['id']}",
        "apply_request_id": f"ap-wo-apply-{w['id']}",
        "credit": {
            "VendorRef": {"value": w["vendor_external_id"]},
            "DocNumber": number,
            "TxnDate": date.today().isoformat(),
            "CurrencyRef": {"value": w["currency"]},
            "PrivateNote": f"AP Automation · payment difference on bill {w['doc_number']} written off by "
            f"{w['decided_by']} (review {w['review_id']})",
            "Line": [
                {
                    "Amount": float(w["amount"]),
                    "Description": f"Payment difference on bill {w['doc_number']}",
                    "DetailType": "AccountBasedExpenseLineDetail",
                    "AccountBasedExpenseLineDetail": {"AccountRef": {"value": cfg.write_off_account_id}},
                }
            ],
        },
    }


def _due_write_off(conn: db.Conn, write_off_id: int) -> db.Row:
    w = conn.execute("SELECT * FROM write_offs WHERE id = %s FOR UPDATE", (write_off_id,)).fetchone()
    if w is None:
        raise NotFound("unknown write-off")
    return w


def _vendor(conn: db.Conn, w: db.Row) -> str:
    row = db.one(conn, "SELECT vendor_external_id FROM accounting_postings WHERE invoice_id = %s", (w["invoice_id"],))
    return str(row["vendor_external_id"])


def write_off_lookup(
    conn: db.Conn, write_off_id: int, kind: Literal["credit", "apply"], status: int, body: Any
) -> postings.Decision:
    """A vendor credit (or the bill payment applying it) with this number may exist already."""
    w = _due_write_off(conn, write_off_id)
    entity = "VendorCredit" if kind == "credit" else "BillPayment"
    return postings.existing_document_decision(status, body, entity, _vendor(conn, w))


def record_write_off_credit(
    conn: db.Conn, cfg: AccountingConfig, write_off_id: int, external_credit_id: str
) -> dict[str, Any]:
    """The vendor credit exists in QuickBooks; returns the bill payment of 0 that applies it to the bill."""
    w = _due_write_off(conn, write_off_id)
    if w["external_credit_id"] and w["external_credit_id"] != external_credit_id:
        raise Conflict(f"vendor credit already recorded as {w['external_credit_id']}")
    conn.execute("UPDATE write_offs SET external_credit_id = %s WHERE id = %s", (external_credit_id, write_off_id))
    p = db.one(
        conn,
        "SELECT external_id, vendor_external_id FROM accounting_postings WHERE invoice_id = %s",
        (w["invoice_id"],),
    )
    amount = float(w["amount"])
    return {
        "write_off_id": write_off_id,
        "apply": {
            "VendorRef": {"value": p["vendor_external_id"]},
            "DocNumber": write_off_number(write_off_id),
            "PayType": "Check",
            "CheckPayment": {"BankAccountRef": {"value": cfg.bank_account_id}, "PrintStatus": "NotSet"},
            "CurrencyRef": {"value": w["currency"]},
            "TotalAmt": 0,  # applies the credit to the bill: no money moves
            "TxnDate": date.today().isoformat(),
            "PrivateNote": f"AP Automation · applies vendor credit {external_credit_id} (payment difference)",
            "Line": [
                {"Amount": amount, "LinkedTxn": [{"TxnId": p["external_id"], "TxnType": "Bill"}]},
                {"Amount": amount, "LinkedTxn": [{"TxnId": external_credit_id, "TxnType": "VendorCredit"}]},
            ],
        },
    }


def record_write_off_applied(
    conn: db.Conn, write_off_id: int, external_payment_id: str, execution_id: str | None = None
) -> db.Row:
    w = _due_write_off(conn, write_off_id)
    if w["status"] == "recorded":
        if w["external_payment_id"] != external_payment_id:
            raise Conflict(f"write-off already recorded with payment {w['external_payment_id']}")
        return w  # idempotent repeat
    if not w["external_credit_id"]:
        raise Conflict("record the vendor credit first")
    conn.execute(
        """UPDATE write_offs SET status = 'recorded', external_payment_id = %s, recorded_at = now(),
                  claimed_until = NULL, last_error = NULL WHERE id = %s""",
        (external_payment_id, write_off_id),
    )
    conn.execute(
        """UPDATE accounting_postings SET payment_status = 'paid_with_write_off', payment_reason = NULL,
                  amount_written_off = %s WHERE invoice_id = %s""",
        (w["amount"], w["invoice_id"]),
    )
    db.log(
        conn,
        step="match",
        level="info",
        message="payment difference written off",
        invoice_id=str(w["invoice_id"]),
        channel="system",
        execution_id=execution_id,
        details={
            "write_off_id": write_off_id,
            "amount": str(w["amount"]),
            "vendor_credit": w["external_credit_id"],
            "bill_payment": external_payment_id,
        },
    )
    effect_recorded(conn, str(w["invoice_id"]), "write_off")
    return _due_write_off(conn, write_off_id)


def record_write_off_failure(conn: db.Conn, cfg: AccountingConfig, write_off_id: int, r: dict[str, Any]) -> db.Row:
    """Retried on the next runs while attempts remain; then shown on the review item, which offers a retry."""
    w = _due_write_off(conn, write_off_id)
    code, retryable = postings.classify_failure(r.get("error_code"), r.get("http_status"), r.get("fault_code"), ())
    final = not retryable or w["attempts"] >= cfg.max_attempts
    quickbooks_failed(conn, str(w["invoice_id"]), code, r.get("error_message"), final)
    conn.execute(
        """UPDATE write_offs SET claimed_until = NULL, last_error = %s,
                  attempts = CASE WHEN %s THEN %s ELSE attempts END WHERE id = %s""",
        (f"{code}: {(r.get('error_message') or '')[:500]}", final, cfg.max_attempts, write_off_id),
    )
    db.log(
        conn,
        step="match",
        level="error" if final else "warning",
        message="write-off not recorded",
        invoice_id=str(w["invoice_id"]),
        channel="system",
        execution_id=r.get("execution_id"),
        details={"code": code, "retryable": not final, "write_off_id": write_off_id},
    )
    return _due_write_off(conn, write_off_id)
