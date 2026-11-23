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

## Chart of accounts (QuickBooks)

[`config/chart_of_accounts_us.json`](../config/chart_of_accounts_us.json) lists a standard US small-business chart: the expense accounts supplier invoices are usually coded to, fixed assets, prepaid expenses and cost of goods sold, each with its QuickBooks account type and detail type. `same_as` names an existing account that already plays the role (e.g. *Rent or Lease* for rent), so no near-duplicate is created.

```bash
set -a; . ./.env; set +a
python3 scripts/create_chart_of_accounts.py           # dry run: which accounts exist, which would be created
python3 scripts/create_chart_of_accounts.py --apply   # create the missing ones
```

- **It never changes or deletes an account.** It only reads the chart and creates accounts: the QuickBooks helper ([`scripts/qbo_via_n8n.py`](../scripts/qbo_via_n8n.py)) refuses any request that could update one.
- **Running it twice does nothing.** An account that exists (by full name, or a `same_as` name) is skipped, and each create carries a fixed request ID.
- **QuickBooks tokens stay in n8n.** The calls run through a temporary n8n workflow using the QuickBooks credential, deleted with its execution after the run.

Then run **AP – Sync QuickBooks reference data** by hand, so the review page's picker shows the new accounts at once.

The chart includes **Payment differences** (*Other Expense*, detail *Other Miscellaneous Expense*; it takes both debits and credits). Reviewers write the open part of under-paid bills off to it. Put its QuickBooks ID in `QBO_WRITE_OFF_ACCOUNT_ID`; until then, a write-off fails and shows on its review item. It is hidden from the review page's account picker: it is never an invoice's expense account.

## Review page accounts

The review page (`http://localhost:8081`, internal listener) has no self-registration. Accounts are managed with a CLI in its container; passwords are typed at a prompt (twice), never passed as arguments:

```bash
docker compose exec review-web python -m app.web.users add --email you@example.com --name "Your Name" --role admin --scope live
docker compose exec review-web python -m app.web.users list
docker compose exec review-web python -m app.web.users set-password --email you@example.com   # ends the account's sessions
docker compose exec review-web python -m app.web.users deactivate --email you@example.com     # ends the account's sessions
docker compose exec review-web python -m app.web.users unlock --email you@example.com         # after 5 failed sign-ins
```

Roles: `reviewer` and `admin` work on `live` (or `e2e`) data and sign in only on the internal listener; `demo` works on `demo` data. The display name is what the audit trail records for each decision. On a server, the internal listener is reached through an SSH tunnel: `ssh -N -L 8081:127.0.0.1:8081 user@server`. Details: [security](security.md).

## Keeping an invoice out of accounting

`POST /invoices/{id}/exclude-from-posting` with `{"reason": "superseded_test_data" | "manual", "note": "…"}`. It is logged and refused once the invoice is posted (reverse it in QuickBooks instead).

Used once on 2026-11-22 for invoice `f6a94cb0-…` (BW-1042, Step 5 test of Day 1): its fictional supplier name was too close to a real company and was renamed in the test set, and it has the same amount as the renamed invoice, so posting it would also confuse bank matching.

## Retrying a failed posting

A posting that failed permanently (validation error in QuickBooks, attempts used up) opens a review item, `quickbooks_refused`, that shows QuickBooks' error. After fixing the cause, the reviewer chooses **Try again in QuickBooks** on the review page. Operators can do the same with `POST /postings/{invoice_id}/retry` (`{"note": "…"}`): attempts go back to 0, the item waits for the posting, and the request is logged. The operator retry is refused while a review item for another reason (e.g. `vendor_currency_conflict`) is open: a reviewer decides those.

## Run now

The workflow **AP – Run now** starts the posting or the bank workflow at once, instead of waiting for the schedule (for a demo, or after fixing something in QuickBooks):

```bash
set -a; . ./.env; set +a
python3 scripts/run_now.py posting   # or: bank
```

- **The token never travels.** n8n stores every webhook request, headers included, even for a workflow set to save nothing: the execution is soft-deleted, then pruned about an hour later. So a request carries a timestamp and `HMAC-SHA256(RUN_NOW_TOKEN, "<timestamp>.<target>")`, not the token. The core checks the signature in constant time. It accepts it within 5 minutes of its timestamp, and only once: what n8n keeps can't be reused.
- **Refused:** no or wrong signature (401), expired or reused (401), unknown target (400), not configured (`RUN_NOW_TOKEN` empty: 503).
- **Rate limits:** 2 runs per target per minute. After 10 refused requests in 10 minutes, every request is refused for 10 minutes (429 with `retry_after`).
- Every request is recorded in `run_requests` and `processing_log` (step `run_now`), without the token.
- **Rotating the token:** change `RUN_NOW_TOKEN` in `.env`, then `docker compose up -d core`.

## Updating the workflows in n8n

The builders in `n8n/build_*.py` are the source of truth. To load a changed workflow into the running n8n (credential IDs from a local file, never committed):

```bash
python3 n8n/build_bank_sync.py --credentials cred_ids.json \
  | python3 -c "import json,sys; d=json.load(sys.stdin); d['id']='apBankSync000001'; print(json.dumps(d))" \
  | docker compose exec -T n8n sh -c "cat > /tmp/w.json && n8n import:workflow --input=/tmp/w.json; n8n publish:workflow --id=apBankSync000001; rm -f /tmp/w.json"
docker compose restart n8n
```

Workflow IDs: `apEmailIntake001`, `apPostQbo0000001`, `apBankSync000001`, `apSyncQboRef0001`, `apRunNow00000001`. An import leaves the workflow unpublished, so publish it again. Wait until the log says "Finished building workflow dependency index" before calling a webhook (until then it answers 404). `scripts/export_workflow.sh` brings editor changes back for comparison.
