# Deployment

The stack runs on one small Ubuntu server (4 GB RAM) with Docker Compose: the same `docker-compose.yml` as on a workstation, plus [`docker-compose.prod.yml`](../docker-compose.prod.yml) for the server settings. The server's `.env` selects both files with `COMPOSE_FILE=docker-compose.yml:docker-compose.prod.yml`, so every `docker compose` command and every script uses them.

## What is reachable from where

| Service | Internet | Server only | How a person reaches it |
|---|---|---|---|
| Caddy (HTTPS) | 80, 443 (TCP and UDP) | | |
| n8n editor, sign-in, OAuth callback, webhooks | **no** | `127.0.0.1:5678` | SSH tunnel |
| Review page, internal listener | **no** | `127.0.0.1:8081` | SSH tunnel ([security](security.md)) |
| Core API, PostgreSQL | **no** | Docker network only | |

- **Firewall** (`ufw`): incoming traffic denied by default; SSH, 80/tcp, 443/tcp and 443/udp allowed. Docker publishes ports past `ufw`, which is why every port except Caddy's is bound to `127.0.0.1` in the compose files.
- **SSH:** keys only, no root login.
- **Checked on every commit and in CI** ([`scripts/check_deploy_config.sh`](../scripts/check_deploy_config.sh)): the merged compose files and the Caddyfile are valid, only Caddy publishes ports beyond `127.0.0.1`, and Caddy never forwards a whole host. A proxy route without a path matcher fails the check.

### Why n8n isn't on the internet

n8n's sign-in page served under a custom domain looks to Google Safe Browsing like a copy of the n8n login page, and a host serving it can be flagged as phishing. A flagged host can taint the whole domain, including the public demo pages. So nothing of n8n is served publicly: its editor is used through a tunnel, like the review page, and all of its workflows either poll (Gmail, Google Drive, schedules) or are called from the server itself (`scripts/run_now.py`).

A public webhook, if one is ever needed (e.g. an upload form handled by n8n), gets a host of its own in the [Caddyfile](../deploy/Caddyfile) that forwards **only** exact `POST` paths and answers every other path with an empty 404. `WEBHOOK_URL` then names that host.

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
