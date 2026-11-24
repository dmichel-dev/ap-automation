-- 015: the daily end-to-end test. Its data carries scope 'e2e' (invoices, review_queue, processing_log,
-- intake_failures) and never reaches live figures. A run's own records are kept here; its invoices are
-- deleted at the start of the next run (only rows with scope 'e2e', decided in the core: e2e_test.py).
SET search_path = ap;

-- ---------------------------------------------------------------------------
-- e2e_runs: one row per run, with what it checked and what failed
-- ---------------------------------------------------------------------------
CREATE TABLE e2e_runs (
  id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  started_at     timestamptz NOT NULL DEFAULT now(),
  finished_at    timestamptz,
  status         text NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'passed', 'failed')),
  execution_id   text,
  messages       jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(messages) = 'array'),  -- scenario + Gmail ID
  checks         integer NOT NULL DEFAULT 0 CHECK (checks >= 0),                              -- checks passed
  failures       jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(failures) = 'array'),
  CONSTRAINT e2e_run_finished CHECK ((status = 'running') = (finished_at IS NULL))
);
CREATE INDEX e2e_runs_started_idx ON e2e_runs (started_at);

-- ---------------------------------------------------------------------------
-- e2e_qbo_objects: the QuickBooks objects the test's postings created, until they are deleted.
-- invoice_id has no foreign key: the row outlives the invoice (deleted by the next run's reset),
-- so the record of what was deleted in QuickBooks stays.
-- ---------------------------------------------------------------------------
CREATE TABLE e2e_qbo_objects (
  id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  run_id        bigint NOT NULL REFERENCES e2e_runs (id),   -- the run that found it
  invoice_id    uuid NOT NULL,
  entity        text NOT NULL CHECK (entity IN ('Bill', 'Attachable')),
  external_id   text NOT NULL,
  state         text NOT NULL DEFAULT 'pending'
                CHECK (state IN ('pending', 'checked', 'deleted', 'gone', 'refused')),
  facts         jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(facts) = 'object'),  -- what was read back
  reason        text,                                       -- why a delete was refused
  updated_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (entity, external_id)
);
CREATE TRIGGER e2e_qbo_objects_updated_at BEFORE UPDATE ON e2e_qbo_objects
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ---------------------------------------------------------------------------
-- intake_failures: scope too, so a failed test email never shows in the live summary
-- ---------------------------------------------------------------------------
ALTER TABLE intake_failures
  ADD COLUMN scope text NOT NULL DEFAULT 'live' CHECK (scope IN ('live', 'e2e', 'demo'));
