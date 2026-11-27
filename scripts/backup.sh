#!/bin/sh
# Full backup: all Postgres databases (n8n + ap + roles) and the n8n data volume.
# Output in ./backups (gitignored, mode 600): it contains password hashes and invoice data.
#
#   scripts/backup.sh            # consistent backup: stops n8n and core during the dump
#   scripts/backup.sh --online   # no downtime (pg_dumpall is consistent per database)
#
# On the server it runs every night from cron, and BACKUP_KEEP_DAYS prunes old copies
# (docs/deployment.md).
#
# Restore into an EMPTY Postgres data volume (see docs/operations.md):
#   psql -U postgres -d postgres -f backups/<file>.sql
set -eu
cd "$(dirname "$0")/.."

set -a; . ./.env; set +a
PG_IMAGE=$(grep -oE 'postgres:[0-9.]+-alpine' docker-compose.yml | head -1)
TS=$(date +%Y%m%d-%H%M%S)
mkdir -p backups && chmod 700 backups

if [ "${1:-}" != "--online" ]; then
  docker compose stop n8n core
  trap 'docker compose start core n8n >/dev/null' EXIT
fi

docker run --rm --network ap-automation_backend -e PGPASSWORD="$POSTGRES_ADMIN_PASSWORD" "$PG_IMAGE" \
  pg_dumpall -h postgres -U "$POSTGRES_ADMIN_USER" --clean --if-exists > "backups/pg_dumpall_$TS.sql"
chmod 600 "backups/pg_dumpall_$TS.sql"
tail -3 "backups/pg_dumpall_$TS.sql" | grep -q "cluster dump complete" || { echo "dump incomplete" >&2; exit 1; }

docker run --rm -v ap-automation_n8n_data:/src:ro -v "$PWD/backups":/dst alpine:3.24 \
  sh -c "tar czf /dst/n8n_data_$TS.tgz -C /src . && chown $(id -u):$(id -g) /dst/n8n_data_$TS.tgz && chmod 600 /dst/n8n_data_$TS.tgz"

ls -l "backups/pg_dumpall_$TS.sql" "backups/n8n_data_$TS.tgz"

# Retention (BACKUP_KEEP_DAYS in .env; empty keeps everything). Reached only after a complete
# backup, so a failing night never deletes the older copies. Copies older than the limit minus
# one hour are deleted (14 days: -mmin +20100), so the night N days later removes the copy
# even if the run starts a little earlier than the one that wrote it. (-mtime +14 would only
# match after 15 full days.)
if [ -n "${BACKUP_KEEP_DAYS:-}" ]; then
  KEEP_MINUTES=$((BACKUP_KEEP_DAYS * 1440 - 60))
  find backups -maxdepth 1 -type f \( -name 'pg_dumpall_*.sql' -o -name 'n8n_data_*.tgz' \) \
    -mmin +"$KEEP_MINUTES" -print -delete
fi
