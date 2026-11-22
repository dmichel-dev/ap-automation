-- 004: accounting postings (QuickBooks), bank transactions (Plaid) and matching.
-- Rules enforced here, not only in code:
--   * one posting per invoice; a posted row always has the external ID and the rate used
--   * a bank transaction pays at most one invoice, an invoice is paid by at most one
--     transaction (accepted matches only; ambiguous candidates are kept for review)
--   * bank amounts keep Plaid's sign: positive = money out of the account
--   * no secrets are stored: Plaid access tokens and QuickBooks tokens live in n8n credentials

SET search_path = ap;

-- Confidence with 4 decimals (0.9775 was stored as 0.978)
ALTER TABLE invoices ALTER COLUMN confidence TYPE numeric(5,4);

-- Invoices that must never be posted (e.g. superseded test data), with an audit trail
ALTER TABLE invoices
  ADD COLUMN posting_excluded_reason text CHECK (posting_excluded_reason IN ('superseded_test_data', 'manual')),
  ADD COLUMN posting_excluded_at     timestamptz,
  ADD CONSTRAINT invoices_posting_exclusion CHECK (
    (posting_excluded_reason IS NULL) = (posting_excluded_at IS NULL)
  );

-- ---------------------------------------------------------------------------
-- vendor_links: our supplier (+ currency) → the vendor in the accounting system.
-- QuickBooks fixes a vendor's currency at creation, so one supplier can have one
-- vendor per currency.
-- ---------------------------------------------------------------------------
CREATE TABLE vendor_links (
  id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  system         text NOT NULL DEFAULT 'qbo' CHECK (system IN ('qbo')),
  supplier_key   text NOT NULL,
  currency       char(3) NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
  external_id    text NOT NULL,
  display_name   text NOT NULL,
  created_at     timestamptz NOT NULL DEFAULT now(),
  UNIQUE (system, supplier_key, currency),
  UNIQUE (system, external_id)
);

-- ---------------------------------------------------------------------------
-- accounting_postings: one row per invoice sent to the accounting system.
-- The row is created (claimed) BEFORE any API call, so a retry can never post twice.
-- ---------------------------------------------------------------------------
CREATE TABLE accounting_postings (
  id                     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  invoice_id             uuid NOT NULL UNIQUE REFERENCES invoices (id) ON DELETE RESTRICT,
  system                 text NOT NULL DEFAULT 'qbo' CHECK (system IN ('qbo')),
  object_type            text NOT NULL CHECK (object_type IN ('bill', 'vendor_credit')),
  status                 text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'posted', 'failed')),
  attempts               integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  claimed_until          timestamptz,            -- lease: one workflow run at a time
  next_attempt_at        timestamptz NOT NULL DEFAULT now(),
  last_error             text,                   -- error code/message from the API, never document content

  currency               char(3) NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
  amount                 numeric(18,4) NOT NULL CHECK (amount >= 0),  -- bill / credit amount (0 only when it failed permanently)
  doc_number             text NOT NULL CHECK (char_length(doc_number) BETWEEN 1 AND 21),
  vendor_external_id     text,
  external_id            text,                   -- QuickBooks Bill or VendorCredit ID
  exchange_rate          numeric(18,8) CHECK (exchange_rate > 0),     -- foreign currency → home currency
  exchange_rate_date     date,
  exchange_rate_source   text CHECK (exchange_rate_source IN ('qbo', 'home_currency')),
  attachment_external_id text,
  posted_at              timestamptz,

  payment_status         text NOT NULL DEFAULT 'unpaid'
                         CHECK (payment_status IN ('unpaid', 'paid', 'paid_with_difference', 'not_applicable')),
  payment_reason         text DEFAULT 'no_match_yet' CHECK (payment_reason IN
                           ('no_match_yet', 'ambiguous_match', 'fx_match_pending', 'payment_pending', 'payment_difference')),
  amount_paid            numeric(18,4) NOT NULL DEFAULT 0,

  created_at             timestamptz NOT NULL DEFAULT now(),
  updated_at             timestamptz NOT NULL DEFAULT now(),

  CONSTRAINT postings_posted_complete CHECK (
    status <> 'posted' OR (external_id IS NOT NULL AND vendor_external_id IS NOT NULL AND posted_at IS NOT NULL
                           AND exchange_rate IS NOT NULL AND exchange_rate_date IS NOT NULL
                           AND exchange_rate_source IS NOT NULL)
  ),
  CONSTRAINT postings_payment_consistent CHECK (
       (payment_status = 'unpaid'               AND payment_reason IN ('no_match_yet', 'ambiguous_match', 'fx_match_pending', 'payment_pending'))
    OR (payment_status = 'paid'                 AND payment_reason IS NULL AND amount_paid = amount)
    OR (payment_status = 'paid_with_difference' AND payment_reason = 'payment_difference' AND amount_paid > 0)
    OR (payment_status = 'not_applicable'       AND payment_reason IS NULL AND object_type = 'vendor_credit')
  ),
  CONSTRAINT postings_paid_only_when_posted CHECK (
    payment_status IN ('unpaid', 'not_applicable') OR (status = 'posted' AND object_type = 'bill')
  )
);
CREATE INDEX accounting_postings_due_idx ON accounting_postings (status, next_attempt_at);
CREATE INDEX accounting_postings_open_bills_idx ON accounting_postings (payment_status)
  WHERE status = 'posted' AND object_type = 'bill';
CREATE TRIGGER accounting_postings_updated_at BEFORE UPDATE ON accounting_postings
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ---------------------------------------------------------------------------
-- bank_sync_state: one row per synced bank account (the Plaid access token is NOT here)
-- ---------------------------------------------------------------------------
CREATE TABLE bank_sync_state (
  item_key          text PRIMARY KEY,            -- e.g. 'plaid-sandbox-main'
  account_id        text NOT NULL,               -- the one account used for matching
  account_currency  char(3) NOT NULL CHECK (account_currency ~ '^[A-Z]{3}$'),
  cursor            text,                        -- saved only after the last page of a sync
  last_synced_at    timestamptz,
  created_at        timestamptz NOT NULL DEFAULT now(),
  updated_at        timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER bank_sync_state_updated_at BEFORE UPDATE ON bank_sync_state
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ---------------------------------------------------------------------------
-- bank_transactions: as received from Plaid (sign: positive = money OUT)
-- ---------------------------------------------------------------------------
CREATE TABLE bank_transactions (
  transaction_id            text PRIMARY KEY,
  item_key                  text NOT NULL REFERENCES bank_sync_state (item_key),
  account_id                text NOT NULL,
  amount                    numeric(24,8) NOT NULL,   -- Plaid can send more than 2 decimals
  iso_currency_code         char(3),
  unofficial_currency_code  text,
  date                      date NOT NULL,
  authorized_date           date,
  name                      text,
  merchant_name             text,
  pending                   boolean NOT NULL,
  pending_transaction_id    text,
  removed_at                timestamptz,              -- reported as removed by a later sync
  raw                       jsonb NOT NULL CHECK (jsonb_typeof(raw) = 'object'),
  first_seen_at             timestamptz NOT NULL DEFAULT now(),
  updated_at                timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT bank_tx_one_currency CHECK (iso_currency_code IS NULL OR unofficial_currency_code IS NULL)
);
-- Matching candidates: posted outflows still present
CREATE INDEX bank_transactions_outflows_idx ON bank_transactions (account_id, date)
  WHERE amount > 0 AND NOT pending AND removed_at IS NULL;
CREATE TRIGGER bank_transactions_updated_at BEFORE UPDATE ON bank_transactions
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ---------------------------------------------------------------------------
-- matches: invoice ↔ bank transaction. 'accepted' = used to record a payment;
-- 'candidate' = one of several possibilities (ambiguous), kept for the reviewer.
-- ---------------------------------------------------------------------------
CREATE TABLE matches (
  id                       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  invoice_id               uuid NOT NULL REFERENCES invoices (id) ON DELETE RESTRICT,
  bank_transaction_id      text NOT NULL REFERENCES bank_transactions (transaction_id) ON DELETE RESTRICT,
  status                   text NOT NULL CHECK (status IN ('accepted', 'candidate', 'dismissed')),
  invoice_amount           numeric(18,4) NOT NULL,
  bank_amount              numeric(18,4) NOT NULL CHECK (bank_amount > 0),   -- rounded to the currency's minor unit
  difference               numeric(18,4) NOT NULL,  -- bank_amount - invoice_amount
  currency                 char(3) NOT NULL,
  days_from_invoice        integer NOT NULL,
  name_similarity          numeric(5,4) NOT NULL CHECK (name_similarity BETWEEN 0 AND 1),
  invoice_number_found     boolean NOT NULL,
  rule_version             text NOT NULL,
  external_payment_id      text,                    -- QuickBooks BillPayment ID
  payment_claimed_until    timestamptz,             -- lease while a workflow records the payment
  payment_attempts         integer NOT NULL DEFAULT 0 CHECK (payment_attempts >= 0),
  payment_recorded_at      timestamptz,
  created_at               timestamptz NOT NULL DEFAULT now(),
  updated_at               timestamptz NOT NULL DEFAULT now(),
  UNIQUE (invoice_id, bank_transaction_id),
  CONSTRAINT matches_payment_only_when_accepted CHECK (
    external_payment_id IS NULL OR (status = 'accepted' AND payment_recorded_at IS NOT NULL)
  )
);
-- A bank transaction pays at most one invoice; an invoice is paid by at most one transaction
CREATE UNIQUE INDEX matches_one_invoice_per_transaction ON matches (bank_transaction_id) WHERE status = 'accepted';
CREATE UNIQUE INDEX matches_one_transaction_per_invoice ON matches (invoice_id) WHERE status = 'accepted';
CREATE TRIGGER matches_updated_at BEFORE UPDATE ON matches
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();
