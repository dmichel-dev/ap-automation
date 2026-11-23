-- 007: review items can also come from bank matching: several bank transactions or
-- bills fit, so a person chooses.
SET search_path = ap;

ALTER TABLE review_queue DROP CONSTRAINT review_queue_stage_check;
ALTER TABLE review_queue
  ADD CONSTRAINT review_queue_stage_check CHECK (stage IN ('validation', 'posting', 'matching'));
