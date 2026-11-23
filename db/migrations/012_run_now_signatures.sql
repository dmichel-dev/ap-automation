-- 012: "run now" requests are signed instead of carrying the token. The caller sends a timestamp and
-- HMAC-SHA256(token, "<timestamp>.<target>"); the token itself never travels (n8n stores webhook
-- requests). A signature is valid for a few minutes and only once: the SHA-256 of each signature that
-- started a run is kept, so a replayed request is refused.
SET search_path = ap;

ALTER TABLE run_requests ADD COLUMN signature_sha256 text CHECK (signature_sha256 ~ '^[0-9a-f]{64}$');
CREATE UNIQUE INDEX run_requests_signature_once ON run_requests (signature_sha256) WHERE outcome = 'run';

ALTER TABLE run_requests DROP CONSTRAINT run_requests_outcome_check;
ALTER TABLE run_requests ADD CONSTRAINT run_requests_outcome_check CHECK (
  outcome IN ('run', 'bad_token', 'expired', 'replayed', 'unknown_target', 'rate_limited', 'not_configured')
);
