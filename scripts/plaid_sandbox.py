"""Plaid sandbox setup for the demo (host script, standard library only). Prints no secrets.

  python3 scripts/plaid_sandbox.py setup                 # bank connection for the bank-sync workflow
  python3 scripts/plaid_sandbox.py transactions FILE     # add custom test transactions, then refresh
  python3 scripts/plaid_sandbox.py remove                # remove the sandbox connection

setup:
  1. creates a sandbox Item (test bank login with custom transactions enabled) and waits until
     its transaction history is ready;
  2. keeps the access token in .env (PLAID_ACCESS_TOKEN, gitignored) and in an n8n credential
     ("Plaid – Sandbox", encrypted by n8n); it is never printed;
  3. registers the checking account and its currency in the core service (matching uses it).

Reads PLAID_CLIENT_ID / PLAID_SECRET / PLAID_ENV from .env. Sandbox only: refuses any other env.
"""

import argparse
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"
API = "https://sandbox.plaid.com"
ITEM_KEY = "plaid-sandbox"  # the bank connection's key in the core service
INSTITUTION = "ins_109508"  # sandbox test bank
TEST_USER = {"override_username": "user_transactions_dynamic", "override_password": "pass_good"}
CREDENTIAL = {"id": "apPlaidSandbox01", "name": "Plaid – Sandbox", "type": "httpCustomAuth"}


def read_env() -> dict[str, str]:
    env = {}
    for line in ENV_FILE.read_text().splitlines():
        if m := re.match(r"^([A-Z0-9_]+)=(.*)$", line.strip()):
            env[m.group(1)] = m.group(2).strip()
    return env


def set_env(key: str, value: str) -> None:
    """Adds or replaces KEY=value in .env (file stays private)."""
    lines = [ln for ln in ENV_FILE.read_text().splitlines() if not ln.startswith(f"{key}=")]
    ENV_FILE.write_text("\n".join([*lines, f"{key}={value}"]) + "\n")
    ENV_FILE.chmod(0o600)


def plaid(env: dict[str, str], path: str, **body: Any) -> dict[str, Any]:
    payload = {"client_id": env["PLAID_CLIENT_ID"], "secret": env["PLAID_SECRET"], **body}
    req = urllib.request.Request(
        API + path, json.dumps(payload).encode(), {"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            result: dict[str, Any] = json.load(r)
            return result
    except urllib.error.HTTPError as e:
        err = json.load(e)
        sys.exit(f"{path}: {err.get('error_code')} {err.get('error_message')}")


def wait_until_ready(env: dict[str, str], token: str) -> None:
    """The first sync returns nothing until the historical pull is done."""
    for _ in range(36):
        cursor, status = None, None
        while True:
            body: dict[str, Any] = {"access_token": token, "count": 500}
            if cursor:
                body["cursor"] = cursor
            r = plaid(env, "/transactions/sync", **body)
            cursor, status = r["next_cursor"], r.get("transactions_update_status")
            if not r["has_more"]:
                break
        if status == "HISTORICAL_UPDATE_COMPLETE":
            print("transaction history ready")
            return
        time.sleep(5)
    sys.exit("transaction history not ready after 3 minutes")


def compose(*args: str, stdin: str | None = None) -> str:
    r = subprocess.run(["docker", "compose", *args], cwd=ROOT, input=stdin, capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"docker compose {args[0]} failed: {r.stderr.strip()[-500:]}")
    return r.stdout


def register_account(account_id: str, currency: str) -> None:
    """PUT /bank/accounts/{item_key} on the core service (from inside its container: not published)."""
    code = (
        "import json, os, urllib.request as u; "
        f"r = u.Request('http://localhost:8000/bank/accounts/{ITEM_KEY}', "
        f"json.dumps({{'account_id': '{account_id}', 'account_currency': '{currency}'}}).encode(), "
        "{'Content-Type': 'application/json', 'Authorization': 'Bearer ' + os.environ['CORE_API_TOKEN']}, "
        "method='PUT'); "
        "print(u.urlopen(r).status)"
    )
    print("core: account registered, HTTP", compose("exec", "-T", "core", "python", "-c", code).strip())


def save_credential(env: dict[str, str], token: str) -> None:
    """n8n 'Custom Auth' credential: Plaid keys as headers, access token merged into the JSON body."""
    auth = {
        "headers": {"PLAID-CLIENT-ID": env["PLAID_CLIENT_ID"], "PLAID-SECRET": env["PLAID_SECRET"]},
        "body": {"access_token": token},
    }
    data = [{**CREDENTIAL, "data": {"json": json.dumps(auth)}}]
    compose(
        "exec",
        "-T",
        "n8n",
        "sh",
        "-c",
        "umask 077 && cat > /tmp/cred.json && n8n import:credentials --input=/tmp/cred.json >/dev/null 2>&1; "
        "status=$?; rm -f /tmp/cred.json; exit $status",
        stdin=json.dumps(data),
    )
    print(f"n8n credential '{CREDENTIAL['name']}' saved (encrypted by n8n)")


def setup(env: dict[str, str], replace: bool) -> None:
    if env.get("PLAID_ACCESS_TOKEN") and not replace:
        sys.exit("a sandbox connection already exists (PLAID_ACCESS_TOKEN in .env); use --replace")
    public = plaid(
        env,
        "/sandbox/public_token/create",
        institution_id=INSTITUTION,
        initial_products=["transactions"],
        options=TEST_USER,
    )["public_token"]
    token = plaid(env, "/item/public_token/exchange", public_token=public)["access_token"]
    if env.get("PLAID_ACCESS_TOKEN"):
        plaid(env, "/item/remove", access_token=env["PLAID_ACCESS_TOKEN"])
        print("previous sandbox connection removed")
    set_env("PLAID_ACCESS_TOKEN", token)
    wait_until_ready(env, token)
    accounts = plaid(env, "/accounts/get", access_token=token)["accounts"]
    checking = next(a for a in accounts if a["subtype"] == "checking")  # custom transactions land here
    currency = checking["balances"]["iso_currency_code"]
    set_env("PLAID_ACCOUNT_ID", checking["account_id"])
    print(f"checking account selected ({currency}); {len(accounts)} accounts in the connection")
    register_account(checking["account_id"], currency)
    save_credential(env, token)


def add_transactions(env: dict[str, str], file: Path) -> None:
    """Custom transactions (max 10 per call; dates today or earlier). Positive amount = money out."""
    spec = json.loads(file.read_text())
    txs = [
        {
            "amount": t["amount"],
            "date_transacted": t["date"],
            "date_posted": t["date"],
            "description": t["description"],
            "iso_currency_code": t.get("currency", "USD"),
        }
        for t in spec["transactions"]
    ]
    for i in range(0, len(txs), 10):
        plaid(env, "/sandbox/transactions/create", access_token=env["PLAID_ACCESS_TOKEN"], transactions=txs[i : i + 10])
    plaid(env, "/transactions/refresh", access_token=env["PLAID_ACCESS_TOKEN"])
    print(f"{len(txs)} custom transactions created; refresh requested")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)
    s = sub.add_parser("setup")
    s.add_argument("--replace", action="store_true", help="replace an existing sandbox connection")
    t = sub.add_parser("transactions")
    t.add_argument("file", type=Path)
    sub.add_parser("remove")
    args = ap.parse_args()
    env = read_env()
    if env.get("PLAID_ENV") != "sandbox":
        sys.exit("PLAID_ENV must be 'sandbox'")
    if args.command == "setup":
        setup(env, args.replace)
    elif args.command == "transactions":
        add_transactions(env, args.file)
    else:
        plaid(env, "/item/remove", access_token=env["PLAID_ACCESS_TOKEN"])
        set_env("PLAID_ACCESS_TOKEN", "")
        print("sandbox connection removed")


if __name__ == "__main__":
    main()
