#!/bin/sh
# The public demo pages' database role (AP_DEMO_DB_USER): only what the pages need, nothing of the
# live data. Runs on the first start of an empty volume (after the migrations), and can be re-run
# any time, e.g. after a migration that touches the demo tables (idempotent):
#   docker compose exec postgres sh /docker-entrypoint-initdb.d/02_demo_role.sh
# Without AP_DEMO_DB_USER and AP_DEMO_DB_PASSWORD it does nothing.
#
# The pages: start sessions (demo_sessions, demo_starts), queue files (demo_uploads, without reading
# any file back), read results through the demo-only view, read bill previews, append to the log.
set -eu

if [ -z "${AP_DEMO_DB_USER:-}" ] || [ -z "${AP_DEMO_DB_PASSWORD:-}" ]; then
  echo "demo role: AP_DEMO_DB_USER / AP_DEMO_DB_PASSWORD not set, skipped"
  exit 0
fi

psql -v ON_ERROR_STOP=1 -X -q \
  --username "$POSTGRES_USER" --dbname "$N8N_DB_NAME" \
  -v db="$N8N_DB_NAME" -v demo_user="$AP_DEMO_DB_USER" -v demo_pw="$AP_DEMO_DB_PASSWORD" <<'SQL'
SELECT NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'demo_user') AS missing \gset
\if :missing
CREATE ROLE :"demo_user" LOGIN PASSWORD :'demo_pw';
\else
ALTER ROLE :"demo_user" LOGIN PASSWORD :'demo_pw';
\endif
ALTER ROLE :"demo_user" SET search_path = ap;
ALTER ROLE :"demo_user" CONNECTION LIMIT 20;
GRANT CONNECT ON DATABASE :"db" TO :"demo_user";
GRANT USAGE ON SCHEMA ap TO :"demo_user";

-- Start from nothing on every run, so a right removed here is removed in the database too
REVOKE ALL ON ALL TABLES IN SCHEMA ap FROM :"demo_user";
REVOKE ALL ON ALL SEQUENCES IN SCHEMA ap FROM :"demo_user";

GRANT SELECT, INSERT ON ap.demo_sessions, ap.demo_starts TO :"demo_user";
-- Queue a file; read the queue without its bytes (content is never read back)
GRANT INSERT ON ap.demo_uploads TO :"demo_user";
GRANT SELECT (id, session_id, kind, sample, file_name, size, status, error, invoice_id, created_at, updated_at)
  ON ap.demo_uploads TO :"demo_user";
GRANT SELECT ON ap.demo_invoice_results, ap.demo_bill_previews TO :"demo_user";
-- The log: append only (the insert reads an invoice's scope)
GRANT INSERT ON ap.processing_log TO :"demo_user";
GRANT SELECT (id, scope) ON ap.invoices TO :"demo_user";
SQL
echo "demo role: $AP_DEMO_DB_USER ready"
