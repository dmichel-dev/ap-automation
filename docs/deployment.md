# Deployment

The stack runs on one small Ubuntu server (4 GB RAM) with Docker Compose: the same `docker-compose.yml` as on a workstation, plus [`docker-compose.prod.yml`](../docker-compose.prod.yml) for the server settings. The server's `.env` selects both files with `COMPOSE_FILE=docker-compose.yml:docker-compose.prod.yml`, so every `docker compose` command and every script uses them.

## What is reachable from where

| Service | Internet | Server only | How a person reaches it |
|---|---|---|---|
| Caddy (HTTPS): home page, demo pages | 80, 443 (TCP and UDP) | | |
| n8n editor, sign-in, OAuth callback, webhooks | **no** | `127.0.0.1:5678` | SSH tunnel |
| Review page, internal listener | **no** | `127.0.0.1:8081` | SSH tunnel ([security](security.md)) |
| Core API, PostgreSQL | **no** | Docker network only | |

- **Firewall** (`ufw`): incoming traffic denied by default; SSH, 80/tcp, 443/tcp and 443/udp allowed. Docker publishes ports past `ufw`, which is why every port except Caddy's is bound to `127.0.0.1` in the compose files.
- **SSH:** keys only, no root login.
- **Checked on every commit and in CI** ([`scripts/check_deploy_config.sh`](../scripts/check_deploy_config.sh)): the merged compose files and the Caddyfile are valid, only Caddy publishes ports beyond `127.0.0.1`, and Caddy never forwards a whole host. A proxy route without a path matcher fails the check.

### Why n8n isn't on the internet

n8n's sign-in page served under a custom domain looks to Google Safe Browsing like a copy of the n8n login page, and a host serving it can be flagged as phishing. A flagged host can taint the whole domain, including the public demo pages. So nothing of n8n is served publicly: its editor is used through a tunnel, like the review page, and all of its workflows either poll (Gmail, Google Drive, schedules) or are called from the server itself (`scripts/run_now.py`).

A public webhook, if one is ever needed (e.g. an upload form handled by n8n), gets a host of its own in the [Caddyfile](../deploy/Caddyfile) that forwards **only** exact `POST` paths and answers every other path with an empty 404. `WEBHOOK_URL` then names that host.

**Home page** (`HOME_HOST`, e.g. `davidmichel.dev`): static files from the [site repository](https://github.com/dmichel-dev/davidmichel.dev), cloned next to this one on the server; Caddy serves its `public/` folder read-only (`SITE_DIR`). The pages have no scripts and load nothing from elsewhere, so Caddy sends a Content-Security-Policy that allows only the site's own stylesheet, fonts and images. Publishing a change is a `git pull` in that clone: Caddy reads the files from disk and needs no restart. `www.` + `HOME_HOST` redirects permanently to it, path kept.

**Retired host names** (`RETIRED_HOSTS` in `.env`, e.g. the former public name of the editor) answer an empty 404 on every path until their DNS record is removed.

## Using the n8n editor

```bash
ssh -N -L 5679:127.0.0.1:5678 <server>     # keep it open while you work
```

Then open **http://localhost:5679**. Local port 5679 leaves 5678 free for a local n8n. The n8n settings that follow from it (`docker-compose.prod.yml`):

| Setting | Value | Why |
|---|---|---|
| `N8N_EDITOR_BASE_URL` | `http://localhost:5679/` | The editor's links and the OAuth redirect URL use the tunnel's address |
| `WEBHOOK_URL` | `http://localhost:5678/` | Webhooks are called on the server itself |
| `N8N_SECURE_COOKIE` | `true` | Browsers accept secure cookies on `http://localhost` |

Sign in with the owner account; it has two-factor authentication on.

## Connecting or reconnecting an OAuth credential (Gmail, Google Drive, QuickBooks)

The access and refresh tokens are stored, encrypted, in n8n, and n8n refreshes them by calling Google and Intuit directly. That doesn't involve the redirect URL, so the credentials keep working without the tunnel. A reconnect is only needed when a token can no longer be refreshed: consent revoked, a scope added, the account password changed, or an Intuit refresh token left unused for too long.

**Once per console**, the redirect URL must be registered (next to the one for a local n8n on port 5678):

| Console | Where | Redirect URL |
|---|---|---|
| Google Cloud | APIs & Services → Credentials → the OAuth client (Internal app) → Authorized redirect URIs | `http://localhost:5679/rest/oauth2-credential/callback` |
| Intuit Developer | the app → Keys & credentials (Development) → Redirect URIs | `http://localhost:5679/rest/oauth2-credential/callback` |

Plaid needs none: the sandbox access token is created through the API, not through Plaid Link.

**To reconnect:**

1. Open the tunnel (above) and the editor at http://localhost:5679.
2. Open the credential (names in [`n8n/nodes.py`](../n8n/nodes.py), `CREDENTIAL_NAMES`). Its **OAuth Redirect URL** must read `http://localhost:5679/rest/oauth2-credential/callback`.
3. Click **Connect my account** (or **Reconnect**), sign in and consent. Google: choose the Workspace account that owns the AP mailbox. Intuit: choose the company whose ID is `QBO_REALM_ID` in `.env`.
4. Google or Intuit sends the browser back to `localhost:5679`, which the tunnel forwards to n8n. The credential then shows **Account connected**.
5. Keep the same credential (same name, same ID): the workflows reference it, so nothing else changes.

If the browser shows "This site can't be reached" after the consent screen, the tunnel was closed: open it and start again from step 3.

## Using the review page

```bash
ssh -N -L 8081:127.0.0.1:8081 <server>     # then http://localhost:8081
```

The local port must be **8081**: the internal listener refuses any other `Host` (400), by design ([security](security.md)). If a local stack already uses 8081, stop its review page first (`docker compose stop review-web`).

## Installing on a new server

Ubuntu 24.04 with Docker from the Ubuntu archive. Commands run as a normal user in the `docker` group; `sudo` only where shown.

1. **Packages and firewall:** `sudo apt install docker-compose-v2`, then `sudo ufw allow 80/tcp`, `443/tcp` and `443/udp` (SSH is already allowed). A swap file of about 2 GB gives the image build some room.
2. **Code:** a read-only deploy key for the repository, with its own `Host` alias in `~/.ssh/config` so it serves this repository only, then `git clone`.
3. **`.env`** (mode 600, never committed), from [`.env.example`](../.env.example):
   - **Generate new** Postgres passwords, `N8N_ENCRYPTION_KEY`, `CORE_API_TOKEN` and `RUN_NOW_TOKEN` (`openssl rand -hex 32`).
   - **Keep `BANK_FINGERPRINT_KEY`** if the data comes from another instance: the stored bank-account fingerprints were computed with it.
   - Add `COMPOSE_FILE=docker-compose.yml:docker-compose.prod.yml`, `ACME_EMAIL`, `RETIRED_HOSTS`, `DEMO_HOST`, `HOME_HOST` and `BACKUP_KEEP_DAYS=14`.
   - **Store `N8N_ENCRYPTION_KEY` and `BANK_FINGERPRINT_KEY` in a password manager.** Without the first, n8n's stored credentials can't be decrypted from a backup; changing the second resets the bank-detail comparison.
4. **Database:** `docker volume create ap-automation_pg17_data` (external, so `docker compose down -v` can never delete it), then `docker compose up -d postgres`. On its first start the container creates the roles and applies every migration.
5. **Data from another instance** (optional, see below), then `docker compose up -d core review-web n8n`.
6. **n8n owner account:** through the tunnel, **before** anything else, then turn on two-factor authentication. Until an owner exists, whoever opens n8n first can create one.
7. **Credentials**, with the exact names in `CREDENTIAL_NAMES` ([`n8n/nodes.py`](../n8n/nodes.py)). OAuth ones (Gmail, Google Drive, QuickBooks) in the editor, as above. The others (core token, database, Plaid) can be created in the editor too, or imported with `n8n import:credentials` from a temporary file built from `.env` and deleted right after; n8n encrypts them on import.
8. **Workflows:** write the instance's credential IDs to `cred_ids.json` (gitignored), then build, import and publish each workflow in the order given in [operations](operations.md#updating-the-workflows-in-n8n).
9. **Caddy:** clone the site repository next to this one (`git clone … ../davidmichel.dev`; its files must exist before Caddy starts, or Docker creates an empty folder in their place), then `docker compose up -d caddy`. It obtains the certificates on its first start.
10. **Backups:** the cron job below.

**Moving from another instance:** stop the old one from processing first (`n8n unpublish:workflow --all`, then restart its n8n), or both would read the same mailbox, Drive folder and bank feed. Then:
- Take a full backup of the old instance (`scripts/backup.sh`).
- Dump the `ap` data only: `pg_dump --schema=ap --data-only --disable-triggers --exclude-table-data=ap.schema_migrations`. The migrations already created the schema on the new server.
- Restore it with `psql --single-transaction -v ON_ERROR_STOP=1`.
- Compare a per-table fingerprint on both sides: row count plus an MD5 of every row in text form, for each `ap` table, and the sequence values.
- Copy the `ap_files` volume with `tar --numeric-owner` (owners and modes kept) and compare SHA-256 sums.
- n8n itself starts empty: credentials and workflows are recreated as above.

## Updating

```bash
git pull
docker compose build core review-web
docker compose up -d
```

Database migrations and workflow changes follow [operations](operations.md): back up first, apply the migration, then reload the changed workflows.

## Backups

```cron
20 3 * * * cd $HOME/ap-automation && scripts/backup.sh --online >> backups/backup.log 2>&1
```

- Every night at 03:20 (server time, UTC), [`scripts/backup.sh`](../scripts/backup.sh) writes a `pg_dumpall` of every database (n8n, `ap`, roles) and an archive of the n8n data volume to `backups/` (mode 600). Copies older than `BACKUP_KEEP_DAYS` are deleted, but only after a complete backup, so a failing night never removes the older copies.
- The whole server is also covered by the hosting provider's automated backup.
- The original invoice files are temporary by design and aren't backed up ([operations](operations.md#backup)).
- `backups/backup.log` shows each run. A failed run isn't reported anywhere else yet.

## n8n temporary files

```cron
*/15 * * * * cd $HOME/ap-automation && scripts/purge_n8n_temp_files.sh >> backups/purge_n8n_temp.log 2>&1
```

A run started by a trigger or a schedule (the Gmail trigger, the Drive listing, the re-check) keeps its downloaded attachments in n8n's `executions/temp/binary_data` folders, and n8n never deletes them: deleting or pruning the run removes only `executions/<id>/`. Every 15 minutes, [`scripts/purge_n8n_temp_files.sh`](../scripts/purge_n8n_temp_files.sh) deletes those older than 90 minutes. Nothing reads them once the run is over, except the n8n editor showing that old run's attachment; the core keeps its own copy of every processed file. The demo's privacy notice relies on this job ("within 90 minutes").

## Verified on the server (2026-11-25)

| Check | Result |
|---|---|
| Exposure from outside | Only 22, 80 and 443 open. Every path of the retired host (`/`, `/signin`, `/rest/*`, `/webhook/*`, the OAuth callback) answers an empty 404 to GET and POST; HTTP redirects to HTTPS; HSTS set; no `Server` header |
| Data moved | Every `ap` table identical to the old instance (row count and MD5 of every row, sequence values); stored files identical (SHA-256, owners and modes) |
| Credentials | The core accepts the token (200) and refuses a wrong one (401); the database role connects; Plaid lists the configured account. Gmail, Drive and QuickBooks connected with the same scopes as before, QuickBooks on the configured company |
| OAuth redirect URL | `http://localhost:5679/rest/oauth2-credential/callback`, computed with n8n's own code and the server's settings |
| Workflows | All 10 built with the server's credential IDs (each equal to its committed JSON without IDs), imported and published; the hourly jobs and the per-minute Drive listing run |
| Run now | Signed requests started posting and bank sync (202); a wrong token was refused (401) |
| Daily end-to-end test, run by hand | Passed: 40 checks, no failure; the test's QuickBooks bill and attachment deleted afterwards |
| Real email to the AP alias | Delivered through the alias; a known file rejected as `duplicate_file` without an OCR call; labels AP/Rejected and AP/Processed |
| Google Drive drop | The same file dropped in the Inbox: `duplicate_file`, moved to Processed |
| Backup and restore | Backup run from cron's minimal environment; the dump restored into a throwaway PostgreSQL with no network gave identical fingerprints (n8n users, credentials, workflows, settings; invoices, lines, review queue, log, migrations) |
| Memory | n8n about 500 MB of its 1 GB after the first runs; the other containers well under their limits |
