"""The test documents' expected results (test-data/invoices/*.expected.json) and how a result is
compared with them. Shared by run_e2e.py (results stored by the live pipeline) and calibrate.py
(results of the core's extraction and checks, nothing stored)."""

import hashlib
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

TEXT_KEYS = ("document_type", "invoice_number", "currency", "supplier_country", "related_invoice_number")
DATE_KEYS = ("invoice_date", "due_date", "service_period_start", "service_period_end")


def load(test_dir: Path) -> list[dict[str, Any]]:
    """Every expected result, with the SHA-256 of its file (how a stored result is found)."""
    items = []
    for f in sorted(test_dir.glob("*.expected.json")):
        e = json.loads(f.read_text())
        e["sha256"] = hashlib.sha256((test_dir / e["file"]).read_bytes()).hexdigest()
        items.append(e)
    return items


def expected_values(exp: dict[str, Any], ignore_reasons: frozenset[str] = frozenset()) -> dict[str, Any]:
    """The values an expected result names, in the form actual values are given.
    ignore_reasons: reason codes the caller can't produce (e.g. checks that need stored history)."""
    out = {k: v for k, v in exp.items() if k in (*TEXT_KEYS, *DATE_KEYS, "status")}
    out["reasons"] = sorted(r for r in exp["reasons"] if r not in ignore_reasons)
    if exp["reasons"] and not out["reasons"] and exp["status"] == "needs_review":
        out["status"] = "ready"  # in review only for reasons the caller can't produce
    if "info" in exp:
        out["info"] = sorted(exp["info"])
    if "total" in exp:
        out["total"] = Decimal(exp["total"]) if exp["total"] is not None else None
    if "suggested_dates" in exp:
        out["suggested order"] = exp["suggested_dates"]["order"]
        out["suggested invoice date"] = exp["suggested_dates"]["invoice_date"]
    return out


def compare(
    exp: dict[str, Any], actual: dict[str, Any], line_count: int, ignore_reasons: frozenset[str] = frozenset()
) -> list[str]:
    """One line per difference between an expected result and the actual values (empty: it matches)."""
    diffs = [
        f"{name}: expected {expected!r}, got {actual[name]!r}"
        for name, expected in expected_values(exp, ignore_reasons).items()
        if actual[name] != expected
    ]
    if "min_lines" in exp and line_count < exp["min_lines"]:
        diffs.append(f"lines: expected at least {exp['min_lines']}, got {line_count}")
    return diffs
