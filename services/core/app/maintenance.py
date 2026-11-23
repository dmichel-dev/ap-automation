"""One-off data maintenance after a normalization rule changes. Dry run by default.

  docker compose exec core python -m app.maintenance renormalize-tax-ids           # show what would change
  docker compose exec core python -m app.maintenance renormalize-tax-ids --apply   # change it (one transaction)

The rule itself is never repeated here: the stored normalized copy is recomputed with the same
function the pipeline uses (normalize.normalize_tax_id). The value as printed is never touched.
"""

import argparse
import json
import sys
from typing import Any

from . import db
from .config import get_settings
from .db import Conn
from .normalize import normalize_tax_id

Row = dict[str, Any]


def tax_id_changes(conn: Conn) -> list[Row]:
    """Invoices whose stored normalized tax ID differs from what the current rule gives."""
    rows = conn.execute(
        "SELECT id, supplier_tax_id, supplier_tax_id_norm, supplier_key FROM invoices "
        "WHERE supplier_tax_id IS NOT NULL ORDER BY created_at FOR UPDATE"
    ).fetchall()
    return [
        {**r, "id": str(r["id"]), "new_norm": normalize_tax_id(r["supplier_tax_id"])}
        for r in rows
        if normalize_tax_id(r["supplier_tax_id"]) != r["supplier_tax_id_norm"]
    ]


def _move_vendor_links(conn: Conn, old_key: str, new_key: str) -> int:
    """Vendor links follow the supplier key, unless a link for the new key already exists."""
    cur = conn.execute(
        """UPDATE vendor_links v SET supplier_key = %s
            WHERE v.supplier_key = %s
              AND NOT EXISTS (SELECT 1 FROM vendor_links w WHERE w.system = v.system
                              AND w.supplier_key = %s AND w.currency = v.currency)""",
        (new_key, old_key, new_key),
    )
    return cur.rowcount


def renormalize_tax_ids(conn: Conn, apply: bool) -> Row:
    changes = tax_id_changes(conn)
    moved = 0
    if apply:
        keys: dict[str, str] = {}
        for c in changes:
            new_key = db.one(
                conn,
                "UPDATE invoices SET supplier_tax_id_norm = %s WHERE id = %s RETURNING supplier_key",
                (c["new_norm"], c["id"]),
            )["supplier_key"]
            if c["supplier_key"] and new_key and c["supplier_key"] != new_key:
                keys[c["supplier_key"]] = new_key
        moved = sum(_move_vendor_links(conn, old, new) for old, new in keys.items())
        db.log(
            conn,
            step="maintenance",
            level="info",
            message="normalized tax IDs recomputed",
            channel="system",
            details={"invoices": len(changes), "vendor_links_moved": moved},
        )
    return {
        "applied": apply,
        "invoices": [{k: c[k] for k in ("id", "supplier_tax_id_norm", "new_norm")} for c in changes],
        "vendor_links_moved": moved,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("renormalize-tax-ids").add_argument("--apply", action="store_true")
    args = ap.parse_args(argv)
    with db.connect(get_settings().ap_database_url.get_secret_value()) as conn:
        result = renormalize_tax_ids(conn, args.apply)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
