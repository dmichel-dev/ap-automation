-- 017: leases for intake channels whose queue is a folder (Google Drive). n8n lists the Inbox folder
-- every minute; a file handed out for processing is leased so the next run doesn't send it to the OCR
-- service again while the first is still working on it. The file leaves the folder once it has an
-- answer, so an expired lease only matters for a run that died (the file is then handed out again).
SET search_path = ap;

CREATE TABLE intake_leases (
  channel       text NOT NULL CHECK (channel IN ('drive')),
  item_id       text NOT NULL CHECK (length(item_id) BETWEEN 1 AND 200),
  leased_until  timestamptz NOT NULL,
  execution_id  text,
  PRIMARY KEY (channel, item_id)
);
