#!/bin/sh
# Runs scripts/calibrate.py in the test container: the core's extraction and checks on every test
# document, compared with its expected result. Nothing is stored; one Mistral OCR call per document.
#   scripts/calibrate.sh [--only ID ...] [--json test-data/results/calibration.json]
set -eu
cd "$(dirname "$0")/.."
exec docker compose --profile test run --rm --no-deps --user "$(id -u):$(id -g)" \
  -e PYTHONPATH=/app \
  -v "$PWD/scripts:/scripts:ro" -v "$PWD/test-data/results:/results" \
  core-test python /scripts/calibrate.py "$@"
