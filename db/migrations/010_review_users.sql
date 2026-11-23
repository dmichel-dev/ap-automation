-- 010: review page accounts, server-side sessions and login attempts (rate limiting, audit).
SET search_path = ap;

-- ---------------------------------------------------------------------------
-- review_users: created by the CLI (python -m app.web.users), never by self-registration.
--   reviewer  decides review items of its scope
--   admin     same, and manages users (CLI only)
--   demo      public demo visitors: demo scope only
-- Passwords: Argon2id hashes only. Lockout after repeated failures (doubling, capped).
-- ---------------------------------------------------------------------------
CREATE TABLE review_users (
  id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  email           text NOT NULL UNIQUE CHECK (email = lower(email) AND email LIKE '%_@_%'),
  display_name    text NOT NULL CHECK (char_length(display_name) BETWEEN 1 AND 100),
  password_hash   text NOT NULL CHECK (password_hash LIKE '$argon2id$%'),
  role            text NOT NULL CHECK (role IN ('reviewer', 'admin', 'demo')),
  scope           text NOT NULL CHECK (scope IN ('live', 'e2e', 'demo')),
  active          boolean NOT NULL DEFAULT true,
  failed_logins   integer NOT NULL DEFAULT 0 CHECK (failed_logins >= 0),
  lock_count      integer NOT NULL DEFAULT 0 CHECK (lock_count >= 0),
  locked_until    timestamptz,
  last_login_at   timestamptz,
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT demo_role_demo_scope CHECK ((role = 'demo') = (scope = 'demo'))
);
CREATE TRIGGER review_users_updated_at BEFORE UPDATE ON review_users
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ---------------------------------------------------------------------------
-- review_sessions: the cookie holds a random token; only its SHA-256 is stored.
-- A session belongs to the listener it was created on (internal or public).
-- ---------------------------------------------------------------------------
CREATE TABLE review_sessions (
  token_hash      bytea PRIMARY KEY CHECK (octet_length(token_hash) = 32),
  user_id         bigint NOT NULL REFERENCES review_users (id) ON DELETE CASCADE,
  listener        text NOT NULL CHECK (listener IN ('internal', 'public')),
  csrf_token      text NOT NULL CHECK (char_length(csrf_token) >= 32),
  created_at      timestamptz NOT NULL DEFAULT now(),
  last_seen_at    timestamptz NOT NULL DEFAULT now(),
  expires_at      timestamptz NOT NULL,                -- absolute limit
  client_ip       text,
  user_agent      text CHECK (char_length(user_agent) <= 200),
  window_start    timestamptz NOT NULL DEFAULT now(),  -- decisions rate limit
  window_count    integer NOT NULL DEFAULT 0
);
CREATE INDEX review_sessions_user_idx ON review_sessions (user_id);

-- ---------------------------------------------------------------------------
-- login_attempts: per-IP rate limit and audit (requests over the limit are refused before
-- anything is written, so an attacker can't grow this table). The email is stored as a hash
-- (an attempt with someone else's address is personal data); kept 30 days.
-- ---------------------------------------------------------------------------
CREATE TABLE login_attempts (
  id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  at          timestamptz NOT NULL DEFAULT now(),
  client_ip   text NOT NULL,
  email_hash  text NOT NULL,
  listener    text NOT NULL CHECK (listener IN ('internal', 'public')),
  outcome     text NOT NULL CHECK (outcome IN ('success', 'bad_credentials', 'locked', 'inactive',
                                               'role_not_allowed_here'))
);
CREATE INDEX login_attempts_ip_idx ON login_attempts (client_ip, at);
