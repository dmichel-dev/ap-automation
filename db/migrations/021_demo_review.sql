-- 021: what the demo's read-only review page may read. demo-web shows a needs-review demo invoice in
-- the review page's design, through these views only (db/init/02_demo_role.sh grants them):
--   - demo rows only, needs-review invoices only, the open review item only;
--   - the fields the review page shows, read from the visitor's own (fictional) document;
--   - no bank details: no bank_accounts column, and the bank-details check is cut down to whether it
--     passed and whether the supplier is new (the masked account lists are left out);
--   - no file path, OCR text, extraction, buyer, reviewer, corrections or decision.
-- Each view runs with its owner's rights, so the demo role needs no right on the tables.
SET search_path = ap;

CREATE VIEW demo_review_items WITH (security_barrier) AS
SELECT r.id AS review_id, r.stage AS review_stage, r.status AS review_status, r.reasons AS review_reasons,
       r.created_at AS review_created_at,
       i.id, i.demo_session_id, i.status, i.document_type, i.supplier_name, i.supplier_tax_id,
       i.supplier_country, i.invoice_number, i.related_invoice_number, i.invoice_date, i.due_date,
       i.service_period_start, i.service_period_end, i.invoice_date_raw, i.due_date_raw, i.dates_ambiguous,
       i.date_order_suggested, i.invoice_date_suggested, i.due_date_suggested, i.date_suggestion_basis,
       i.currency, i.subtotal, i.tax_total, i.total, i.tax_mode, i.confidence, i.ocr_confidence_avg,
       i.confidence_completeness, i.language, i.received_at, i.channel, i.expense_account_id,
       i.expense_account_source,
       CASE WHEN i.checks ? 'bank_details'
            THEN (i.checks - 'bank_details') || jsonb_build_object('bank_details', jsonb_build_object(
                   'passed', i.checks -> 'bank_details' -> 'passed',
                   'first_seen', i.checks -> 'bank_details' -> 'first_seen'))
            ELSE i.checks END AS checks
  FROM review_queue r JOIN invoices i ON i.id = r.invoice_id
 WHERE r.scope = 'demo' AND i.scope = 'demo' AND i.status = 'needs_review' AND r.status = 'open';

CREATE VIEW demo_review_lines WITH (security_barrier) AS
SELECT l.invoice_id, l.line_no, l.kind, l.tax_stage, l.description, l.quantity, l.unit_price, l.amount, l.tax_rate
  FROM invoice_lines l JOIN invoices i ON i.id = l.invoice_id
 WHERE i.scope = 'demo' AND i.status = 'needs_review';

CREATE VIEW demo_review_tax_lines WITH (security_barrier) AS
SELECT t.invoice_id, t.id, t.tax_type, t.label, t.rate, t.taxable_base, t.amount
  FROM invoice_tax_lines t JOIN invoices i ON i.id = t.invoice_id
 WHERE i.scope = 'demo' AND i.status = 'needs_review';
