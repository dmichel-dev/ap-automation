-- 002: printed dates, suggested dates for ambiguous ones, OCR confidence.
-- When a date is ambiguous (e.g. 03/04/2026), invoice_date/due_date stay NULL and the
-- invoice goes to review; the suggestion (from the supplier country) lets the reviewer
-- confirm with one click.

SET search_path = ap;

ALTER TABLE invoices
  ADD COLUMN invoice_date_raw        text,
  ADD COLUMN due_date_raw            text,
  ADD COLUMN date_order_suggested    text CHECK (date_order_suggested IN ('DMY', 'MDY')),
  ADD COLUMN invoice_date_suggested  date,
  ADD COLUMN due_date_suggested      date,
  ADD COLUMN date_suggestion_basis   text,
  ADD COLUMN ocr_confidence_avg      numeric(5,4) CHECK (ocr_confidence_avg BETWEEN 0 AND 1),
  ADD COLUMN ocr_confidence_min      numeric(5,4) CHECK (ocr_confidence_min BETWEEN 0 AND 1),
  ADD CONSTRAINT invoices_suggestion_only_if_ambiguous CHECK (
    dates_ambiguous OR (date_order_suggested IS NULL AND invoice_date_suggested IS NULL AND due_date_suggested IS NULL)
  );
