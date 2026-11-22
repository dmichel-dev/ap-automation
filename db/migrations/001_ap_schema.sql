-- 001: core AP schema. Applied as the ap_app role (owner of schema "ap").
-- Conventions:
--   * Amounts are numeric(18,4) in the document currency. Credit notes are stored SIGNED
--     (total <= 0), even when the PDF prints positive amounts.
--   * Enumerations are text + CHECK (easier to extend than Postgres ENUM types).
--   * processing_log is append-only and must never contain document content (privacy).

SET search_path = ap;

-- ---------------------------------------------------------------------------
-- Shared trigger functions
-- ---------------------------------------------------------------------------
CREATE FUNCTION set_updated_at() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  NEW.updated_at := now();
  RETURN NEW;
END $$;

CREATE FUNCTION forbid_change() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION '% is append-only (% not allowed)', TG_TABLE_NAME, TG_OP;
END $$;

-- ---------------------------------------------------------------------------
-- invoices: one row per received document (invoice or credit note)
-- ---------------------------------------------------------------------------
CREATE TABLE invoices (
  id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),

  -- Source
  channel                 text NOT NULL CHECK (channel IN ('email', 'drive', 'upload')),
  source_ref              text NOT NULL,          -- e.g. gmail:<messageId>:<attachmentId>
  received_at             timestamptz NOT NULL DEFAULT now(),
  file_name               text NOT NULL,
  file_mime_type          text NOT NULL,
  file_size_bytes         integer NOT NULL CHECK (file_size_bytes > 0),
  file_sha256             char(64) NOT NULL CHECK (file_sha256 ~ '^[0-9a-f]{64}$'),

  -- Original file storage (privacy: deleted after posting or after the retention period;
  -- file_sha256 is kept so the duplicate-file check still works afterwards)
  file_path               text,                   -- relative to the core service storage root
  file_delete_after       timestamptz,
  file_deleted_at         timestamptz,
  file_deletion_reason    text CHECK (file_deletion_reason IN ('posted', 'retention', 'rejected', 'manual')),

  -- Document
  document_type           text NOT NULL DEFAULT 'invoice' CHECK (document_type IN ('invoice', 'credit_note')),
  invoice_number          text,
  invoice_number_norm     text,                   -- uppercase, separators removed
  related_invoice_number  text,                   -- credit note: the invoice it cancels
  invoice_date            date,
  due_date                date,
  dates_ambiguous         boolean NOT NULL DEFAULT false,

  -- Parties
  supplier_name           text,
  supplier_name_norm      text,                   -- lowercase, legal form and punctuation removed
  supplier_tax_id         text,
  supplier_tax_id_norm    text,                   -- uppercase, spaces/dots/dashes removed
  supplier_country        char(2) CHECK (supplier_country ~ '^[A-Z]{2}$'),   -- ISO 3166-1 alpha-2
  buyer_name              text,
  buyer_tax_id            text,
  -- Duplicate key: tax ID when known, otherwise normalized name
  supplier_key            text GENERATED ALWAYS AS (COALESCE(supplier_tax_id_norm, supplier_name_norm)) STORED,

  -- Amounts (document currency, signed for credit notes)
  currency                char(3) CHECK (currency ~ '^[A-Z]{3}$'),           -- ISO 4217
  subtotal                numeric(18,4),
  tax_total               numeric(18,4),
  total                   numeric(18,4),

  -- Home-currency conversion (Day 4, Frankfurter/ECB)
  home_currency           char(3) CHECK (home_currency ~ '^[A-Z]{3}$'),
  home_amount             numeric(18,4),
  fx_rate                 numeric(18,8) CHECK (fx_rate > 0),
  fx_date                 date,

  -- Extraction
  language                text,                   -- BCP 47, e.g. fr, ja, de-CH
  ocr_text                text,                   -- original-language text
  extraction              jsonb,                  -- raw provider output, for audit
  extraction_provider     text,
  confidence              numeric(4,3) CHECK (confidence BETWEEN 0 AND 1),

  -- Outcome
  status                  text NOT NULL CHECK (status IN ('ready', 'needs_review', 'rejected')),
  status_reasons          jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(status_reasons) = 'array'),
  n8n_execution_id        text,

  created_at              timestamptz NOT NULL DEFAULT now(),
  updated_at              timestamptz NOT NULL DEFAULT now(),

  CONSTRAINT invoices_source_unique UNIQUE (channel, source_ref),
  CONSTRAINT invoices_total_sign CHECK (
    total IS NULL
    OR (document_type = 'invoice'     AND total >= 0)
    OR (document_type = 'credit_note' AND total <= 0)
  ),
  CONSTRAINT invoices_due_after_issue CHECK (due_date IS NULL OR invoice_date IS NULL OR due_date >= invoice_date),
  CONSTRAINT invoices_fx_complete CHECK (
    (home_amount IS NULL AND fx_rate IS NULL AND fx_date IS NULL)
    OR (home_currency IS NOT NULL AND home_amount IS NOT NULL AND fx_rate IS NOT NULL AND fx_date IS NOT NULL)
  ),
  CONSTRAINT invoices_file_state CHECK (
    (file_deleted_at IS NULL  AND file_deletion_reason IS NULL)
    OR (file_deleted_at IS NOT NULL AND file_deletion_reason IS NOT NULL AND file_path IS NULL)
  ),
  CONSTRAINT invoices_ready_complete CHECK (
    status <> 'ready' OR (
      supplier_name IS NOT NULL AND invoice_number IS NOT NULL AND invoice_date IS NOT NULL
      AND currency IS NOT NULL AND total IS NOT NULL AND NOT dates_ambiguous
    )
  )
);

CREATE INDEX invoices_duplicate_idx ON invoices (supplier_key, document_type, invoice_number_norm)
  WHERE supplier_key IS NOT NULL AND invoice_number_norm IS NOT NULL;
CREATE INDEX invoices_sha256_idx       ON invoices (file_sha256);
CREATE INDEX invoices_status_idx       ON invoices (status, created_at);
CREATE INDEX invoices_file_purge_idx   ON invoices (file_delete_after) WHERE file_path IS NOT NULL;

CREATE TRIGGER invoices_updated_at BEFORE UPDATE ON invoices
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ---------------------------------------------------------------------------
-- invoice_lines
-- ---------------------------------------------------------------------------
CREATE TABLE invoice_lines (
  id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  invoice_id   uuid NOT NULL REFERENCES invoices (id) ON DELETE CASCADE,
  line_no      integer NOT NULL CHECK (line_no > 0),
  description  text,
  quantity     numeric(18,6),
  unit_price   numeric(18,6),
  amount       numeric(18,4) NOT NULL,      -- net line amount; may be negative (discounts, credit notes)
  tax_rate     numeric(7,4) CHECK (tax_rate BETWEEN 0 AND 100),   -- percent
  UNIQUE (invoice_id, line_no)
);

-- ---------------------------------------------------------------------------
-- invoice_tax_lines: generic, country-neutral tax breakdown
-- ---------------------------------------------------------------------------
CREATE TABLE invoice_tax_lines (
  id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  invoice_id    uuid NOT NULL REFERENCES invoices (id) ON DELETE CASCADE,
  tax_type      text NOT NULL CHECK (tax_type IN ('vat', 'gst', 'sales_tax', 'withholding', 'other', 'none')),
  label         text,                        -- as printed, e.g. 'TVA 20 %', '消費税 10%'
  rate          numeric(7,4) CHECK (rate BETWEEN 0 AND 100),        -- percent
  taxable_base  numeric(18,4),
  amount        numeric(18,4) NOT NULL       -- withholding is negative
);
CREATE INDEX invoice_tax_lines_invoice_idx ON invoice_tax_lines (invoice_id);

-- ---------------------------------------------------------------------------
-- review_queue: human review (workflow built on Day 3)
-- ---------------------------------------------------------------------------
CREATE TABLE review_queue (
  id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  invoice_id    uuid NOT NULL REFERENCES invoices (id) ON DELETE CASCADE,
  reasons       jsonb NOT NULL CHECK (jsonb_typeof(reasons) = 'array' AND jsonb_array_length(reasons) > 0),
  status        text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'approved', 'corrected', 'rejected')),
  reviewer      text,
  decision_note text,
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now(),
  resolved_at   timestamptz,
  CONSTRAINT review_resolution CHECK (
    (status = 'open' AND resolved_at IS NULL)
    OR (status <> 'open' AND resolved_at IS NOT NULL AND reviewer IS NOT NULL)
  )
);
-- At most one open review item per invoice
CREATE UNIQUE INDEX review_queue_one_open_idx ON review_queue (invoice_id) WHERE status = 'open';
CREATE INDEX review_queue_status_idx ON review_queue (status, created_at);

CREATE TRIGGER review_queue_updated_at BEFORE UPDATE ON review_queue
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ---------------------------------------------------------------------------
-- processing_log: append-only audit trail. invoice_id is optional so events with
-- no invoice (e.g. an email without a valid attachment) are still logged.
-- ---------------------------------------------------------------------------
CREATE TABLE processing_log (
  id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  logged_at         timestamptz NOT NULL DEFAULT now(),
  invoice_id        uuid REFERENCES invoices (id) ON DELETE SET NULL,
  channel           text CHECK (channel IN ('email', 'drive', 'upload', 'system')),
  source_ref        text,
  n8n_execution_id  text,
  step              text NOT NULL,           -- e.g. intake, extract, validate, route, post
  level             text NOT NULL CHECK (level IN ('debug', 'info', 'warning', 'error')),
  message           text NOT NULL,
  details           jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(details) = 'object')
);
CREATE INDEX processing_log_invoice_idx ON processing_log (invoice_id, logged_at);
CREATE INDEX processing_log_time_idx    ON processing_log (logged_at);

-- Block UPDATE and DELETE. The ON DELETE SET NULL above is the only allowed change,
-- so the trigger lets an update through only when it just clears invoice_id.
CREATE FUNCTION processing_log_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'UPDATE'
     AND NEW.invoice_id IS NULL
     AND (to_jsonb(NEW) - 'invoice_id') = (to_jsonb(OLD) - 'invoice_id') THEN
    RETURN NEW;
  END IF;
  RAISE EXCEPTION 'processing_log is append-only (% not allowed)', TG_OP;
END $$;

CREATE TRIGGER processing_log_append_only BEFORE UPDATE OR DELETE ON processing_log
  FOR EACH ROW EXECUTE FUNCTION processing_log_guard();
CREATE TRIGGER processing_log_no_truncate BEFORE TRUNCATE ON processing_log
  FOR EACH STATEMENT EXECUTE FUNCTION forbid_change();
