#!/bin/sh
# Pre-commit check: each committed workflow JSON must be exactly what its builder generates
# (the builders are the source of truth; edits made in the n8n editor are exported, then
# brought back into the builder).
set -eu
cd "$(dirname "$0")/.."
status=0
for pair in build_email_intake.py:ap-email-intake.json build_post_qbo.py:ap-post-to-quickbooks.json \
    build_bank_sync.py:ap-bank-sync-and-match.json build_sync_reference.py:ap-sync-quickbooks-reference.json \
    build_run_now.py:ap-run-now.json build_error_log.py:ap-error-log.json \
    build_daily_summary.py:ap-daily-summary.json build_e2e_test.py:ap-daily-e2e-test.json \
    build_e2e_cleanup.py:ap-e2e-quickbooks-cleanup.json build_drive_intake.py:ap-drive-intake.json \
    build_demo_processing.py:ap-demo-processing.json; do
  if ! python3 "n8n/${pair%%:*}" | cmp -s - "n8n/workflows/${pair#*:}"; then
    echo "n8n/workflows/${pair#*:} differs from the output of n8n/${pair%%:*}"; status=1
  fi
done
# Readable canvases: every node in one section note, nothing overlapping
python3 n8n/check_layout.py || status=1
exit $status
