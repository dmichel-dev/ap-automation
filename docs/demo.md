# Public demo

A visitor tries the whole pipeline at **demo.davidmichel.dev**: one click starts a session, then they send invoices to it and watch the results. No account, no sign-in, nothing to type. The demo runs on the same core, checks and workflows as the live system, inside its own scope (`demo`), and nothing from it reaches QuickBooks.

## What a visitor can do

| Way in | How | OCR cost |
|---|---|---|
| **Sample** | Click one of 7 fictional invoices: French (discount), German (total doesn't add up), Spanish (ambiguous date), Arabic (AED), Hindi (INR), and two hosting invoices from the same supplier, the second paid to a different bank account | None: each sample has its stored OCR result |
| **Upload** | A PDF or image of their own, up to 5 MB and 3 pages | Mistral OCR |
| **Email** | To `invoices+CODE@davidmichel.dev`, where CODE is the session's 8-letter code shown on the page | Mistral OCR |

The session page shows each file's outcome in the review page's words: **ready to post** (with the QuickBooks bill it would create: supplier, account, lines, tax, total), or **Review:** and the reason (total doesn't add up, date reads two ways, bank details changed?, new supplier…), or **rejected** and why. The confidence shown is the real one the core routes on, as a whole percentage, with an info button explaining it (see [review](review.md)). The samples always give the outcome they describe: the demo knows their suppliers and default accounts itself ([`demo_samples.py`](../services/core/app/demo_samples.py)), so live data never changes a demo result.

## How a file goes through

```
demo page ──► demo_uploads (queued, bytes in Postgres) ──► webhook ──► AP – Demo processing ──► core: POST /demo/uploads/{id}/process
 (demo-web)                                                (internal)        (n8n)                 (same processing as every channel, scope demo)

email to invoices+CODE@ ──► AP – Email intake ──► core screening: Delivered-To names a live session? ──► processing in that session
```

- The **demo pages** (`demo-web`, [`web/demo_site.py`](../services/core/app/web/demo_site.py)) only queue files. They call n8n's webhook `ap-demo-upload` with the file's **ID only**, on the Docker network (never through the proxy), with the header `X-Demo-Key`. The key only lets a caller ask for a queued file to be processed now; the core refuses any file that isn't waiting.
- A file still queued a minute later (the webhook call was missed) is picked up by the clean-up's sweep, one run per file.
- **Email:** the session code is read from `Delivered-To` only, which Google sets, never from `To`. An email to an unknown or ended session, over its sender's daily limit, or while the demo is paused is **refused**: logged, and deleted at once.
- Every comparison with earlier documents stays **within the session**: duplicate file, possible duplicate, bank-detail change (the November hosting sample raises the fraud alert only after October was sent in the same session).
- A ready demo invoice is never posted. The core computes the QuickBooks bill it would post and keeps it for the page (`demo_bill_previews`).
- A **needs-review** demo invoice opens in the real review page's design, **read-only** (`/s/CODE/review/ID`): the same content as the reviewer's page (`review_item.html`, shaped by `review.page_data`), with every control switched off and a note saying so. No form, no token, no write route. Only the visitor's own session (cookie and code), only its open needs-review invoices. The original is drawn from the demo's own sample PDFs; for a visitor's own file the panel says it isn't shown, and its page images are a 404. The data comes from the views `demo_review_items`, `demo_review_lines` and `demo_review_tax_lines` (migrations 021 and 022): no bank account details, no file path, OCR text or extraction. The supplier's default account it names is the demo's own, from its sample list (a supplier it doesn't know has none), never a live default. The page leaves out the review number, which counts every review on the server and means nothing to a visitor.

## Limits

All in `.env` (`DEMO_*`, see `.env.example`); the defaults:

| Limit | Default |
|---|---|
| Session length | 60 minutes |
| Sessions per address | 3 per hour, 10 per day |
| Sessions active at once | 30 |
| Files per session (samples, uploads and emails together) | 5 |
| File size, pages | 5 MB, 3 pages |
| OCR pages for visitors' own files | 200 per day, 2,000 per month (reserved before the call, given back if it fails) |
| Emails per sender | 10 per day |
| Kill switch | `DEMO_ENABLED=false`: the pages say the demo is paused, and demo emails are refused |

Addresses and senders are stored as keyed hashes (`DEMO_HASH_KEY`), never as written, and deleted after 2 days. Behind Caddy, only Caddy's `X-Forwarded-For` names the client (`DEMO_TRUSTED_PROXIES`, Caddy's fixed address); a forged header from a visitor is ignored. The Mistral account is prepaid with automatic top-up off, so its balance is a hard cap on top of the page budget.

## Security

- **A session needs its cookie and its code.** The browser that starts a session gets a random token (HttpOnly, Secure, SameSite=Strict; only its SHA-256 is stored). The code alone, from the email address or the URL, never opens the page.
- **Least privilege:** `demo-web` connects as `ap_demo` ([`db/init/02_demo_role.sh`](../db/init/02_demo_role.sh)): it can start sessions, queue files without reading them back, read results through the view `demo_invoice_results` (demo rows only, no OCR text, no extraction, no bank details), read bill previews and the review page's views (needs-review demo invoices, no bank details) and append to the log. A test applies the script's grants to a temporary role and checks that it reads those views but not the invoices table. Nothing of the live data. It has no file store.
- **Nothing is served back:** no uploaded or emailed file is ever shown or downloadable.
- **Only the demo's paths are public.** Caddy forwards `/`, `/start`, `/privacy`, `/credits`, `/s/*` and `/static/*` to `demo-web` (a test checks that every page route is covered and `/health` is not); every other path (its `/health` included) is an empty 404, and bodies over 6 MB are refused ([`deploy/Caddyfile`](../deploy/Caddyfile)). Nothing of n8n is public.
- Every page sends a strict CSP and the security headers, and ends with the copyright footer. Cross-site form posts are refused.
- **The AI only reads the document.** The demo's outcomes come from the same deterministic checks as the live system.

## What is deleted, and when

Matching the [privacy notice](../services/core/app/web/templates/demo_privacy.html) (`/privacy`):

| What | When |
|---|---|
| The session, its invoices, stored files, uploads and bill previews | Within 5 minutes of its end (`POST /demo/cleanup`, every 5 minutes) |
| Its emails | Then, **permanently** (Gmail API `messages.delete`, never the trash) |
| A refused email | At once (the email intake deletes it; the clean-up's list is the safety net) |
| The n8n run records that handled its items | When it ends (n8n API, which removes their stored files too). A run shared by two sessions goes with the first that ends |
| An emailed file in n8n's temporary storage | Within 90 minutes (cron, [`scripts/purge_n8n_temp_files.sh`](../scripts/purge_n8n_temp_files.sh)) |
| Address and sender hashes | After 2 days |
| Nightly backups (may hold a session open at 03:20 UTC) | After 14 days; the host's 24-hour copy of the whole server can keep one up to 15 days in total |

The core decides what each delete answer means: 200, 204 or 404 (already gone) takes the item off the list; anything else is logged and tried again 5 minutes later. The processing log keeps dates, statuses and counts, never a document's content.

## Running it

- **Switch:** `DEMO_ENABLED=true|false` in `.env`, then `docker compose up -d core demo-web`.
- **n8n credentials** (by these names, created in the editor): `Demo webhook key` (Header Auth: name `X-Demo-Key`, value `DEMO_WEBHOOK_KEY` from `.env`) and `n8n API (this instance)` (an API key from *Settings → n8n API*, base URL `http://localhost:5678/api/v1`).
- **Workflows:** `apDemoProcess001` (published before `apDemoCleanup001`, which calls it) and `apDemoCleanup001`. The clean-up's successful runs are not saved (288 a day, IDs only).
- **n8n runs at most 4 executions at once** (`N8N_CONCURRENCY_PRODUCTION_LIMIT`): a burst of demo files waits as "queued" on the page.

## Verified on the server (2026-11-26)

| Check | Result |
|---|---|
| HTTPS and paths | Certificate issued; the demo's paths answer, `/health`, `/docs`, `/openapi.json` and `/.env` an empty 404 |
| Address limit behind Caddy | Only the visitor's real address counted; a forged `X-Forwarded-For` and Caddy's own address ignored |
| Samples | All 7 gave the outcome they describe, the hosting fraud alert included |
| Upload | A fictional invoice of an unknown supplier: `needs_review` (new supplier) |
| Webhook | The demo pages' key accepted, a wrong key refused (403); a file that isn't waiting refused by the core (409) |
| Email | Sent to a live session: processed in it. Sent to a made-up code: refused and deleted at once |
| Session end | Sessions, invoices and the 10 n8n run records of 3 test sessions deleted by the clean-up; both emails answer 404 in Gmail (deleted, not in the trash) |
| Live pipeline | The daily end-to-end test passed (40 checks) after the deploy |
| Kill switch | `DEMO_ENABLED=false`: the page says the demo is paused |

## Known limits

- A run that fails before it reaches a session (the core down during the email screening) can't be tied to it: it stays in n8n's history up to 14 days, without its file (deleted within 90 minutes).
- A file whose processing call is lost after the core claimed it stays "processing" on the page until the session ends.
- After 90 minutes, the n8n editor can't show an old run's attachment (live runs too). The core keeps its own copy of every processed file.
- A visitor's own file costs OCR; samples don't. Mistral keeps API inputs and outputs for 30 days for abuse monitoring (zero data retention is not active), as the privacy notice says.
