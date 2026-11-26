-- 020: what a demo session leaves outside the database is deleted with it, permanently. Besides its
-- Gmail messages and Drive files, a session now also remembers the n8n runs that handled its items
-- (their records hold the files and the extracted data). When the session ends, all of them go on
-- the deletion list, which the n8n clean-up works through (demo.py).
SET search_path = ap;

ALTER TABLE demo_messages RENAME TO demo_external_items;
ALTER INDEX demo_messages_session_idx RENAME TO demo_external_items_session_idx;
ALTER TABLE demo_external_items
  DROP CONSTRAINT demo_messages_channel_check,
  ADD CONSTRAINT demo_external_items_channel_check CHECK (channel IN ('email', 'drive', 'n8n'));
COMMENT ON TABLE demo_external_items IS
  'What a demo session left outside the database (Gmail message, Drive file, n8n run): deleted when it ends';

ALTER TABLE demo_trash RENAME TO demo_deletions;
ALTER TABLE demo_deletions
  DROP CONSTRAINT demo_trash_channel_check,
  ADD CONSTRAINT demo_deletions_channel_check CHECK (channel IN ('email', 'drive', 'n8n'));
COMMENT ON TABLE demo_deletions IS
  'Items the clean-up still has to delete permanently in Gmail, Drive or n8n (outlives the session)';
