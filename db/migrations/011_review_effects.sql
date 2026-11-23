-- 011: review decisions that take effect in QuickBooks (confirmed bank matches, write-offs of payment
-- differences, retries after a failed call), and the "run now" requests that start a workflow.
SET search_path = ap;

-- ---------------------------------------------------------------------------
-- A decided item waits, still open, until QuickBooks confirms the effect:
--   waiting_for   what it waits for (the bill posting, the bill payment, the write-off)
--   effect_error  the last QuickBooks failure, shown to the reviewer (a retry clears it)
-- The reviewer and the decision are recorded when the decision is made; the item closes
-- (status, resolved_at) only when the effect is recorded.
-- ---------------------------------------------------------------------------
ALTER TABLE review_queue
  ADD COLUMN waiting_for text CHECK (waiting_for IN ('posting', 'payment', 'write_off')),
  ADD COLUMN effect_error text,
  ADD CONSTRAINT review_waiting_only_open CHECK (waiting_for IS NULL OR status = 'open');

-- ---------------------------------------------------------------------------
-- write_offs: the open part of an under-paid bill, written off by a reviewer. In QuickBooks: a vendor
-- credit on the payment-differences account, applied to the bill by a bill payment of 0 (no money
-- moves). Both carry the number AP-WO-<id>, searched before each create (no double write-off).
-- ---------------------------------------------------------------------------
CREATE TABLE write_offs (
  id                   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  match_id             bigint NOT NULL UNIQUE REFERENCES matches (id),
  invoice_id           uuid NOT NULL REFERENCES invoices (id) ON DELETE CASCADE,
  review_id            bigint NOT NULL REFERENCES review_queue (id),
  amount               numeric(18,4) NOT NULL CHECK (amount > 0),
  currency             char(3) NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
  decided_by           text NOT NULL,
  status               text NOT NULL DEFAULT 'due' CHECK (status IN ('due', 'recorded')),
  attempts             integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  claimed_until        timestamptz,
  last_error           text,
  external_credit_id   text,
  external_payment_id  text,
  created_at           timestamptz NOT NULL DEFAULT now(),
  recorded_at          timestamptz,
  CONSTRAINT write_off_recorded_complete CHECK (
    status <> 'recorded'
    OR (external_credit_id IS NOT NULL AND external_payment_id IS NOT NULL AND recorded_at IS NOT NULL)
  )
);
CREATE INDEX write_offs_due_idx ON write_offs (status) WHERE status = 'due';

-- A bill paid with a difference whose open part was written off
ALTER TABLE accounting_postings ADD COLUMN amount_written_off numeric(18,4) CHECK (amount_written_off > 0);
ALTER TABLE accounting_postings DROP CONSTRAINT accounting_postings_payment_status_check;
ALTER TABLE accounting_postings ADD CONSTRAINT accounting_postings_payment_status_check
  CHECK (payment_status IN ('unpaid', 'paid', 'paid_with_difference', 'paid_with_write_off', 'not_applicable'));
ALTER TABLE accounting_postings DROP CONSTRAINT postings_payment_consistent;
ALTER TABLE accounting_postings ADD CONSTRAINT postings_payment_consistent CHECK (
     (payment_status = 'unpaid'               AND payment_reason IN ('no_match_yet', 'ambiguous_match', 'fx_match_pending', 'payment_pending'))
  OR (payment_status = 'paid'                 AND payment_reason IS NULL AND amount_paid = amount)
  OR (payment_status = 'paid_with_difference' AND payment_reason = 'payment_difference' AND amount_paid > 0)
  OR (payment_status = 'paid_with_write_off'  AND payment_reason IS NULL AND amount_paid > 0
                                              AND amount_paid + amount_written_off = amount)
  OR (payment_status = 'not_applicable'       AND payment_reason IS NULL AND object_type = 'vendor_credit')
);

-- ---------------------------------------------------------------------------
-- run_requests: every "run now" request (a workflow started by hand through its webhook), allowed or
-- refused, for the rate limit and the audit trail. The token itself is never stored.
-- ---------------------------------------------------------------------------
CREATE TABLE run_requests (
  id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  target    text NOT NULL CHECK (char_length(target) <= 40),
  outcome   text NOT NULL CHECK (outcome IN ('run', 'bad_token', 'unknown_target', 'rate_limited', 'not_configured')),
  at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX run_requests_at_idx ON run_requests (at);
