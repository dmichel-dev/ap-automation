# Data model (`ap` schema)

Defined in [`db/migrations/`](../db/migrations). Migrations run as the `ap_app` role, which owns the `ap` schema and has no access to n8n's own tables.

| Table | Purpose |
|---|---|
| `invoices` | One row per received document (invoice or credit note): source, file, parties, amounts, extraction, status |
| `invoice_lines` | Line items |
| `invoice_tax_lines` | Generic tax breakdown: `vat`, `gst`, `sales_tax`, `withholding`, `other`, `none` |
| `review_queue` | Human review items (at most one open item per invoice), with the decision, the reviewer and every correction (before/after; see [review](review.md)); `stage` = `validation` (extraction/checks) `posting` (e.g. `vendor_currency_conflict`, `vendor_inactive`, `duplicate_in_accounting`) or `matching` (`ambiguous_match`: one bank transaction fits several bills, or one bill several transactions; the candidate transactions are named) |
| `processing_log` | Append-only audit trail; `invoice_id` is optional so events without an invoice are logged too |
| `vendor_links` | Supplier + currency → accounting vendor (QuickBooks fixes a vendor's currency, so one supplier can have one vendor per currency) |
| `accounting_postings` | One per invoice sent to QuickBooks: bill or vendor credit, rate used, attachment, payment status and reason; created **before** any API call |
| `bank_sync_state` | The bank account used for matching, its currency and the Plaid sync cursor (no access token) |
| `bank_transactions` | Plaid transactions as received; **positive amount = money out** |
| `matches` | Invoice ↔ bank transaction: `accepted` (used for a payment), `candidate` (ambiguous, for review), `dismissed` |
| `qbo_accounts`, `qbo_vendors` | Copy of the QuickBooks chart of accounts and vendor list, refreshed hourly by the sync workflow; a row missing from the latest sync is kept as inactive |
| `supplier_defaults` | Supplier → default account, set by a reviewer on the supplier's first confirmed invoice (`source` = `reviewer`, or `test_setup` for the e2e test suppliers) |

## Conventions

- **Amounts** are `numeric(18,4)` in the document currency. **Credit notes are signed** (`total <= 0`), even when the PDF prints positive amounts; invoices have `total >= 0`. Individual lines may be negative (discounts).
- **Service period** (`service_period_start` / `_end`): printed dates of the service or delivery the invoice covers, parsed like the other dates (raw text and suggested readings kept); used by the cut-off check.
- **Possible duplicates:** same supplier + document type + number, **or** same supplier + document type + total + currency, dated within 7 days, with another number.
- **Duplicate key:** `supplier_key` (normalized tax ID: printed labels such as `EIN`, `VAT`, `USt-IdNr.` removed, letters and digits only; otherwise the normalized name. The tax ID as printed is kept in `supplier_tax_id`) + `document_type` + `invoice_number_norm`.
  Same `file_sha256` → `rejected`; same key with a different file → `needs_review`.
- **Idempotency:** `(channel, source_ref)` is unique, so re-running an intake never creates a second row for the same attachment.
- **Codes:** `currency` is ISO 4217, `supplier_country` is ISO 3166-1 alpha-2, `language` is BCP 47.
- **Expense account:** `expense_account_id` + `expense_account_source` (`supplier_default` or `reviewer`) record the account a ready invoice is posted to, and why. Posting refuses an invoice without one.
- **Scope:** `invoices`, `review_queue` and `processing_log` have `scope` = `live`, `e2e` or `demo`, so test and demo data stay out of live summaries, alerts and the review list.
- **Database safeguards:** a `ready` invoice must have every required field, including a resolved invoice date. `dates_ambiguous` records what was printed (a reviewer may settle it). `processing_log` rejects UPDATE, DELETE and TRUNCATE.
- `processing_log` never stores document content (privacy): only IDs, steps, check names and short messages.
- **Posting safeguards:** one posting per invoice; a `posted` row always has the external ID, vendor and exchange rate used; payment status and reason must agree (e.g. `paid_with_difference` ⇔ `payment_difference`).
- **Matching safeguards:** a bank transaction pays at most one invoice and an invoice is paid by at most one transaction (partial unique indexes on accepted matches), so even a buggy or concurrent run can't pay twice.
- `invoices.posting_excluded_reason` keeps an invoice out of posting (e.g. superseded test data); it can't be set once the invoice is posted.

## Original file retention

Original files are needed until they're attached to the QuickBooks bill, then deleted.

- **Storage:** Docker volume `ap_files`, mounted **only in the core service** at `/data/files`. `invoices.file_path` is relative to that root.
  n8n gets a file through the core API (`GET /invoices/{id}/file`), so there is no shared volume.
- **Deletion triggers:**
  1. **After posting**: when n8n reports the attachment ID to the core service (`POST /postings/{id}/result`), the core deletes the file (`reason = posted`), after the database commit.
  2. **Retention limit** (Day 3): a daily n8n schedule calls a purge endpoint that deletes files whose `file_delete_after` has passed (`reason = retention`). `file_delete_after = received_at + FILE_RETENTION_DAYS` (default 30; shorter for the public demo).
  3. **Rejected by a reviewer**: deleted right after the decision (`reason = rejected`). Documents rejected automatically are never stored.
- **After deletion:** `file_path` is cleared, `file_deleted_at` and `file_deletion_reason` are set, and the deletion is logged. `file_sha256` stays, so a re-sent file is still detected as a duplicate.
- **To decide on Day 6:** for the public demo, whether `ocr_text` and `extraction` should also be cleared at retention time.
