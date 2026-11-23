#!/bin/sh
# Pre-commit check: each committed workflow JSON must be exactly what its builder generates
# (the builders are the source of truth; edits made in the n8n editor are exported, then
# brought back into the builder).
set -eu
cd "$(dirname "$0")/.."
status=0
for pair in build_email_intake.py:ap-email-intake.json build_post_qbo.py:ap-post-to-quickbooks.json; do
  if ! python3 "n8n/${pair%%:*}" | cmp -s - "n8n/workflows/${pair#*:}"; then
    echo "n8n/workflows/${pair#*:} differs from the output of n8n/${pair%%:*}"; status=1
  fi
done
exit $status
