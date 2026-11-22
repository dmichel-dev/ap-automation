-- 003: validation results and review-friendly constraints.
--  * Wrong sign and due-before-issue go to human review instead of being blocked at insert,
--    so these two rules now apply to 'ready' invoices only.
--  * Line kinds (discount, shipping, fee, rounding) and their position relative to tax.
--  * Confidence parts stored separately for reviewers (score = OCR × completeness).

SET search_path = ap;

ALTER TABLE invoices
  DROP CONSTRAINT invoices_total_sign,
  ADD CONSTRAINT invoices_total_sign CHECK (
    status <> 'ready' OR total IS NULL
    OR (document_type = 'invoice'     AND total >= 0)
    OR (document_type = 'credit_note' AND total <= 0)
  ),
  DROP CONSTRAINT invoices_due_after_issue,
  ADD CONSTRAINT invoices_due_after_issue CHECK (
    status <> 'ready' OR due_date IS NULL OR invoice_date IS NULL OR due_date >= invoice_date
  ),
  ADD COLUMN confidence_completeness numeric(5,4) CHECK (confidence_completeness BETWEEN 0 AND 1),
  ADD COLUMN confidence_threshold    numeric(4,3) CHECK (confidence_threshold BETWEEN 0 AND 1),
  ADD COLUMN tax_mode                text CHECK (tax_mode IN ('exclusive', 'inclusive')),
  ADD COLUMN checks                  jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(checks) = 'object'),
  ADD COLUMN duplicate_of            uuid REFERENCES invoices (id) ON DELETE SET NULL;

COMMENT ON COLUMN invoices.confidence IS 'ocr_confidence_avg × confidence_completeness';

-- Unreadable amounts are stored as NULL and sent to review
ALTER TABLE invoice_lines
  ALTER COLUMN amount DROP NOT NULL,
  ADD COLUMN kind      text NOT NULL DEFAULT 'item'
    CHECK (kind IN ('item', 'discount', 'shipping', 'fee', 'rounding', 'other')),
  ADD COLUMN tax_stage text NOT NULL DEFAULT 'before_tax'
    CHECK (tax_stage IN ('before_tax', 'after_tax'));

ALTER TABLE invoice_tax_lines ALTER COLUMN amount DROP NOT NULL;
