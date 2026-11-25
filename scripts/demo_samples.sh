#!/bin/sh
# Runs scripts/demo_samples.py in the test container: records the OCR response of each public demo
# sample that has none yet (one Mistral OCR call each).   scripts/demo_samples.sh [--force]
set -eu
cd "$(dirname "$0")/.."
exec docker compose --profile test run --rm --no-deps --user "$(id -u):$(id -g)" \
  -e PYTHONPATH=/app \
  -v "$PWD/scripts:/scripts:ro" \
  core-test python /scripts/demo_samples.py "$@"
