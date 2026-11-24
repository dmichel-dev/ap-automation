-- 014: emails that could not be processed (OCR service down, core unreachable…). One row per email:
-- the re-check retries it with backoff until it works or the attempts run out; then a person looks.
-- An email labelled AP/Error with no row here (the core was unreachable when it failed) is due at once:
-- the re-check creates its row.
SET search_path = ap;

CREATE TABLE intake_failures (
  id                    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  channel               text NOT NULL CHECK (channel IN ('email', 'drive', 'upload')),
  message_id            text NOT NULL CHECK (length(message_id) BETWEEN 1 AND 200),
  state                 text NOT NULL DEFAULT 'waiting' CHECK (state IN ('waiting', 'given_up', 'resolved')),
  attempts              integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  next_attempt_at       timestamptz NOT NULL DEFAULT now(),
  last_stage            text,                -- fetch | screen | process | recheck
  last_status_code      integer,             -- the core's HTTP status; NULL = no answer
  last_error_code       text NOT NULL,       -- classified by the core (intake_failures.py)
  permanent             boolean NOT NULL DEFAULT false,
  counted_execution_id  text,                -- the n8n run that last counted an attempt (one per run)
  failed_execution_id   text,                -- the n8n run that last reported a failure
  operator_retries      integer NOT NULL DEFAULT 0,
  first_failed_at       timestamptz NOT NULL DEFAULT now(),
  last_failed_at        timestamptz NOT NULL DEFAULT now(),
  given_up_at           timestamptz,
  resolved_at           timestamptz,
  UNIQUE (channel, message_id)
);

CREATE INDEX intake_failures_due_idx ON intake_failures (next_attempt_at) WHERE state = 'waiting';
