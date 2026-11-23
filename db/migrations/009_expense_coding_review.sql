-- 009: expense coding, supplier onboarding control, review decisions, scope.
SET search_path = ap;

-- ---------------------------------------------------------------------------
-- Copy of QuickBooks reference data, refreshed by the n8n workflow
-- "AP – Sync QuickBooks reference data". QuickBooks tokens stay in n8n: the core
-- and the review page read this copy. A row missing from the latest sync is kept
-- (bills may point to it) but marked inactive.
-- ---------------------------------------------------------------------------
CREATE TABLE qbo_accounts (
  external_id          text PRIMARY KEY,
  name                 text NOT NULL,
  fully_qualified_name text NOT NULL,
  account_type         text NOT NULL,
  account_sub_type     text,
  classification       text,
  currency             char(3),
  active               boolean NOT NULL,
  synced_at            timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE qbo_vendors (
  external_id   text PRIMARY KEY,
  display_name  text NOT NULL,
  name_norm     text,            -- same normalization as invoices.supplier_name_norm
  currency      char(3),
  active        boolean NOT NULL,
  synced_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX qbo_vendors_name_norm_idx ON qbo_vendors (name_norm) WHERE active;

-- ---------------------------------------------------------------------------
-- supplier_defaults: the account a supplier's invoices are coded to by default.
-- Set by a reviewer on the supplier's first confirmed invoice (never guessed).
-- ---------------------------------------------------------------------------
CREATE TABLE supplier_defaults (
  supplier_key        text PRIMARY KEY,   -- = invoices.supplier_key (tax ID, else normalized name)
  expense_account_id  text NOT NULL,
  source              text NOT NULL CHECK (source IN ('reviewer', 'test_setup')),
  set_by              text NOT NULL,
  set_from_invoice    uuid REFERENCES invoices (id) ON DELETE SET NULL,
  created_at          timestamptz NOT NULL DEFAULT now(),
  updated_at          timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER supplier_defaults_updated_at BEFORE UPDATE ON supplier_defaults
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- The account actually used for an invoice, and why (audit trail)
ALTER TABLE invoices
  ADD COLUMN expense_account_id      text,
  ADD COLUMN expense_account_source  text CHECK (expense_account_source IN ('supplier_default', 'reviewer')),
  ADD CONSTRAINT expense_account_with_source
    CHECK ((expense_account_id IS NULL) = (expense_account_source IS NULL));

-- ---------------------------------------------------------------------------
-- Review decisions: what was decided, with the values before and after.
-- 'verified' = a fraud item confirmed with the supplier (note required).
-- ---------------------------------------------------------------------------
ALTER TABLE review_queue DROP CONSTRAINT review_queue_status_check;
ALTER TABLE review_queue ADD CONSTRAINT review_queue_status_check
  CHECK (status IN ('open', 'approved', 'corrected', 'rejected', 'verified', 'written_off'));
ALTER TABLE review_queue
  ADD COLUMN decision jsonb CHECK (decision IS NULL OR jsonb_typeof(decision) = 'object'),
  ADD COLUMN corrections jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(corrections) = 'array');

-- ---------------------------------------------------------------------------
-- scope: live data, the daily end-to-end test, or the public demo.
-- Summaries, alerts and the review page filter on it.
-- ---------------------------------------------------------------------------
ALTER TABLE invoices
  ADD COLUMN scope text NOT NULL DEFAULT 'live' CHECK (scope IN ('live', 'e2e', 'demo'));
ALTER TABLE review_queue
  ADD COLUMN scope text NOT NULL DEFAULT 'live' CHECK (scope IN ('live', 'e2e', 'demo'));
ALTER TABLE processing_log
  ADD COLUMN scope text NOT NULL DEFAULT 'live' CHECK (scope IN ('live', 'e2e', 'demo'));
CREATE INDEX review_queue_scope_status_idx ON review_queue (scope, status, created_at);

-- ---------------------------------------------------------------------------
-- dates_ambiguous records what was printed (an extraction fact, kept for the audit trail
-- with the raw text and the suggested reading). A reviewer settles the date by correcting it
-- or accepting the suggestion, so a ready invoice needs a resolved date, not unambiguous print.
-- Automatic routing is unchanged: validation still sends every ambiguous date to review.
-- ---------------------------------------------------------------------------
ALTER TABLE invoices DROP CONSTRAINT invoices_ready_complete;
ALTER TABLE invoices ADD CONSTRAINT invoices_ready_complete CHECK (
  status <> 'ready' OR (
    supplier_name IS NOT NULL AND invoice_number IS NOT NULL AND invoice_date IS NOT NULL
    AND currency IS NOT NULL AND total IS NOT NULL
  )
);
