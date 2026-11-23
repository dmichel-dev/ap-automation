"""Starts a workflow now through the 'AP – Run now' webhook, with a signed request.

Usage (RUN_NOW_TOKEN from .env):
  set -a; . ./.env; set +a
  python3 scripts/run_now.py posting            # post approved bills to QuickBooks
  python3 scripts/run_now.py bank               # bank sync, matching, payments, write-offs
  python3 scripts/run_now.py bank --url https://n8n.example.com/webhook/ap-run-now

The token never leaves this machine: the request carries a timestamp and
HMAC-SHA256(token, "<timestamp>.<target>"), valid for a few minutes and only once. The core checks it
(services/core/app/run_now.py).
"""

import argparse
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.request

DEFAULT_URL = "http://localhost:5678/webhook/ap-run-now"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("target", choices=("posting", "bank"))
    parser.add_argument("--url", default=DEFAULT_URL)
    args = parser.parse_args()
    token = os.environ.get("RUN_NOW_TOKEN", "")
    if not token:
        print("RUN_NOW_TOKEN is not set (load .env first)", file=sys.stderr)
        return 2
    timestamp = str(int(time.time()))
    signature = hmac.new(token.encode(), f"{timestamp}.{args.target}".encode(), hashlib.sha256).hexdigest()
    request = urllib.request.Request(
        args.url,
        data=json.dumps({"target": args.target}).encode(),
        headers={"Content-Type": "application/json", "X-Run-Timestamp": timestamp, "X-Run-Signature": signature},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            print(response.status, response.read().decode())
            return 0
    except urllib.error.HTTPError as e:
        print(e.code, e.read().decode(), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
