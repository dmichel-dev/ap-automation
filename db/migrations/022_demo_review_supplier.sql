-- 022: the demo's read-only review page names the supplier's default account, as the reviewer's page
-- does. A demo supplier's default comes from the demo's own sample list (demo_samples.py), never from
-- the live supplier defaults, so the view only adds the key that list is looked up by. supplier_key is
-- derived from the supplier's tax ID or name, both already in this view.
-- CREATE OR REPLACE keeps the view's grants (db/init/02_demo_role.sh); the new column goes last.
SET search_path = ap;

CREATE OR REPLACE VIEW demo_review_items WITH (security_barrier) AS
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
            ELSE i.checks END AS checks,
       i.supplier_key
  FROM review_queue r JOIN invoices i ON i.id = r.invoice_id
 WHERE r.scope = 'demo' AND i.scope = 'demo' AND i.status = 'needs_review' AND r.status = 'open';
