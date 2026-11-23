"""Adds the missing accounts of config/chart_of_accounts_us.json to QuickBooks. Never changes or
deletes an account; running it again does nothing once every account exists.

  set -a; . ./.env; set +a
  python3 scripts/create_chart_of_accounts.py            # dry run: what exists, what would be created
  python3 scripts/create_chart_of_accounts.py --apply    # create the missing ones

An account counts as present when an active QuickBooks account has its full name (or a name listed in
same_as), compared without case, punctuation or "&"/"and" differences. A name already used by an
account of another type is reported, never created twice. Each create carries a fixed request ID,
so a retried request can't create a second account. Afterwards, run the reference sync workflow so
the review page's picker shows the new accounts (docs/operations.md).
"""

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from qbo_via_n8n import Request, Response, send

DEFAULT_FILE = Path(__file__).resolve().parents[1] / "config" / "chart_of_accounts_us.json"
QUERY = "select * from Account maxresults 1000"


@dataclass(frozen=True)
class Step:
    full_name: str
    status: str  # exists | create | conflict | no_parent
    detail: str
    body: dict[str, Any] | None = None


def norm(name: str) -> str:
    """ "Repairs & Maintenance" and "repairs and maintenance" are the same name."""
    words = re.sub(r"[^0-9a-z]+", " ", name.casefold().replace("&", " and ")).split()
    return " ".join(words)


def _full_name(entry: dict[str, Any]) -> str:
    return f"{entry['parent']}:{entry['name']}" if entry.get("parent") else str(entry["name"])


def _body(entry: dict[str, Any], by_name: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    body: dict[str, Any] = {"Name": entry["name"], "AccountType": entry["type"], "AccountSubType": entry["sub_type"]}
    if entry.get("parent"):
        parent = by_name.get(norm(entry["parent"]))
        if parent is None:
            return None
        body |= {"SubAccount": True, "ParentRef": {"value": parent["Id"]}}
    return body


def plan(entries: list[dict[str, Any]], accounts: list[dict[str, Any]]) -> list[Step]:
    """What to do for each entry, given QuickBooks' active accounts. Pure: no call is made here."""
    by_name = {norm(a["FullyQualifiedName"]): a for a in accounts if a.get("Active", True)}
    steps = []
    for entry in entries:
        full = _full_name(entry)
        found = next((by_name[n] for n in map(norm, [full, *entry.get("same_as", [])]) if n in by_name), None)
        if found is not None and found["AccountType"] == entry["type"]:
            steps.append(
                Step(full, "exists", f"{found['FullyQualifiedName']} (#{found['Id']}, {found['AccountType']})")
            )
        elif found is not None:
            steps.append(Step(full, "conflict", f"name used by {found['FullyQualifiedName']} ({found['AccountType']})"))
        elif (body := _body(entry, by_name)) is None:
            steps.append(Step(full, "no_parent", f"parent {entry['parent']} not found"))
        else:
            steps.append(Step(full, "create", f"{entry['type']} / {entry['sub_type']}", body))
    return steps


def _accounts() -> list[dict[str, Any]]:
    response = send([Request("GET", "/query", {"query": QUERY})])[0]
    accounts = (response.body or {}).get("QueryResponse", {}).get("Account") if response.status == 200 else None
    if not accounts or len(accounts) >= 1000:
        raise SystemExit(f"could not read the chart of accounts (HTTP {response.status}, {len(accounts or [])} rows)")
    return list(accounts)


def _request_id(full_name: str) -> str:
    """Fixed per account: QuickBooks answers a repeated request ID with the first result."""
    return "coa-" + hashlib.sha256(norm(full_name).encode()).hexdigest()[:24]


def _outcome(r: Response) -> str:
    if r.status == 200 and isinstance(r.body, dict) and "Account" in r.body:
        return f"created #{r.body['Account']['Id']}"
    errors = (r.body or {}).get("Fault", {}).get("Error", []) if isinstance(r.body, dict) else []
    return f"FAILED HTTP {r.status}: " + "; ".join(
        f"{e.get('code')} {e.get('Detail') or e.get('Message')}" for e in errors
    )


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--apply", action="store_true", help="create the missing accounts (default: dry run)")
    p.add_argument("--file", type=Path, default=DEFAULT_FILE)
    args = p.parse_args()
    steps = plan(json.loads(args.file.read_text())["accounts"], _accounts())
    to_create = [s for s in steps if s.status == "create"]
    results: dict[str, str] = {}
    if args.apply and to_create:
        requests = [Request("POST", "/account", {"requestid": _request_id(s.full_name)}, s.body) for s in to_create]
        results = {s.full_name: _outcome(r) for s, r in zip(to_create, send(requests), strict=True)}
    for s in steps:
        result = results.get(s.full_name, "would create" if s.status == "create" else "")
        print(f"{s.status:<9} {s.full_name:<38} {s.detail:<60} {result}".rstrip())
    failed = [r for r in results.values() if r.startswith("FAILED")]
    summary = f"{len(steps)} accounts: {sum(s.status == 'exists' for s in steps)} exist, {len(to_create)} missing"
    done = f", {len(to_create) - len(failed)} created, {len(failed)} failed" if args.apply else " (dry run)"
    print("\n" + summary + done)
    return 1 if failed or any(s.status in ("conflict", "no_parent") for s in steps) else 0


if __name__ == "__main__":
    sys.exit(main())
