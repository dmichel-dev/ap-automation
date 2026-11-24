-- 016: foreign-currency bank matching. A bill in another currency than the bank account is compared
-- with the bank amount through the ECB reference rate of the transaction date (Frankfurter API, fetched
-- by n8n and stored here). The bill payment then carries the actual rate (bank amount ÷ bill amount),
-- and QuickBooks posts the exchange gain or loss itself.
SET search_path = ap;

-- ---------------------------------------------------------------------------
-- fx_rates: daily reference rates, 1 base = rate quote (e.g. 1 EUR = 1.0939 USD on 2026-11-23).
-- Only business days are published; a lookup takes the latest rate on or before a date.
-- ---------------------------------------------------------------------------
CREATE TABLE fx_rates (
  provider    text NOT NULL CHECK (provider IN ('ecb')),
  base        char(3) NOT NULL CHECK (base ~ '^[A-Z]{3}$'),
  quote       char(3) NOT NULL CHECK (quote ~ '^[A-Z]{3}$' AND quote <> base),
  rate_date   date NOT NULL,
  rate        numeric(20,10) NOT NULL CHECK (rate > 0),
  fetched_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (provider, base, quote, rate_date)
);

-- ---------------------------------------------------------------------------
-- fx_unsupported: currencies the provider said it does not quote (not a network failure). A later
-- answer with rates removes the row.
-- ---------------------------------------------------------------------------
CREATE TABLE fx_unsupported (
  provider    text NOT NULL CHECK (provider IN ('ecb')),
  base        char(3) NOT NULL CHECK (base ~ '^[A-Z]{3}$'),
  quote       char(3) NOT NULL CHECK (quote ~ '^[A-Z]{3}$'),
  message     text NOT NULL,
  checked_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (provider, base, quote)
);

-- ---------------------------------------------------------------------------
-- matches: the bank side can be in another currency than the bill.
--   bank_currency          currency of bank_amount (the bank account's)
--   fx_rate, fx_rate_date  reference rate used to compare (NULL when both sides share the currency,
--                          or when no usable rate exists: currency not quoted, or rate too old)
--   expected_bank_amount   bill amount × fx_rate, rounded to the bank currency
--   difference             bank_amount − invoice_amount (same currency), bank_amount − expected_bank_amount
--                          (FX), or NULL when no usable rate exists (nothing to compare with)
-- ---------------------------------------------------------------------------
ALTER TABLE matches
  ADD COLUMN bank_currency char(3),
  ADD COLUMN fx_rate numeric(20,10) CHECK (fx_rate > 0),
  ADD COLUMN fx_rate_date date,
  ADD COLUMN expected_bank_amount numeric(18,4) CHECK (expected_bank_amount > 0);
UPDATE matches SET bank_currency = currency;
ALTER TABLE matches
  ALTER COLUMN bank_currency SET NOT NULL,
  ALTER COLUMN difference DROP NOT NULL,
  ADD CONSTRAINT matches_fx_consistent CHECK (
       (bank_currency = currency  AND fx_rate IS NULL AND fx_rate_date IS NULL AND expected_bank_amount IS NULL
                                  AND difference IS NOT NULL)
    OR (bank_currency <> currency AND (fx_rate IS NULL) = (fx_rate_date IS NULL)
                                  AND (fx_rate IS NULL) = (expected_bank_amount IS NULL)
                                  AND (fx_rate IS NULL) = (difference IS NULL))
  );

-- ---------------------------------------------------------------------------
-- Payment reasons: fx_match_pending (bills in another currency were not compared) is replaced by the
-- FX outcomes: no usable rate, or a bank amount too far from the converted bill.
-- ---------------------------------------------------------------------------
ALTER TABLE accounting_postings DROP CONSTRAINT postings_payment_consistent;
UPDATE accounting_postings SET payment_reason = 'no_match_yet' WHERE payment_reason = 'fx_match_pending';
ALTER TABLE accounting_postings DROP CONSTRAINT accounting_postings_payment_reason_check;
ALTER TABLE accounting_postings ADD CONSTRAINT accounting_postings_payment_reason_check
  CHECK (payment_reason IN ('no_match_yet', 'ambiguous_match', 'fx_rate_unavailable', 'fx_amount_difference',
                            'payment_pending', 'payment_difference'));
ALTER TABLE accounting_postings ADD CONSTRAINT postings_payment_consistent CHECK (
     (payment_status = 'unpaid'               AND payment_reason IN ('no_match_yet', 'ambiguous_match',
                                                  'fx_rate_unavailable', 'fx_amount_difference', 'payment_pending'))
  OR (payment_status = 'paid'                 AND payment_reason IS NULL AND amount_paid = amount)
  OR (payment_status = 'paid_with_difference' AND payment_reason = 'payment_difference' AND amount_paid > 0)
  OR (payment_status = 'paid_with_write_off'  AND payment_reason IS NULL AND amount_paid > 0
                                              AND amount_paid + amount_written_off = amount)
  OR (payment_status = 'not_applicable'       AND payment_reason IS NULL AND object_type = 'vendor_credit')
);
