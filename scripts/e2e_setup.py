"""One-time setup of the daily end-to-end test: its fictional supplier as a QuickBooks vendor, linked in the
core with a default expense account, so the test's clean invoice goes straight through.

  set -a; . ./.env; set +a
  python3 scripts/e2e_setup.py                          # dry run: what exists, what would be done
  python3 scripts/e2e_setup.py --apply                  # create the vendor if missing, then link it
  python3 scripts/e2e_setup.py --apply --account "Office Supplies"

Safe to run twice: an existing vendor (same display name) is reused, and the vendor create carries a fixed
request ID. QuickBooks calls go through n8n's credential (qbo_via_n8n.py: reads and creates only). The test
never deletes this vendor; it deletes only the bills its postings create.
"""

import argparse
import subprocess
import sys
from pathlib import Path

from qbo_via_n8n import Request, Response, send

ROOT = Path(__file__).resolve().parents[1]
SUPPLIER = "Ostlewick Test Supplies LLC"  # = e2e_test.SUPPLIER_NAME in the core
REQUEST_ID = "ap-e2e-vendor-ostlewick"


def _quote(text: str) -> str:
    return text.replace("'", "\\'")


def find(entity: str, where: str) -> list[dict]:
    r = send([Request("GET", "/query", {"query": f"select * from {entity} where {where}"})])[0]
    if r.status != 200:
        raise RuntimeError(f"QuickBooks query failed ({r.status}): {str(r.body)[:300]}")
    return (r.body.get("QueryResponse") or {}).get(entity) or []


def create_vendor() -> Response:
    body = {"DisplayName": SUPPLIER, "CompanyName": SUPPLIER, "CurrencyRef": {"value": "USD"}}
    return send([Request("POST", "/vendor", {"requestid": REQUEST_ID}, body)])[0]


def link_in_core(vendor_id: str, account_id: str) -> None:
    cmd = ["docker", "compose", "exec", "-T", "core", "python", "-m", "app.e2e_test", "setup",
           "--vendor-id", vendor_id, "--account-id", account_id]  # fmt: skip
    subprocess.run(cmd, cwd=ROOT, check=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--account", default="Office Supplies", help="the expense account the test invoice uses")
    args = parser.parse_args()

    accounts = find("Account", f"Name = '{_quote(args.account)}' and Active = true")
    if len(accounts) != 1:
        print(f"expected one active account named {args.account!r}, found {len(accounts)}")
        return 1
    account_id = accounts[0]["Id"]
    vendors = find("Vendor", f"DisplayName = '{_quote(SUPPLIER)}'")
    print(f"account {args.account!r}: {account_id}")
    print(f"vendor {SUPPLIER!r}: {vendors[0]['Id'] if vendors else 'missing, would be created'}")
    if not args.apply:
        print("dry run: nothing changed (add --apply)")
        return 0
    if vendors:
        vendor_id = vendors[0]["Id"]
    else:
        r = create_vendor()
        if r.status != 200:
            print(f"vendor not created ({r.status}): {str(r.body)[:300]}")
            return 1
        vendor_id = r.body["Vendor"]["Id"]
        print(f"vendor created: {vendor_id}")
    link_in_core(vendor_id, account_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
