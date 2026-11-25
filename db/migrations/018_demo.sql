-- 018: the public demo. A visitor starts a short session with one click (no account, no sign-in) and
-- sends invoices to it: a sample, an upload, or an email to invoices+CODE@. Its invoices carry scope
-- 'demo' and the session's ID; every comparison with earlier documents (same file, duplicate, bank
-- details) stays within the session. Expired sessions are deleted with their invoices; the rules
-- (limits, budget, expiry) are in the core (demo.py).
SET search_path = ap;

-- ---------------------------------------------------------------------------
-- demo_sessions: one per "Start a demo session" click. The browser holds the token (cookie); only
-- its SHA-256 is stored. The code names the session in the email address and the page URL, but is
-- never enough on its own to open the page.
-- ---------------------------------------------------------------------------
CREATE TABLE demo_sessions (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  code           text NOT NULL UNIQUE CHECK (code ~ '^[a-hjkmnp-z2-9]{8}$'),
  token_digest   bytea NOT NULL UNIQUE CHECK (length(token_digest) = 32),
  client_digest  text NOT NULL CHECK (length(client_digest) = 64),     -- keyed hash of the address
  created_at     timestamptz NOT NULL DEFAULT now(),
  expires_at     timestamptz NOT NULL,
  files_used     integer NOT NULL DEFAULT 0 CHECK (files_used >= 0),   -- every channel counts
  CONSTRAINT demo_session_expires_after_start CHECK (expires_at > created_at)
);
CREATE INDEX demo_sessions_expires_idx ON demo_sessions (expires_at);

-- Session starts per client address, kept after the session itself (the daily limit outlives it)
CREATE TABLE demo_starts (
  client_digest  text NOT NULL CHECK (length(client_digest) = 64),
  started_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX demo_starts_client_idx ON demo_starts (client_digest, started_at);

-- OCR pages spent on visitors' files (samples cost nothing), per day and per month
CREATE TABLE demo_budget (
  period        text NOT NULL CHECK (period IN ('day', 'month')),
  period_start  date NOT NULL,
  pages         integer NOT NULL DEFAULT 0 CHECK (pages >= 0),
  PRIMARY KEY (period, period_start)
);

-- Emails to demo addresses per sender and day (keyed hash of the address, never the address)
CREATE TABLE demo_senders (
  sender_digest  text NOT NULL CHECK (length(sender_digest) = 64),
  day            date NOT NULL,
  emails         integer NOT NULL DEFAULT 0 CHECK (emails >= 0),
  PRIMARY KEY (sender_digest, day)
);

-- ---------------------------------------------------------------------------
-- demo_uploads: files waiting for processing (a sample click or an upload). The bytes are kept
-- here until processed, so the public web service needs no access to the file store.
-- ---------------------------------------------------------------------------
CREATE TABLE demo_uploads (
  id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  session_id    uuid NOT NULL REFERENCES demo_sessions (id) ON DELETE CASCADE,
  kind          text NOT NULL CHECK (kind IN ('sample', 'upload')),
  sample        text CHECK (sample ~ '^[a-z0-9-]{1,40}$'),
  file_name     text NOT NULL CHECK (length(file_name) BETWEEN 1 AND 255),
  size          integer NOT NULL CHECK (size > 0),
  content       bytea,                                   -- emptied once processed
  status        text NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'processing', 'done', 'failed')),
  invoice_id    uuid,                                    -- set when done (the invoice goes with the session)
  error         text,
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT demo_upload_sample_named CHECK ((kind = 'sample') = (sample IS NOT NULL)),
  CONSTRAINT demo_upload_content_until_done CHECK (status IN ('queued', 'processing') OR content IS NULL)
);
CREATE INDEX demo_uploads_session_idx ON demo_uploads (session_id, created_at);
CREATE TRIGGER demo_uploads_updated_at BEFORE UPDATE ON demo_uploads
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- Messages and files that reached the mailbox or Drive for a session: trashed there once it expires
CREATE TABLE demo_messages (
  session_id  uuid NOT NULL REFERENCES demo_sessions (id) ON DELETE CASCADE,
  channel     text NOT NULL CHECK (channel IN ('email', 'drive')),
  item_id     text NOT NULL CHECK (length(item_id) BETWEEN 1 AND 200),
  PRIMARY KEY (channel, item_id)
);
CREATE INDEX demo_messages_session_idx ON demo_messages (session_id);

-- What the clean-up still has to trash in Gmail or Drive (outlives the session)
CREATE TABLE demo_trash (
  channel    text NOT NULL CHECK (channel IN ('email', 'drive')),
  item_id    text NOT NULL CHECK (length(item_id) BETWEEN 1 AND 200),
  queued_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (channel, item_id)
);

-- ---------------------------------------------------------------------------
-- Demo invoices belong to their session and go with it. The bill a ready one would post is
-- computed by the core and kept for the page: demo invoices are never sent to the accounting system.
-- ---------------------------------------------------------------------------
ALTER TABLE invoices
  ADD COLUMN demo_session_id uuid REFERENCES demo_sessions (id) ON DELETE CASCADE,
  ADD CONSTRAINT invoices_demo_session CHECK ((scope = 'demo') = (demo_session_id IS NOT NULL));
CREATE INDEX invoices_demo_session_idx ON invoices (demo_session_id) WHERE demo_session_id IS NOT NULL;

CREATE TABLE demo_bill_previews (
  invoice_id  uuid PRIMARY KEY REFERENCES invoices (id) ON DELETE CASCADE,
  preview     jsonb NOT NULL CHECK (jsonb_typeof(preview) = 'object'),
  created_at  timestamptz NOT NULL DEFAULT now()
);
