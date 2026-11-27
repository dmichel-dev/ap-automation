# Data model (`ap` schema)

Defined in [`db/migrations/`](../db/migrations). Migrations run as the `ap_app` role, which owns the `ap` schema and has no access to n8n's own tables.

| Table | Purpose |
|---|---|
| `invoices` | One row per received document (invoice or credit note): source, file, parties, amounts, extraction, status. `bank_accounts`: the accounts printed on it, **masked** (last 4 characters) with a keyed fingerprint to compare them, never the full numbers (see [security](security.md#bank-details)) |
| `invoice_lines` | Line items |
| `invoice_tax_lines` | Generic tax breakdown: `vat`, `gst`, `sales_tax`, `withholding`, `other`, `none` |
| `review_queue` | Human review items (at most one open item per invoice), with the decision, the reviewer and every correction (before/after; see [review](review.md)); `stage` = `validation` (extraction/checks) `posting` (e.g. `vendor_currency_conflict`, `vendor_inactive`, `duplicate_in_accounting`) or `matching` (`ambiguous_match`: one bank transaction fits several bills, or one bill several transactions; the candidate transactions are named. `payment_difference`: the bank paid less than the bill). A decision that needs QuickBooks keeps the item open with `waiting_for` (`posting`, `payment`, `write_off`) until QuickBooks confirms it; `effect_error` holds the last QuickBooks failure |
| `processing_log` | Append-only audit trail; `invoice_id` is optional so events without an invoice are logged too |
| `vendor_links` | Supplier + currency → accounting vendor (QuickBooks fixes a vendor's currency, so one supplier can have one vendor per currency) |
| `accounting_postings` | One per invoice sent to QuickBooks: bill or vendor credit, rate used, attachment, payment status and reason; created **before** any API call |
| `bank_sync_state` | The bank account used for matching, its currency and the Plaid sync cursor (no access token) |
| `bank_transactions` | Plaid transactions as received; **positive amount = money out** |
| `matches` | Invoice ↔ bank transaction: `accepted` (used for a payment), `candidate` (ambiguous, for review), `dismissed` (never proposed again for that bill) |
| `write_offs` | The open part of an under-paid bill, written off by a reviewer: amount, who decided, the vendor credit and the bill payment applying it in QuickBooks (`AP-WO-<id>`), attempts and the last error. `recorded` only with both QuickBooks IDs |
| `intake_failures` | One per email or Drive file that could not be processed: `waiting` (the re-check retries it, with backoff), `given_up` (a person looks) or `resolved`; attempts, next attempt, last error code and stage, whether it is permanent. See [operations](operations.md#errors-and-retries) |
| `intake_leases` | Drive files handed out for processing (15-minute lease), so overlapping runs don't send a file to the OCR service twice. See [operations](operations.md#google-drive-intake) |
| `run_requests` | Every "Run now" request, allowed or refused: target, outcome, time and, for a request that ran, the SHA-256 of its signature (a signature is accepted once). Never the token |
| `qbo_accounts`, `qbo_vendors` | Copy of the QuickBooks chart of accounts and vendor list, refreshed hourly by the sync workflow; a row missing from the latest sync is kept as inactive |
| `supplier_defaults` | Supplier → default account, set by a reviewer on the supplier's first confirmed invoice (`source` = `reviewer`, or `test_setup` for the e2e test suppliers) |
| `review_users` | Review-page accounts: email, display name, Argon2id password hash, `role` (`reviewer`, `admin`, `demo`), `scope` (a `demo` account always has the `demo` scope), lockout state. Created with the CLI only (see [security](security.md)) |
| `review_sessions` | Server-side sessions: SHA-256 of the cookie token (never the token), the listener it belongs to (`internal` or `public`), its CSRF token, idle and absolute expiry, the per-minute decision count |
| `demo_sessions` | Public demo sessions: the 8-letter code (email address and page URL), SHA-256 of the browser's token (never the token), a keyed hash of the client address, expiry, files used. Deleted when they end, with everything below that names them (see [demo](demo.md)) |
| `demo_starts`, `demo_senders` | Session starts per client address and demo emails per sender and day, for the limits: keyed hashes only, kept 2 days |
| `demo_budget` | OCR pages spent on visitors' own files, per day and per month |
| `demo_uploads` | Samples and uploads waiting for processing: the bytes until processed (then emptied), status, the invoice it became, or the error |
| `demo_bill_previews` | The bill a ready demo invoice would post (demo invoices are never sent to QuickBooks) |
| `demo_external_items` | What a session left outside the database: its Gmail messages, Drive files and the n8n runs that handled its items |
| `demo_deletions` | Those still to be deleted permanently in Gmail, Drive or n8n (outlives the session; a refused email goes here at once) |
| `demo_invoice_results` (view) | What the demo pages may read of invoices: demo rows and results only, no OCR text, extraction or bank details |
| `login_attempts` | Sign-in attempts for the per-address limit and the audit: address, listener, outcome and a hash of the typed email (never the email or a password); kept 30 days |

## Conventions

- **Amounts** are `numeric(18,4)` in the document currency. **Credit notes are signed** (`total <= 0`), even when the PDF prints positive amounts; invoices have `total >= 0`. Individual lines may be negative (discounts).
- **Service period** (`service_period_start` / `_end`): printed dates of the service or delivery the invoice covers, parsed like the other dates (raw text and suggested readings kept); used by the cut-off check.
- **Possible duplicates:** same supplier + document type + number, **or** same supplier + document type + total + currency, dated within 7 days, with another number.
- **Duplicate key:** `supplier_key` (normalized tax ID: printed labels such as `EIN`, `VAT`, `USt-IdNr.` removed, letters and digits only; otherwise the normalized name. The tax ID as printed is kept in `supplier_tax_id`) + `document_type` + `invoice_number_norm`.
  Same `file_sha256` → `rejected`; same key with a different file → `needs_review`.
- **Idempotency:** `(channel, source_ref)` is unique, so re-running an intake never creates a second row for the same attachment.
- **Codes:** `currency` is ISO 4217, `supplier_country` is ISO 3166-1 alpha-2, `language` is BCP 47.
- **Expense account:** `expense_account_id` + `expense_account_source` (`supplier_default` or `reviewer`) record the account a ready invoice is posted to, and why. Posting refuses an invoice without one.
- **Scope:** `invoices`, `review_queue`, `processing_log` and `intake_failures` have `scope` = `live`, `e2e` or `demo`, so test and demo data stay out of live summaries, alerts and the review list. A review item and a log entry take their invoice's scope. Duplicate checks and the bank-details baseline compare only within one scope, and a demo invoice (with its `demo_session_id`, required for scope `demo` only) only within its session.
- **Daily end-to-end test:** `e2e_runs` (one row per run: status, checks passed, failures) and `e2e_qbo_objects` (the QuickBooks bills and attachments its postings created, with their state: `pending`, `checked`, `deleted`, `gone` or `refused`). See [operations](operations.md#daily-end-to-end-test).
- **Database safeguards:** a `ready` invoice must have every required field, including a resolved invoice date. `dates_ambiguous` records what was printed (a reviewer may settle it). `processing_log` rejects UPDATE, DELETE and TRUNCATE.
- `processing_log` never stores document content (privacy): only IDs, steps, check names and short messages.
- **Posting safeguards:** one posting per invoice; a `posted` row always has the external ID, vendor and exchange rate used; payment status and reason must agree (e.g. `paid_with_difference` ⇔ `payment_difference`; `paid_with_write_off` only when the amount paid plus the amount written off equals the bill).
- **Matching safeguards:** a bank transaction pays at most one invoice and an invoice is paid by at most one transaction (partial unique indexes on accepted matches), so even a buggy or concurrent run can't pay twice.
- `invoices.posting_excluded_reason` keeps an invoice out of posting (e.g. superseded test data); it can't be set once the invoice is posted.

## Original file retention

Original files are needed until they're attached to the QuickBooks bill, then deleted.

- **Storage:** Docker volume `ap_files`, mounted **only in the core service** at `/data/files`. `invoices.file_path` is relative to that root.
  n8n gets a file through the core API (`GET /invoices/{id}/file`), so there is no shared volume.
- **Deletion triggers:**
  1. **After posting**: when n8n reports the attachment ID to the core service (`POST /postings/{id}/result`), the core deletes the file (`reason = posted`), after the database commit.
  2. **Retention limit** (planned, not built yet): a daily n8n schedule will call a purge endpoint that deletes files whose `file_delete_after` has passed (`reason = retention`). `file_delete_after = received_at + FILE_RETENTION_DAYS` (default 30; shorter for the public demo).
  3. **Rejected by a reviewer**: deleted right after the decision (`reason = rejected`). Documents rejected automatically are never stored.
- **After deletion:** `file_path` is cleared, `file_deleted_at` and `file_deletion_reason` are set, and the deletion is logged. `file_sha256` stays, so a re-sent file is still detected as a duplicate.
- **Open question:** whether `ocr_text` and `extraction` should also be cleared at retention time. The public demo already deletes a session's invoices entirely when it ends ([demo](demo.md#what-is-deleted-and-when)).
