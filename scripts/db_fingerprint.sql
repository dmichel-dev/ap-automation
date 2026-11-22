-- Row counts + fingerprints used to verify a backup/restore or upgrade.
-- Prints hashes only, never secrets or personal data.
\pset format unaligned
\pset tuples_only on
\pset fieldsep ' | '
SELECT 'server_version', current_setting('server_version');
SELECT 'n8n users', count(*), md5(string_agg(id::text || email || coalesce(password, ''), ',' ORDER BY id)) FROM public."user";
SELECT 'n8n credentials', count(*), md5(string_agg(id || name || type || data, ',' ORDER BY id)) FROM public.credentials_entity;
SELECT 'n8n workflows', count(*), md5(string_agg(id || name || active::text || nodes::text, ',' ORDER BY id)) FROM public.workflow_entity;
SELECT 'n8n executions', count(*) FROM public.execution_entity;
SELECT 'n8n settings', count(*), md5(string_agg(key || value, ',' ORDER BY key)) FROM public.settings;
SELECT 'ap.invoices', count(*), md5(string_agg(id::text || status || coalesce(total::text, ''), ',' ORDER BY id)) FROM ap.invoices;
SELECT 'ap.invoice_lines', count(*) FROM ap.invoice_lines;
SELECT 'ap.invoice_tax_lines', count(*) FROM ap.invoice_tax_lines;
SELECT 'ap.review_queue', count(*) FROM ap.review_queue;
SELECT 'ap.processing_log', count(*), md5(string_agg(id::text || step || message, ',' ORDER BY id)) FROM ap.processing_log;
SELECT 'ap.schema_migrations', string_agg(version, ',' ORDER BY version) FROM ap.schema_migrations;
