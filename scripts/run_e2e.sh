#!/bin/sh
# Runs scripts/run_e2e.py in the test container against the real databases (read-only).
#   scripts/run_e2e.sh --since 2026-11-22T16:00:00Z [--timeout 600] [--json test-data/results/run.json]
set -eu
cd "$(dirname "$0")/.."
set -a; . ./.env; set +a
exec docker compose run --rm --no-deps --user "$(id -u):$(id -g)" \
  -e AP_E2E_DATABASE_URL="postgresql://${AP_DB_USER}:${AP_DB_PASSWORD}@postgres:5432/${N8N_DB_NAME}" \
  -e N8N_E2E_DATABASE_URL="postgresql://${N8N_DB_USER}:${N8N_DB_PASSWORD}@postgres:5432/${N8N_DB_NAME}" \
  -v "$PWD/scripts:/scripts:ro" -v "$PWD/test-data/results:/results" \
  core-test python /scripts/run_e2e.py "$@"
