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

## Applying migrations to an existing database

Migrations run automatically only on an empty volume. On an existing database (back up first):

```bash
scripts/backup.sh --online
docker compose exec postgres sh /docker-entrypoint-initdb.d/01_migrate.sh   # applies only the new ones
docker compose up -d --build core
```

Migration 004 (Day 2) was applied this way on 2026-11-22, after backup `pg_dumpall_20261122-172709.sql`; 006 and 007 on 2026-11-23 (backups `pg_dumpall_20261123-113152.sql`, `pg_dumpall_20261123-114811.sql`).

## Recomputing a normalized value after a rule change

When a normalization rule changes, rows stored before it keep the old normalized copy (the duplicate key `supplier_key` and the vendor links depend on it). Recompute with the pipeline's own function; the value as printed is never changed:

```bash
docker compose exec core python -m app.maintenance renormalize-tax-ids           # dry run: what would change
docker compose exec core python -m app.maintenance renormalize-tax-ids --apply   # one transaction, logged
```

Run on 2026-11-23 after tax ID labels (`EIN`, `VAT`, `USt-IdNr.`, `登録番号`…) were removed from the normalized copy: 3 invoices (`EIN000000001` → `000000001`, `EIN000000002` → `000000002`) and their 2 vendor links moved; a second dry run found nothing.

## Bank connection (Plaid sandbox)

```bash
python3 scripts/plaid_sandbox.py setup [--replace]        # test bank login, checking account registered in the core,
                                                         # token in .env + n8n credential "Plaid – Sandbox" (never printed)
python3 scripts/plaid_sandbox.py transactions test-data/bank/matching-transactions.json   # custom test transactions
python3 scripts/plaid_sandbox.py remove
```

The workflow **AP – Bank sync & match** runs every hour (or by hand). A failed sync page stops the run; the next run restarts from the saved cursor. A bill payment that fails is retried on later runs while attempts remain; a permanent failure stops it (`matches.payment_last_error`), and a review item is opened when the failure needs a person.

## QuickBooks reference data

The workflow **AP – Sync QuickBooks reference data** runs every hour (or by hand). It copies the chart of accounts and the vendor list into `ap.qbo_accounts` / `ap.qbo_vendors`: the account picker and the known-supplier check read this copy, so QuickBooks tokens stay in n8n. After creating an account in QuickBooks, run it by hand to see it in the picker at once. If the core refuses an answer (QuickBooks error, empty account list, a full page of 1,000 rows), the run fails and the previous copy stays.

## Keeping an invoice out of accounting

`POST /invoices/{id}/exclude-from-posting` with `{"reason": "superseded_test_data" | "manual", "note": "…"}`. It is logged and refused once the invoice is posted (reverse it in QuickBooks instead).

Used once on 2026-11-22 for invoice `f6a94cb0-…` (BW-1042, Step 5 test of Day 1): its fictional supplier name was too close to a real company and was renamed in the test set, and it has the same amount as the renamed invoice, so posting it would also confuse bank matching.

## Retrying a failed posting

A posting that failed permanently (validation error in QuickBooks, attempts used up) is retried after the cause is fixed with `POST /postings/{invoice_id}/retry` (`{"note": "…"}`): attempts go back to 0 and the request is logged. It is refused while an open `posting` review item exists for the invoice: the reviewer decides those (Day 3).
