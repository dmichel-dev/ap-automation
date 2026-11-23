-- 006: the last error of a bill payment that could not be recorded in the accounting system
-- (retried while payment_attempts < the maximum; the core decides, from the failure facts).
SET search_path = ap;

ALTER TABLE matches ADD COLUMN payment_last_error text;
