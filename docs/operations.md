# Operations

## Backup

```bash
scripts/backup.sh            # stops n8n and core during the dump (consistent)
scripts/backup.sh --online   # no downtime
```

This writes `backups/pg_dumpall_<timestamp>.sql` (all databases and roles) and `backups/n8n_data_<timestamp>.tgz`. Both are gitignored and mode 600, because they contain password hashes and invoice data. The original invoice files (`ap_files` volume) are temporary by design, so they're not backed up.

`N8N_ENCRYPTION_KEY` (in `.env`) is needed to decrypt the n8n credentials in a backup: **keep a copy of `.env` with the backups, somewhere safe.**

## Verify a restore or an upgrade

[`scripts/db_fingerprint.sql`](../scripts/db_fingerprint.sql) prints row counts and hashes (users, credentials, workflows, invoices, log…). It shows no secrets. Run it before and after, then diff the two outputs:

```bash
docker compose cp scripts/db_fingerprint.sql postgres:/tmp/fp.sql
docker compose exec -T postgres psql -X -q -U postgres -d n8n -f /tmp/fp.sql > before.txt
```

## Postgres major upgrade (done 16 → 17 on 2026-11-22)

1. Back up with the **new** version's client: `scripts/backup.sh` (after changing the image tag, or run `pg_dumpall` from the new image as in the script).
2. Create a new volume and restore into it through a temporary container *without* the init scripts, so the dump is restored as-is:
   ```bash
   docker volume create ap-automation_pg17_data
   docker run -d --name ap-pg17-restore -v ap-automation_pg17_data:/var/lib/postgresql/data \
     -v "$PWD/backups":/backups:ro -e POSTGRES_USER=postgres -e POSTGRES_PASSWORD=... postgres:17.12-alpine
   docker exec ap-pg17-restore psql -U postgres -d postgres -f /backups/<dump>.sql
   docker stop ap-pg17-restore && docker rm ap-pg17-restore
   ```
   Two errors are expected and harmless: the dump tries to drop and recreate the `postgres` superuser that is running the restore.
3. Point `docker-compose.yml` at the new image and volume, then `docker compose up -d`.
4. Verify:
   - The fingerprints are identical, except `server_version`.
   - n8n starts without the "Postgres outside the supported range" warning.
   - The Gmail trigger polls (`N8N_LOG_LEVEL=debug docker compose up -d n8n` shows "Poll trigger initiated").
   - The credentials decrypt, and the tests pass.
5. Keep the old volume (`ap-automation_pg_data`) as a fallback for a while, then delete it: `docker volume rm ap-automation_pg_data`.

**Rollback:** switch the image back to `postgres:16.16-alpine` and the volume back to `pg_data` in `docker-compose.yml`, then `docker compose up -d`.
