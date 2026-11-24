"""Confidence calibration: runs each test document through the core's own extraction and checks,
compares the result with test-data/invoices/*.expected.json, and stores nothing.

Run with scripts/calibrate.sh (one Mistral OCR call per document).
  --extra FILE   one more document with no expected result: its score is recorded, nothing compared
  --only ID      only these documents (repeatable)
  --json FILE    write the results to this file

What differs from the live pipeline: nothing is looked up in the database, so the checks that need
stored history can't fire (new supplier, expense account, possible duplicate, a bank account
unlike the supplier's earlier ones). Those reasons are left out of the comparison. Everything else
is the same code: files.prepare → provider → build_common_invoice → bank_details.assess → validate.
"""

import argparse
import json
import os
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

from app import bank_details, files
from app.config import get_settings
from app.extraction import build_common_invoice
from app.models import CommonInvoice
from app.providers import MistralOcrProvider
from app.validation import Policy, Validation, validate
from expectations import DATE_KEYS, TEXT_KEYS, compare, load

TEST_DIR = Path(os.environ.get("TEST_DATA_DIR", "/test-data/invoices"))
NEEDS_HISTORY = frozenset({"new_vendor", "expense_account_missing", "possible_duplicate"})
FINGERPRINT_KEY = b"calibration run: fingerprints are compared with nothing"


def actual_values(inv: CommonInvoice, v: Validation) -> dict[str, Any]:
    """The same names as the expected results (see expectations.expected_values)."""
    out: dict[str, Any] = {key: getattr(inv, key) for key in TEXT_KEYS if key != "supplier_country"}
    out["supplier_country"] = inv.supplier.country
    out.update({key: (d.isoformat() if (d := getattr(inv, key)) else None) for key in DATE_KEYS})
    out["status"] = v.status
    out["reasons"] = sorted(r.code for r in v.reasons)
    out["info"] = sorted(i.code for i in v.info)
    out["total"] = inv.total
    out["suggested order"] = inv.dates.suggested_order
    suggested = inv.dates.suggested_invoice_date
    out["suggested invoice date"] = suggested.isoformat() if suggested else None
    return out


def run_one(
    path: Path, email_text: str | None, provider: MistralOcrProvider, threshold: float, policy: Policy
) -> tuple[CommonInvoice, Validation]:
    prepared = files.prepare(path.read_bytes())
    inv = build_common_invoice(provider.extract(prepared.content, prepared.mime_type), provider.name)
    bank = bank_details.assess(inv, email_text, [], FINGERPRINT_KEY)
    return inv, validate(inv, confidence_threshold=threshold, policy=policy, supplier=None, bank=bank)


def summary(inv: CommonInvoice, v: Validation) -> dict[str, Any]:
    """What the calibration records for each document, beyond the comparison."""
    return {
        "status": v.status,
        "reasons": [r.code for r in v.reasons],
        "info": [i.code for i in v.info],
        "confidence": v.confidence,
        "ocr_avg": v.confidence_ocr,
        "ocr_min": inv.extraction.ocr_confidence_min,
        "completeness": v.confidence_completeness,
        "pages": inv.extraction.pages,
        "supplier": inv.supplier.name,
        "supplier_tax_id": inv.supplier.tax_id,
        "total": str(inv.total) if inv.total is not None else None,
        "currency": inv.currency,
        "lines": len(inv.lines),
        "taxes": [{"type": t.tax_type, "rate": str(t.rate), "amount": str(t.amount)} for t in inv.taxes],
        "issues": [i.code for i in inv.issues],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--extra", action="append", default=[], type=Path)
    ap.add_argument("--only", action="append", default=[])
    ap.add_argument("--json", type=Path)
    args = ap.parse_args()
    s = get_settings()
    provider = MistralOcrProvider(s.mistral_api_key.get_secret_value(), model=s.mistral_ocr_model)
    policy = Policy(s.home_currency, Decimal(s.cutoff_materiality), Decimal(s.capex_threshold))

    # One run per distinct document: G is A's file sent again, H has no document to read
    docs: list[dict[str, Any]] = [e for e in load(TEST_DIR) if not set(e["expect"]) & {"occurrence", "no_invoice_row"}]
    docs = [e for e in docs if not args.only or e["id"] in args.only]
    docs += [{"id": p.name, "file": str(p), "expect": None} for p in args.extra]
    results = []
    for e in docs:
        email = e.get("email") or {}
        email_text = f"{email['subject']}\n{email['body']}" if email else None
        inv, v = run_one(TEST_DIR / e["file"], email_text, provider, s.confidence_threshold, policy)
        exp = e["expect"]
        diffs = compare(exp, actual_values(inv, v), len(inv.lines), NEEDS_HISTORY) if exp else None
        results.append({"id": e["id"], "correct": diffs == [] if exp else None, "diffs": diffs} | summary(inv, v))
        verdict = {True: "PASS", False: "FAIL", None: "----"}[results[-1]["correct"]]
        print(
            f"{verdict}  {e['id']:<34} conf={v.confidence} ocr={v.confidence_ocr} fields={v.confidence_completeness} "
            f"status={v.status} reasons={[r.code for r in v.reasons]}",
            flush=True,
        )
        for d in diffs or []:
            print(f"      - {d}")

    checked = [r for r in results if r["correct"] is not None]
    print(
        f"\n{sum(r['correct'] for r in checked)}/{len(checked)} documents match their expected result "
        f"(reasons that need stored history left out: {', '.join(sorted(NEEDS_HISTORY))})"
    )
    if args.json:
        args.json.write_text(
            json.dumps(
                {"model": s.mistral_ocr_model, "threshold": s.confidence_threshold, "results": results},
                indent=2,
                ensure_ascii=False,
            )
            + "\n"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
