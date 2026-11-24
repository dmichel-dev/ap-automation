-- 013: bank details read from the invoice, for the possible-change check (bank_details.py).
-- Stored MASKED only (last 4 characters of each number), with a keyed fingerprint (HMAC-SHA256, key
-- BANK_FINGERPRINT_KEY) to compare an invoice's accounts with the supplier's earlier invoices.
-- The full numbers are never stored here: the only full copies are the document and its OCR text.
SET search_path = ap;

ALTER TABLE invoices ADD COLUMN bank_accounts jsonb NOT NULL DEFAULT '[]'::jsonb
  CHECK (jsonb_typeof(bank_accounts) = 'array');

COMMENT ON COLUMN invoices.bank_accounts IS
  'Accounts printed on the document, never trusted: [{iban, bic, account_number, routing_code} masked to the '
  'last 4 characters, account_holder, label, fingerprint}]. Full numbers are never stored.';
