"""Review decisions on invoices that validation sent to a person, and which actions each item allows.

The core decides which actions an item allows; the review page only offers those.
  approve  the reviewer accepts the invoice (after corrections, if any) and chooses its account
  verify   fraud items only: the reviewer confirmed with the supplier, a note says how
  correct  fix fields printed or read wrongly; the item stays open until approved or rejected
  reject   with a reason; the invoice is not posted and its file is deleted

Approving re-runs the deterministic checks a bill needs (posting_blockers). The judgement reasons
(possible duplicate, new supplier, capex, cut-off, low confidence) are settled by the decision.
Posting and matching items take effect in QuickBooks; their decisions are in settlement.py.

Every read and decision takes an optional scope: the review page passes the signed-in user's scope,
so an item of another scope is "unknown" (404) to it; the machine API (bearer token) passes None.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Literal

from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field

from . import db, qbo_reference
from .accounting import Conflict, NotFound
from .models import CommonInvoice, DateInfo, ExtractionMeta, InvoiceLine, Issue, Normalized, Party, TaxLine
from .normalize import normalize_invoice_number, normalize_supplier_name, normalize_tax_id
from .validation import DUPLICATE_DATE_WINDOW_DAYS, DUPLICATE_RULES, posting_blockers

Action = Literal[
    "approve", "verify", "correct", "reject", "confirm_match", "reject_match", "write_off", "keep_open", "retry"
]
SETTLEMENT_ACTIONS = frozenset({"confirm_match", "reject_match", "write_off", "keep_open", "retry"})
Scope = Literal["live", "e2e", "demo"]
FRAUD_CODES = frozenset({"possible_bank_detail_change"})
# Reasons whose approval must name the account (never the supplier's default by fallback): an item
# that may be a fixed asset needs an explicit expense-or-asset choice
ACCOUNT_CHOICE_CODES = frozenset({"possible_capex"})
MIN_VERIFY_NOTE = 10  # "how was it verified" must say something
MIN_REJECT_NOTE = 3


class Blocked(Conflict):
    """The invoice can't be approved yet: these problems must be corrected first."""

    def __init__(self, problems: list[Issue]) -> None:
        super().__init__("correct these first: " + ", ".join(p.code for p in problems))
        self.problems = problems


class AccountRequired(Conflict):
    """The decision must name the account: the supplier's default isn't used for this item."""


def account_choice_required(item: db.Row) -> bool:
    return bool({r["code"] for r in item["reasons"]} & ACCOUNT_CHOICE_CODES)


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reviewer: str = Field(min_length=1, max_length=100)
    account_id: str | None = Field(default=None, max_length=50)  # required when the invoice has none
    # None: saved as the supplier's default only if it has none yet; True: replace it; False: don't save
    set_default: bool | None = None
    accept_suggested_dates: bool = False  # ambiguous dates: take the suggested reading
    note: str | None = Field(default=None, max_length=1000)


class Rejection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reviewer: str = Field(min_length=1, max_length=100)
    note: str = Field(min_length=MIN_REJECT_NOTE, max_length=1000)


class Corrections(BaseModel):
    """Fields a reviewer may correct. Only the fields sent are changed; null clears an optional one."""

    model_config = ConfigDict(extra="forbid")

    document_type: Literal["invoice", "credit_note"] | None = None
    supplier_name: str | None = Field(default=None, min_length=1, max_length=200)
    supplier_tax_id: str | None = Field(default=None, max_length=50)
    invoice_number: str | None = Field(default=None, min_length=1, max_length=100)
    invoice_date: date | None = None
    due_date: date | None = None
    service_period_start: date | None = None
    service_period_end: date | None = None
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    subtotal: Decimal | None = None
    tax_total: Decimal | None = None
    total: Decimal | None = None


class Correction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reviewer: str = Field(min_length=1, max_length=100)
    changes: Corrections


CANNOT_CLEAR = ("document_type", "supplier_name", "invoice_number", "invoice_date", "currency", "total")
# Columns derived from a corrected field (the duplicate keys); supplier_key follows by itself
DERIVED = {
    "supplier_name": ("supplier_name_norm", normalize_supplier_name),
    "supplier_tax_id": ("supplier_tax_id_norm", normalize_tax_id),
    "invoice_number": ("invoice_number_norm", normalize_invoice_number),
}


# =====================================================================================
# Reading
# =====================================================================================
def actions(item: db.Row) -> list[Action]:
    """What a person may do now. An item waiting for QuickBooks allows nothing; after a failed
    QuickBooks call (effect_error, not waiting), only a retry."""
    if item["status"] != "open" or item.get("waiting_for"):
        return []
    codes = {r["code"] for r in item["reasons"]}
    if item["stage"] == "validation":
        if codes & FRAUD_CODES:
            return ["verify", "reject"]  # never a plain approve
        return ["approve", "correct", "reject"]
    if item.get("effect_error") or item["stage"] == "posting":
        return ["retry"]
    if "ambiguous_match" in codes:
        return ["confirm_match", "reject_match"]
    if "payment_difference" in codes:
        return ["write_off", "keep_open"]
    return []


def _item(conn: db.Conn, review_id: int, lock: bool = False, scope: Scope | None = None) -> db.Row:
    row = conn.execute(
        "SELECT * FROM review_queue WHERE id = %s AND (%s::text IS NULL OR scope = %s)"
        + (" FOR UPDATE" if lock else ""),
        (review_id, scope, scope),
    ).fetchone()
    if row is None:
        raise NotFound("unknown review item")
    return row


def item_invoice_id(conn: db.Conn, review_id: int, scope: Scope | None = None) -> Any:
    """The invoice behind a review item (NotFound if unknown or outside the scope)."""
    return _item(conn, review_id, scope=scope)["invoice_id"]


def _invoice(conn: db.Conn, invoice_id: Any, lock: bool = False) -> db.Row:
    return db.one(conn, "SELECT * FROM invoices WHERE id = %s" + (" FOR UPDATE" if lock else ""), (invoice_id,))


def _lines(conn: db.Conn, invoice_id: Any) -> tuple[list[db.Row], list[db.Row]]:
    lines = conn.execute(
        "SELECT line_no, kind, tax_stage, description, quantity, unit_price, amount, tax_rate "
        "FROM invoice_lines WHERE invoice_id = %s ORDER BY line_no",
        (invoice_id,),
    ).fetchall()
    taxes = conn.execute(
        "SELECT tax_type, label, rate, taxable_base, amount FROM invoice_tax_lines WHERE invoice_id = %s ORDER BY id",
        (invoice_id,),
    ).fetchall()
    return lines, taxes


def stored_invoice(conn: db.Conn, inv: db.Row) -> CommonInvoice:
    """The invoice as stored now (with any corrections), in the common format the checks use."""
    lines, taxes = _lines(conn, inv["id"])
    return CommonInvoice(
        is_invoice=True,
        document_type=inv["document_type"],
        supplier=Party(name=inv["supplier_name"], tax_id=inv["supplier_tax_id"], country=inv["supplier_country"]),
        buyer=Party(name=inv["buyer_name"], tax_id=inv["buyer_tax_id"]),
        invoice_number=inv["invoice_number"],
        related_invoice_number=inv["related_invoice_number"],
        invoice_date=inv["invoice_date"],
        due_date=inv["due_date"],
        service_period_start=inv["service_period_start"],
        service_period_end=inv["service_period_end"],
        dates=DateInfo(),
        currency=inv["currency"],
        lines=[InvoiceLine(**li) for li in lines],
        taxes=[TaxLine(**t) for t in taxes],
        subtotal=inv["subtotal"],
        tax_total=inv["tax_total"],
        total=inv["total"],
        ocr_text="",
        normalized=Normalized(
            invoice_number=inv["invoice_number_norm"],
            supplier_name=inv["supplier_name_norm"],
            supplier_tax_id=inv["supplier_tax_id_norm"],
        ),
        extraction=ExtractionMeta(provider=inv["extraction_provider"] or "unknown", pages=0),
        issues=[],
    )


def _supplier_default(conn: db.Conn, supplier_key: str | None) -> db.Row | None:
    if not supplier_key:
        return None
    return conn.execute(
        """SELECT d.expense_account_id AS id, a.fully_qualified_name AS name
             FROM supplier_defaults d LEFT JOIN qbo_accounts a ON a.external_id = d.expense_account_id
            WHERE d.supplier_key = %s""",
        (supplier_key,),
    ).fetchone()


INVOICE_FIELDS = (
    "id", "status", "document_type", "supplier_name", "supplier_tax_id", "supplier_country", "invoice_number",
    "related_invoice_number", "invoice_date", "due_date", "service_period_start", "service_period_end",
    "invoice_date_raw", "due_date_raw", "dates_ambiguous", "date_order_suggested", "invoice_date_suggested",
    "due_date_suggested", "date_suggestion_basis", "currency", "subtotal", "tax_total", "total", "tax_mode",
    "confidence", "ocr_confidence_avg", "confidence_completeness", "language", "file_path", "received_at",
    "channel", "expense_account_id", "expense_account_source", "checks",
)  # fmt: skip


def detail(conn: db.Conn, review_id: int, scope: Scope | None = None) -> dict[str, Any]:
    """Everything the review page shows for one item. Values are JSON-safe."""
    item = _item(conn, review_id, scope=scope)
    inv = _invoice(conn, item["invoice_id"])
    lines, taxes = _lines(conn, inv["id"])
    blockers, _ = posting_blockers(stored_invoice(conn, inv))
    result = {
        **{k: item[k] for k in ("id", "stage", "status", "scope", "reasons", "corrections", "decision", "reviewer")},
        **{k: item[k] for k in ("waiting_for", "effect_error")},
        "created_at": item["created_at"],
        "resolved_at": item["resolved_at"],
        "actions": actions(item),
        "invoice": {k: inv[k] for k in INVOICE_FIELDS} | {"has_file": inv["file_path"] is not None},
        "lines": lines,
        "taxes": taxes,
        "supplier_default_account": _supplier_default(conn, inv["supplier_key"]),
        "blockers": [b.model_dump() for b in blockers],  # what approving would refuse now
        "payment": _payment(conn, inv["id"]),
    }
    result["invoice"].pop("file_path")
    out: dict[str, Any] = db.jsonable(result)
    return out


def _payment(conn: db.Conn, invoice_id: Any) -> dict[str, Any] | None:
    """The bill in QuickBooks and its bank side: candidate and accepted matches, any write-off."""
    posting = conn.execute(
        """SELECT status, external_id, amount, currency, payment_status, payment_reason, amount_paid,
                  amount_written_off, last_error
             FROM accounting_postings WHERE invoice_id = %s""",
        (invoice_id,),
    ).fetchone()
    if posting is None:
        return None
    matches = conn.execute(
        """SELECT m.id, m.status, m.bank_transaction_id, m.invoice_amount, m.bank_amount, m.difference,
                  m.currency, m.days_from_invoice, m.name_similarity, m.invoice_number_found,
                  m.external_payment_id, m.payment_last_error, t.date, t.name AS bank_text, t.merchant_name
             FROM matches m JOIN bank_transactions t ON t.transaction_id = m.bank_transaction_id
            WHERE m.invoice_id = %s AND m.status IN ('candidate', 'accepted')
            ORDER BY m.status, t.date, m.id""",
        (invoice_id,),
    ).fetchall()
    write_off = conn.execute(
        "SELECT id, amount, currency, status, last_error FROM write_offs WHERE invoice_id = %s", (invoice_id,)
    ).fetchone()
    under_paid = posting["payment_status"] == "paid_with_difference" and posting["amount_paid"] < posting["amount"]
    open_amount = posting["amount"] - posting["amount_paid"] if under_paid else None
    return {"posting": posting, "matches": matches, "write_off": write_off, "open_amount": open_amount}


def list_items(conn: db.Conn, scope: Scope, status: str = "open", limit: int = 100) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT r.id, r.stage, r.status, r.reasons, r.created_at, r.resolved_at, r.reviewer, r.waiting_for,
                  r.effect_error,
                  i.id AS invoice_id, i.supplier_name, i.invoice_number, i.invoice_date, i.currency, i.total,
                  i.document_type
             FROM review_queue r JOIN invoices i ON i.id = r.invoice_id
            WHERE r.scope = %s AND (%s = 'all' OR r.status = %s)
            ORDER BY r.created_at DESC LIMIT %s""",
        (scope, status, status, limit),
    ).fetchall()
    out: list[dict[str, Any]] = db.jsonable([{**r, "actions": actions(r)} for r in rows])
    return out


# =====================================================================================
# Decisions
# =====================================================================================
def _open_item(conn: db.Conn, review_id: int, action: Action, scope: Scope | None) -> db.Row:
    item = _item(conn, review_id, lock=True, scope=scope)
    if action not in actions(item):
        state = item["status"] if item["status"] != "open" else f"{item['stage']} review"
        raise Conflict(f"'{action}' is not possible on this item ({state}); allowed: {actions(item) or 'none'}")
    return item


def _log(conn: db.Conn, item: db.Row, message: str, details: dict[str, Any]) -> None:
    db.log(
        conn,
        step="review",
        level="info",
        message=message,
        invoice_id=str(item["invoice_id"]),
        details={"review_id": item["id"], **details},
    )


def _apply_changes(conn: db.Conn, item: db.Row, inv: db.Row, values: dict[str, Any], reviewer: str) -> None:
    """Updates the invoice and records before/after on the review item (audit trail)."""
    columns = dict(values)
    for name, (column, normalize) in DERIVED.items():
        if name in values:
            columns[column] = normalize(values[name])
    sets = ", ".join(f"{c} = %({c})s" for c in columns)
    conn.execute(f"UPDATE invoices SET {sets} WHERE id = %(invoice_id)s", {**columns, "invoice_id": inv["id"]})
    entry = {
        "by": reviewer,
        "at": datetime.now(UTC),
        "before": {k: inv[k] for k in values},
        "after": values,
    }
    conn.execute(
        "UPDATE review_queue SET corrections = corrections || %s WHERE id = %s",
        (Jsonb([db.jsonable(entry)]), item["id"]),
    )


def _add_duplicate_reasons(conn: db.Conn, item: db.Row, inv: db.Row) -> list[str]:
    """After a correction (number, supplier, total, date), the duplicate check runs again; new
    matches are added to the item's reasons so the reviewer sees them before approving."""
    found = db.find_possible_duplicates(conn, stored_invoice(conn, inv), exclude_id=str(inv["id"]))
    known = {r.get("message") for r in item["reasons"]}
    new = [
        Issue(
            code="possible_duplicate",
            field="invoice_number" if d.rule == "same_number" else "total",
            message=DUPLICATE_RULES[d.rule].format(id=d.invoice_id, status=d.status, days=DUPLICATE_DATE_WINDOW_DAYS),
        )
        for d in found
    ]
    new = [n for n in new if n.message not in known]
    if new:
        conn.execute(
            "UPDATE review_queue SET reasons = reasons || %s WHERE id = %s",
            (Jsonb([n.model_dump() for n in new]), item["id"]),
        )
    return [n.code for n in new]


def correct(conn: db.Conn, review_id: int, c: Correction, scope: Scope | None = None) -> dict[str, Any]:
    item = _open_item(conn, review_id, "correct", scope)
    values = c.changes.model_dump(exclude_unset=True)
    if not values:
        raise Conflict("nothing to correct")
    cleared = [k for k in CANNOT_CLEAR if k in values and values[k] is None]
    if cleared:
        raise Conflict(f"these fields can be corrected but not emptied: {cleared}")
    inv = _invoice(conn, item["invoice_id"], lock=True)
    _apply_changes(conn, item, inv, values, c.reviewer)
    added = _add_duplicate_reasons(conn, item, _invoice(conn, inv["id"]))
    _log(conn, item, "review corrected", {"fields": sorted(values), "new_reasons": added})
    return detail(conn, review_id, scope)


def _accept_suggested_dates(conn: db.Conn, item: db.Row, inv: db.Row, reviewer: str) -> db.Row:
    if not inv["dates_ambiguous"] or inv["invoice_date_suggested"] is None:
        raise Conflict("there is no suggested reading of the dates to accept")
    values = {"invoice_date": inv["invoice_date_suggested"]}
    if inv["due_date"] is None and inv["due_date_suggested"] is not None:
        values["due_date"] = inv["due_date_suggested"]
    for name in ("service_period_start", "service_period_end"):
        if inv[name] is None and inv[f"{name}_suggested"] is not None:
            values[name] = inv[f"{name}_suggested"]
    _apply_changes(conn, item, inv, values, reviewer)
    return _invoice(conn, inv["id"], lock=True)


def _choose_account(conn: db.Conn, inv: db.Row, account_id: str | None) -> tuple[db.Row, str]:
    """(account, source): the reviewer's choice, else the supplier's default. Never guessed."""
    source = "reviewer"
    if account_id is None:
        default = _supplier_default(conn, inv["supplier_key"])
        if default is None:
            raise Conflict("choose an account: this supplier has no default account")
        account_id, source = default["id"], "supplier_default"
    account = qbo_reference.coding_account(conn, account_id)
    if account is None:
        raise Conflict(f"account {account_id} can't be used (unknown, inactive or not a cost/asset account)")
    return account, source


def _save_default(conn: db.Conn, inv: db.Row, account_id: str, d: Decision) -> str | None:
    """Saved on the supplier's first confirmed invoice (unless set_default is False); replaced only
    when the reviewer asks (set_default True), so a one-off choice (e.g. capex) doesn't spread."""
    current = _supplier_default(conn, inv["supplier_key"])
    if current is None and d.set_default is not False:
        conn.execute(
            """INSERT INTO supplier_defaults (supplier_key, expense_account_id, source, set_by, set_from_invoice)
               VALUES (%s, %s, 'reviewer', %s, %s)""",
            (inv["supplier_key"], account_id, d.reviewer, inv["id"]),
        )
        return "created"
    if current is not None and d.set_default and current["id"] != account_id:
        conn.execute(
            """UPDATE supplier_defaults SET expense_account_id = %s, source = 'reviewer', set_by = %s,
                      set_from_invoice = %s WHERE supplier_key = %s""",
            (account_id, d.reviewer, inv["id"], inv["supplier_key"]),
        )
        return "updated"
    return None


def approve(
    conn: db.Conn,
    review_id: int,
    d: Decision,
    action: Literal["approve", "verify"] = "approve",
    scope: Scope | None = None,
) -> db.Row:
    """The invoice becomes ready (the posting workflow then posts it to the chosen account)."""
    item = _open_item(conn, review_id, action, scope)
    note = (d.note or "").strip()
    if action == "verify" and len(note) < MIN_VERIFY_NOTE:
        raise Conflict("say how the change was verified with the supplier (e.g. phone call to a known number)")
    inv = _invoice(conn, item["invoice_id"], lock=True)
    if d.accept_suggested_dates:
        inv = _accept_suggested_dates(conn, item, inv, d.reviewer)
    blockers, tax_mode = posting_blockers(stored_invoice(conn, inv))
    if blockers:
        raise Blocked(blockers)
    if d.account_id is None and account_choice_required(item):
        raise AccountRequired("choose the account: this item may be a fixed asset (expense or fixed asset account)")
    account, source = _choose_account(conn, inv, d.account_id)
    conn.execute(
        """UPDATE invoices SET status = 'ready', expense_account_id = %s, expense_account_source = %s, tax_mode = %s
            WHERE id = %s""",
        (account["id"], source, tax_mode, inv["id"]),
    )
    default = _save_default(conn, inv, account["id"], d) if source == "reviewer" else None
    corrected = bool(_item(conn, review_id)["corrections"])
    status = "verified" if action == "verify" else ("corrected" if corrected else "approved")
    decision = {
        "action": action,
        "account_id": account["id"],
        "account_name": account["name"],
        "account_source": source,
        "supplier_default": default,
        "suggested_dates_accepted": d.accept_suggested_dates,
    }
    conn.execute(
        """UPDATE review_queue SET status = %s, reviewer = %s, decision_note = %s, decision = %s, resolved_at = now()
            WHERE id = %s""",
        (status, d.reviewer, note or None, Jsonb(decision), review_id),
    )
    _log(conn, item, f"review {status}", {k: v for k, v in decision.items() if k != "account_name"})
    return _item(conn, review_id)


def reject(conn: db.Conn, review_id: int, r: Rejection, scope: Scope | None = None) -> tuple[db.Row, str | None]:
    """The invoice is rejected and won't be posted. Returns the stored file to delete after COMMIT."""
    item = _open_item(conn, review_id, "reject", scope)
    inv = _invoice(conn, item["invoice_id"], lock=True)
    conn.execute("UPDATE invoices SET status = 'rejected' WHERE id = %s", (inv["id"],))
    if inv["file_path"]:
        conn.execute(
            """UPDATE invoices SET file_path = NULL, file_deleted_at = now(), file_deletion_reason = 'rejected'
                WHERE id = %s""",
            (inv["id"],),
        )
    conn.execute(
        """UPDATE review_queue SET status = 'rejected', reviewer = %s, decision_note = %s, decision = %s,
                  resolved_at = now() WHERE id = %s""",
        (r.reviewer, r.note.strip(), Jsonb({"action": "reject"}), review_id),
    )
    _log(conn, item, "review rejected", {})
    return _item(conn, review_id), inv["file_path"]
