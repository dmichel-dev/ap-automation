-- 005: review items can come from validation (Day 1) or from posting (Day 2), e.g. a
-- vendor with the same name already exists in QuickBooks in another currency.
SET search_path = ap;

ALTER TABLE review_queue
  ADD COLUMN stage text NOT NULL DEFAULT 'validation' CHECK (stage IN ('validation', 'posting'));
