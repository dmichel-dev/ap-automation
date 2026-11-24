#!/bin/sh
# Exports the live n8n workflows into n8n/workflows/, without credentials or instance data,
# and shows what changed compared with the committed files.
#   scripts/export_workflow.sh            # all project workflows
#   scripts/export_workflow.sh <id>       # one workflow
set -eu
cd "$(dirname "$0")/.."

# workflow ID → committed file
WORKFLOWS="apEmailIntake001:ap-email-intake.json apDriveIntake001:ap-drive-intake.json apPostQbo0000001:ap-post-to-quickbooks.json
  apBankSync000001:ap-bank-sync-and-match.json apSyncQboRef0001:ap-sync-quickbooks-reference.json
  apRunNow00000001:ap-run-now.json apDailySummary01:ap-daily-summary.json
  apDailyE2eTest01:ap-daily-e2e-test.json apE2eCleanup0001:ap-e2e-quickbooks-cleanup.json"
RAW="$(mktemp)"; trap 'rm -f "$RAW"' EXIT

for entry in $WORKFLOWS; do
  ID="${entry%%:*}"; OUT="n8n/workflows/${entry#*:}"
  [ $# -gt 0 ] && [ "$1" != "$ID" ] && continue
  # Export inside the container and stream it out (credentials are stored separately, encrypted)
  docker compose exec -T n8n sh -c \
    "n8n export:workflow --id='$ID' --output=/tmp/wf-export.json >/dev/null && cat /tmp/wf-export.json && rm -f /tmp/wf-export.json" > "$RAW"
  python3 scripts/export_workflow.py "$RAW" "$OUT"
  if [ -z "$(git status --porcelain -- "$OUT")" ]; then
    echo "  no change compared with the committed $OUT"
  else
    git --no-pager diff --stat -- "$OUT" 2>/dev/null || true
  fi
done
