"""Copy of the QuickBooks chart of accounts and vendor list, kept by the sync workflow.

The workflow passes QuickBooks' query responses through unchanged; this module decides what
they mean. QuickBooks tokens stay in n8n: the core and the review page only read this copy.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from . import db
from .normalize import normalize_supplier_name

Entity = Literal["Account", "Vendor"]
PAGE_SIZE = 1000  # the workflow asks for this many; a full page means there may be more

# Accounts an invoice line can be coded to: costs, fixed assets (capitalised purchases)
# and other current assets (e.g. prepaid expenses).
CODING_ACCOUNT_TYPES = ("Expense", "Other Expense", "Cost of Goods Sold", "Fixed Asset", "Other Current Asset")
# QuickBooks system accounts of those types that a bill must never be coded to
SYSTEM_SUB_TYPES = ("UnappliedCashBillPaymentExpense",)
_CODING = "active AND account_type = ANY(%(types)s) AND NOT (coalesce(account_sub_type, '') = ANY(%(system)s))"
_CODING_PARAMS = {"types": list(CODING_ACCOUNT_TYPES), "system": list(SYSTEM_SUB_TYPES)}

_CURRENCY_SUFFIX = re.compile(r"\s*\([A-Z]{3}\)\s*$")  # vendors created per currency: "Name (EUR)"


class SyncError(Exception):
    """The response can't be used: the stored copy is left unchanged."""


def vendor_name_norm(display_name: str) -> str | None:
    """Same normalization as invoices.supplier_name_norm, after removing our currency suffix."""
    return normalize_supplier_name(_CURRENCY_SUFFIX.sub("", display_name))


def _rows(status: int, body: Any, entity: Entity) -> list[dict[str, Any]]:
    response = body.get("QueryResponse") if status == 200 and isinstance(body, dict) else None
    if not isinstance(response, dict):
        raise SyncError(f"no {entity} list in the response (HTTP {status})")
    rows = response.get(entity, [])  # an empty list is absent from the response
    if not isinstance(rows, list) or len(rows) >= PAGE_SIZE:
        raise SyncError(f"{entity} list incomplete or larger than one page ({PAGE_SIZE}); nothing stored")
    if entity == "Account" and not rows:  # every company has accounts: never deactivate them all
        raise SyncError("empty account list; nothing stored")
    return [r for r in rows if isinstance(r, dict) and r.get("Id")]


def _account(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "external_id": str(r["Id"]),
        "name": str(r.get("Name") or ""),
        "fully_qualified_name": str(r.get("FullyQualifiedName") or r.get("Name") or ""),
        "account_type": str(r.get("AccountType") or ""),
        "account_sub_type": r.get("AccountSubType"),
        "classification": r.get("Classification"),
        "currency": (r.get("CurrencyRef") or {}).get("value"),
        "active": bool(r.get("Active", True)),
    }


def _vendor(r: dict[str, Any]) -> dict[str, Any]:
    name = str(r.get("DisplayName") or "")
    return {
        "external_id": str(r["Id"]),
        "display_name": name,
        "name_norm": vendor_name_norm(name),
        "currency": (r.get("CurrencyRef") or {}).get("value"),
        "active": bool(r.get("Active", True)),
    }


_TABLES: dict[Entity, tuple[str, Any]] = {"Account": ("qbo_accounts", _account), "Vendor": ("qbo_vendors", _vendor)}


def store(conn: db.Conn, entity: Entity, status: int, body: Any) -> dict[str, int]:
    """Replaces the stored copy with this response. Rows missing from it are kept as inactive
    (posted bills may refer to them). An unusable response raises SyncError and changes nothing."""
    table, convert = _TABLES[entity]
    rows = [convert(r) for r in _rows(status, body, entity)]
    cols = list(rows[0]) if rows else []
    for row in rows:
        updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols if c != "external_id")
        conn.execute(
            f"INSERT INTO {table} ({', '.join(cols)}, synced_at) "
            f"VALUES ({', '.join('%(' + c + ')s' for c in cols)}, now()) "
            f"ON CONFLICT (external_id) DO UPDATE SET {updates}, synced_at = now()",
            row,
        )
    gone = conn.execute(
        f"UPDATE {table} SET active = false, synced_at = now() WHERE active AND NOT (external_id = ANY(%s))",
        ([r["external_id"] for r in rows],),
    ).rowcount
    db.log(conn, step="sync", level="info", message=f"{entity} list synced", details={"rows": len(rows), "gone": gone})
    return {"rows": len(rows), "deactivated": gone}


def coding_accounts(conn: db.Conn) -> list[db.Row]:
    """The account picker: active accounts of the types an invoice can be coded to."""
    return conn.execute(
        "SELECT external_id AS id, fully_qualified_name AS name, account_type AS type "
        f"FROM qbo_accounts WHERE {_CODING} ORDER BY account_type, fully_qualified_name",
        _CODING_PARAMS,
    ).fetchall()


def coding_account(conn: db.Conn, account_id: str) -> db.Row | None:
    """The account if an invoice can be coded to it now (active, right type), else None."""
    return conn.execute(
        "SELECT external_id AS id, fully_qualified_name AS name, account_type AS type "
        f"FROM qbo_accounts WHERE external_id = %(id)s AND {_CODING}",
        {**_CODING_PARAMS, "id": account_id},
    ).fetchone()
