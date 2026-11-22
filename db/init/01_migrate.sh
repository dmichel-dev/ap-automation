#!/bin/sh
# Applies db/migrations/*.sql in order, as the ap_app role, each in its own transaction.
# Runs automatically on the first start of an empty volume, and can be re-run any time:
#   docker compose exec postgres sh /docker-entrypoint-initdb.d/01_migrate.sh
set -eu

MIGRATIONS_DIR=/migrations
PSQL="psql -v ON_ERROR_STOP=1 -X -q --username $AP_DB_USER --dbname $N8N_DB_NAME"

$PSQL -c "CREATE TABLE IF NOT EXISTS ap.schema_migrations (
  version    text PRIMARY KEY,
  applied_at timestamptz NOT NULL DEFAULT now()
);"

for f in "$MIGRATIONS_DIR"/*.sql; do
  [ -e "$f" ] || continue
  version=$(basename "$f" .sql)
  applied=$($PSQL -tAc "SELECT 1 FROM ap.schema_migrations WHERE version = '$version'")
  if [ "$applied" = "1" ]; then
    echo "migration $version: already applied"
    continue
  fi
  echo "migration $version: applying"
  $PSQL --single-transaction -f "$f" -c "INSERT INTO ap.schema_migrations (version) VALUES ('$version')"
done
