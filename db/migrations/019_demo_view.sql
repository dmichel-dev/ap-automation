-- 019: what the public demo pages may read of invoices. The pages connect with their own role
-- (db/init/02_demo_role.sh), which reads invoices only through this view: demo rows only, results
-- only (no OCR text, no extraction, no bank details). A view runs with its owner's rights, so the
-- role needs no right on the invoices table itself (except the two columns the log insert reads).
SET search_path = ap;

CREATE VIEW demo_invoice_results WITH (security_barrier) AS
SELECT i.id, i.demo_session_id, i.file_name, i.channel, i.status, i.status_reasons, i.supplier_name,
       i.invoice_number, i.document_type, i.total, i.currency, i.confidence, i.created_at,
       (SELECT count(*) FROM invoice_lines l WHERE l.invoice_id = i.id) AS lines
  FROM invoices i
 WHERE i.scope = 'demo';
