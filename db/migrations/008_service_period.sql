-- 008: service / delivery period printed on the invoice (cut-off check), with the suggested
-- reading when its dates are ambiguous (one-click confirmation in the review).
SET search_path = ap;

ALTER TABLE invoices
  ADD COLUMN service_period_start            date,
  ADD COLUMN service_period_end              date,
  ADD COLUMN service_period_start_raw        text,
  ADD COLUMN service_period_end_raw          text,
  ADD COLUMN service_period_start_suggested  date,
  ADD COLUMN service_period_end_suggested    date;
