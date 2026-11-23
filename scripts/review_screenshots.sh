#!/bin/sh
# Screenshots of the review page on the five sample items (fictional suppliers, test database only).
#   sh scripts/review_screenshots.sh
# Needs Python with Playwright (pip install playwright) and Google Chrome on this machine.
# Rebuilds the ap_test database (as the test suite does): don't run it while the tests run.
set -eu
cd "$(dirname "$0")/.."
NAME=ap-review-samples
PORT=8091
PASSWORD=$(python3 -c "import secrets; print(secrets.token_urlsafe(18))")

docker rm -f "$NAME" >/dev/null 2>&1 || true
trap 'docker rm -f "$NAME" >/dev/null 2>&1 || true' EXIT
docker compose --profile test run -d --name "$NAME" -p "127.0.0.1:$PORT:$PORT" \
  -e SAMPLE_PASSWORD="$PASSWORD" -e FILE_STORAGE_DIR=/tmp/samples \
  -e REVIEW_LISTENER=internal -e REVIEW_ORIGINS="http://localhost:$PORT" -e REVIEW_ENVIRONMENT=test \
  core-test sh -c "python -m tests.review_samples /tmp/samples && AP_DATABASE_URL=\$AP_TEST_DATABASE_URL \
    exec uvicorn --factory app.web.main:create_app --host 0.0.0.0 --port $PORT --no-server-header" >/dev/null

until curl -fs "http://localhost:$PORT/health" >/dev/null 2>&1; do
  docker ps -q -f name="$NAME" | grep -q . || { docker logs "$NAME"; exit 1; }
  sleep 1
done
SAMPLE_PASSWORD="$PASSWORD" ${PYTHON:-python3} scripts/review_screenshots.py "http://localhost:$PORT" docs/screenshots
