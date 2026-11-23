# Security of the review page

The review page is where people decide invoices that the checks couldn't pass alone. A decision makes an invoice ready to be **posted** to QuickBooks as a bill (payments are recorded separately, from bank matching). The page shows supplier documents, which may contain personal data.

Code: [`services/core/app/web/`](../services/core/app/web/). Tests: [`test_web.py`](../services/core/tests/test_web.py) (one test per rule below).

## What it protects against

| Threat | Main defences |
|---|---|
| Password guessing | Argon2id hashes, account lockout, per-address limit, no account enumeration |
| Stolen or fixed session | Random server-side sessions, hardened cookie, short lifetimes, new session at each sign-in |
| Cross-site request forgery | Per-session token on every form, same-origin check, `SameSite=Strict` |
| Script injection through document content (supplier names, OCR text) | Escaped templates, a CSP without inline code |
| Malicious PDF or image | Documents shown as images rendered on the server, in a limited child process |
| A demo visitor seeing real data | Every query filtered by the user's scope; anything else answers 404 |
| A reviewer account exposed on the internet | Reviewer and admin accounts sign in only on the internal listener |

## Two listeners

The same code runs in one of two modes, fixed when the process starts (`REVIEW_LISTENER`), never chosen by a request:

| | Internal | Public |
|---|---|---|
| Reached | through an SSH tunnel to `127.0.0.1:8081` | through the reverse proxy (HTTPS) |
| Who can sign in | reviewer, admin, demo | **demo only** |
| Origins (`REVIEW_ORIGINS`) | loopback only (`localhost`, `127.0.0.1`) | HTTPS only |
| Proxy headers (`X-Forwarded-For`, `Forwarded`, …) | refused (400) | used only from the trusted proxy's address |

Reaching the internal listener on a server:

```bash
ssh -N -L 8081:127.0.0.1:8081 user@server   # then open http://localhost:8081
```

How the rule "internal roles never from the internet" is enforced, layer by layer:

1. **Docker** publishes the internal listener on the host's loopback only (`127.0.0.1:8081`).
2. **Startup** refuses settings that contradict the mode: an internal listener with a non-loopback origin or a trusted proxy, a public listener with an `http://` origin. A missing mode defaults to `public`, the stricter one.
3. **Every request** to the internal listener with proxy headers or an unknown `Host` is refused. If the proxy were ever pointed at it by mistake, it would answer 400.
4. **Sign-in** on the public listener treats reviewer and admin accounts as unknown: same message, same timing, and no failed-attempt count. Someone on the internet can't lock a reviewer out.
5. **Sessions** belong to the listener they were created on, and the role is checked again on every request. An internal session cookie is useless on the public site.

## Sign-in

- **Accounts are created with a command-line tool** ([operations](operations.md#review-page-accounts)). There is no self-registration and no reset by email. Passwords are typed at a prompt, never passed as arguments.
- **Passwords:** 12 to 128 characters, not the email address. They are stored as **Argon2id** hashes (19 MiB, 2 passes: OWASP's minimum, sized for a small container). At most two hashes are computed at once, so a burst of sign-ins can't exhaust memory. A hash made with older parameters is upgraded at the next sign-in.
- **One message for every failure:** "Email or password incorrect, or too many failed attempts". An unknown address is checked against a dummy hash, so the timing doesn't reveal which addresses have an account either.
- **Lockout:** after 5 failed attempts the account is locked for 15 minutes, then 30, 60… up to 4 hours for repeated locks. While it is locked, even the right password is refused. A successful sign-in resets the count. An admin can unlock an account with the CLI.
- **Per address:** 20 attempts in 10 minutes, then `429` with `Retry-After`. Attempts over the limit are refused before anything is written, so they can't grow the table.
- **Attempts are logged** (`login_attempts`, kept 30 days) with the address, the outcome and a hash of the typed email, never the email itself or a password.

## Sessions

- The session cookie holds a random 256-bit token. The database stores only its SHA-256, so a copy of the table can't be used as a cookie.
- The cookie is `__Host-ap_review` with `Secure`, `HttpOnly`, `SameSite=Strict`, `Path=/` and no `Domain`. The `__Host-` prefix means no subdomain can set or overwrite it. It has no `Max-Age`: it ends with the browser session, and the server enforces the time limits.
- **Limits:** 30 minutes idle, 8 hours at most.
- **Sign-in always creates a new session.** Any session cookie the browser already held is ended, which prevents session fixation.
- **Sign-out deletes the session on the server.** A new password or a deactivated account ends all of that account's sessions.

## Cross-site requests

- **Every form carries a token** that must match the session's, compared in constant time. Before signing in, the form's token is matched against a `__Host-` cookie set with the form.
- **Every POST must come from the page's own origin:** `Origin` must be one of the configured origins and match the `Host`. When `Origin` is missing, `Sec-Fetch-Site: same-origin` is required. Anything else is refused with 403.
- `SameSite=Strict` on the cookies, and GET requests never change anything.
- A form body must declare its length and stay under 64 KB (`411` / `413` otherwise), since forms are read into memory.

## Rate limits

| What | Limit | Then |
|---|---|---|
| Failed sign-ins per account | 5 | locked 15 min, doubling up to 4 h |
| Sign-in attempts per address | 20 in 10 min | `429` with `Retry-After` |
| Decisions per session | 60 a minute | `429` (stops a runaway script; a person never gets near it) |

When the public listener is deployed, the reverse proxy also limits requests per address in front.

## Documents

The browser never opens the original PDF. Each page is **rendered on the server as a PNG** and shown as an image, so scripts, forms, links or exploits aimed at a browser's PDF viewer never reach it. Rendering happens in a **child process**:

- **Limits:** memory (768 MB of address space), 10 s of CPU, a 15 s timeout, and 32 open files.
- **Clean environment:** none of the service's settings, so no database URL or secret.
- **Size caps:** at most 20 pages, 40 megapixels per source image (decompression bombs are refused) and 4 megapixels per rendered page.
- **No active content:** PDF form fields are not drawn and no script runs.
- **Re-encoding:** images come out as plain PNG, so no metadata from the original survives.

The original file can be **downloaded** (`Content-Disposition: attachment`, a cleaned file name, `Content-Security-Policy: sandbox`), never displayed inline. The file type comes from the bytes checked at intake, never from the file name.

## Pages

- **Templates escape everything** (Jinja2 autoescaping): a supplier name such as `<script>…</script>` is shown as text.
- **Response headers:**
  - `Content-Security-Policy: default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'`. There is no inline script or style at all.
  - `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: same-origin` (not `no-referrer`: with it, browsers send `Origin: null` on form posts, which the same-origin check refuses), a restrictive `Permissions-Policy`, `Cross-Origin-Opener-Policy` and `Cross-Origin-Resource-Policy: same-origin`.
  - `Cache-Control: no-store`, so no page or document image stays in a shared cache.
  - HSTS is added by the reverse proxy.
- **Small surface:** no API documentation, and none of the machine API that n8n uses. The page runs in its own container (read-only file system, no Linux capabilities, `no-new-privileges`), without the API token or the OCR key.

## Access by scope

- **One user, one scope.** Each account works on one scope: `live`, `e2e` or `demo`. A `demo` account always has the `demo` scope, and a database constraint enforces it.
- **Every read and decision is filtered by that scope.** This covers the list, the detail, the page images, the download and the four actions.
- **Outside your scope, an item doesn't exist.** It answers 404, exactly like an unknown item, so its existence isn't revealed.
- **Per-visitor ownership** (a demo visitor sees only their own uploads) will narrow this further when the public demo opens. It goes in the same filter.

## Audit trail

- **Decisions:** each decision and correction records the reviewer **from the session**, as "Name (review user N)". It is never a name typed into a form; a `reviewer` field sent with a form is ignored. The machine API keeps an explicit reviewer field for automated runs, which name themselves (e.g. "claude-code (test run, authorised by …)").
- **Sign-ins:** sign-in outcomes are in `login_attempts`, and decisions are in `processing_log` (step `review`).

## Known limits and next steps

- **No two-factor sign-in yet.** TOTP for reviewer and admin accounts is on the roadmap. Until then, these accounts can't sign in from the internet at all (see the listeners above).
- **Shared database role.** The page uses the same database role as the core service. A dedicated role with only the grants the page needs is planned before the public demo opens.
- **Proxy-side settings.** HSTS and the per-address request limit in front are set in the reverse proxy configuration when the site is deployed.
