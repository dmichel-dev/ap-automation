#!/bin/sh
# Exports the live "AP – Email intake" workflow from n8n into n8n/workflows/, without
# credentials or instance data, and shows what changed compared with the committed file.
#   scripts/export_workflow.sh [workflow-id]
set -eu
cd "$(dirname "$0")/.."
ID="${1:-apEmailIntake001}"
OUT="n8n/workflows/ap-email-intake.json"
RAW="$(mktemp)"; trap 'rm -f "$RAW"' EXIT

# Export inside the container to a temp file, then stream it out (nothing secret is in a
# workflow export: credentials are stored separately and encrypted)
docker compose exec -T n8n sh -c \
  "n8n export:workflow --id='$ID' --output=/tmp/wf-export.json >/dev/null && cat /tmp/wf-export.json && rm -f /tmp/wf-export.json" > "$RAW"

python3 scripts/export_workflow.py "$RAW" "$OUT"
if git diff --quiet -- "$OUT"; then
  echo "no change compared with the committed workflow"
else
  git --no-pager diff --stat -- "$OUT"
fi
