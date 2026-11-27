-- 023: the demo's session page recognises a sample whatever channel brought it. A sample sent by email
-- has no upload row naming it, so the results view adds the file's SHA-256: the page looks it up in the
-- demo's own sample list (demo_samples.py), as processing does to use the sample's stored OCR result.
-- Demo rows only, as before; the hash of a visitor's own file says nothing about its content.
-- CREATE OR REPLACE keeps the view's grants (db/init/02_demo_role.sh); the new column goes last.
SET search_path = ap;

CREATE OR REPLACE VIEW demo_invoice_results WITH (security_barrier) AS
SELECT i.id, i.demo_session_id, i.file_name, i.channel, i.status, i.status_reasons, i.supplier_name,
       i.invoice_number, i.document_type, i.total, i.currency, i.confidence, i.created_at,
       (SELECT count(*) FROM invoice_lines l WHERE l.invoice_id = i.id) AS lines,
       i.file_sha256
  FROM invoices i
 WHERE i.scope = 'demo';
