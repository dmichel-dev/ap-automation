# Data model (`ap` schema)

Defined in [`db/migrations/`](../db/migrations). Migrations run as the `ap_app` role, which owns the `ap` schema and has no access to n8n's own tables.

| Table | Purpose |
|---|---|
| `invoices` | One row per received document (invoice or credit note): source, file, parties, amounts, extraction, status |
| `invoice_lines` | Line items |
| `invoice_tax_lines` | Generic tax breakdown: `vat`, `gst`, `sales_tax`, `withholding`, `other`, `none` |
| `review_queue` | Human review items (at most one open item per invoice) |
| `processing_log` | Append-only audit trail; `invoice_id` is optional so events without an invoice are logged too |

## Conventions

- **Amounts** are `numeric(18,4)` in the document currency. **Credit notes are signed** (`total <= 0`), even when the PDF prints positive amounts; invoices have `total >= 0`. Individual lines may be negative (discounts).
- **Duplicate key:** `supplier_key` (normalized tax ID, otherwise normalized name) + `document_type` + `invoice_number_norm`.
  Same `file_sha256` → `rejected`; same key with a different file → `needs_review`.
- **Idempotency:** `(channel, source_ref)` is unique, so re-running an intake never creates a second row for the same attachment.
- **Codes:** `currency` is ISO 4217, `supplier_country` is ISO 3166-1 alpha-2, `language` is BCP 47.
- **Database safeguards:** a `ready` invoice must have every required field and no ambiguous date. `processing_log` rejects UPDATE, DELETE and TRUNCATE.
- `processing_log` never stores document content (privacy): only IDs, steps, check names and short messages.

## Original file retention

Original files are needed until they're attached to the QuickBooks bill (Day 2), then deleted.

- **Storage:** Docker volume `ap_files`, mounted **only in the core service** at `/data/files`. `invoices.file_path` is relative to that root.
  n8n gets a file through the core API (`GET /invoices/{id}/file`), so there is no shared volume.
- **Deletion triggers:**
  1. **After posting** (Day 2): once the attachment is confirmed in QuickBooks, n8n calls the core service to delete the file (`reason = posted`).
  2. **Retention limit** (Day 3): a daily n8n schedule calls a purge endpoint that deletes files whose `file_delete_after` has passed (`reason = retention`). `file_delete_after = received_at + FILE_RETENTION_DAYS` (default 30; shorter for the public demo).
  3. Rejected documents follow the same retention limit (`reason = rejected`).
- **After deletion:** `file_path` is cleared, `file_deleted_at` and `file_deletion_reason` are set, and the deletion is logged. `file_sha256` stays, so a re-sent file is still detected as a duplicate.
- **To decide on Day 6:** for the public demo, whether `ocr_text` and `extraction` should also be cleared at retention time.
