-- 007: review items can also come from bank matching (Day 2): several bank transactions or
-- bills fit, so a person chooses (Day 3 review flow).
SET search_path = ap;

ALTER TABLE review_queue DROP CONSTRAINT review_queue_stage_check;
ALTER TABLE review_queue
  ADD CONSTRAINT review_queue_stage_check CHECK (stage IN ('validation', 'posting', 'matching'));
