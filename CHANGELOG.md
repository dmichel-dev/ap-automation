# Changelog

All notable changes to this project. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Changed

- **Demo pages restyled** in the look of davidmichel.dev: warm palette, self-hosted fonts (Bricolage Grotesque, Inter, JetBrains Mono; `font-src 'self'` added to the demo's CSP), Tabler outline icons drawn inline, softer cards. Layout, text, flows and behaviour are unchanged; the status colours keep their meaning. The demo's styles moved from `app.css` to `demo.css`, so the review page is unaffected.
- **Links to other sites open in a new tab** on every demo page (`target="_blank" rel="noopener noreferrer"`), davidmichel.dev included; links within the demo stay in the same tab. A test enforces it.
- The start page no longer lists the sample invoices; the session page still offers all seven.

### Added

- **Credits page** (`/credits`, linked from every demo page's footer): fonts and icons with their licence texts, served by the demo ([THIRD-PARTY.md](THIRD-PARTY.md)). Caddy forwards `/credits`; a test checks that Caddy forwards every demo page route and not `/health`.

## [0.6.0] - 2026-11-26

A public demo at demo.davidmichel.dev: anyone can send invoices through the real pipeline, in a short session of their own, and nothing they send outlives it.

### Added

- **Public demo** ([docs/demo.md](docs/demo.md)): one click starts a 60-minute session (no account, nothing typed). A visitor sends one of 7 fictional samples (French, German, Spanish, Arabic, Hindi, and two hosting invoices that raise the bank-details fraud alert), a file of their own, or an email to `invoices+CODE@`. The session page shows each outcome in the review page's words, the real confidence, and for a ready invoice the QuickBooks bill it would create. Nothing is posted. Duplicate and bank-detail checks compare only within the session. The samples give the outcome they describe whatever the live data, and cost no OCR (stored results).
- **Demo limits:** sessions per address (3 an hour, 10 a day), 30 at once, 5 files per session, 5 MB and 3 pages per file, an OCR page budget per day and month, 10 emails per sender a day, and a kill switch. Addresses and senders are kept only as keyed hashes, for 2 days.
- **Demo security:** a session page needs the browser's cookie as well as the code; the pages connect with their own database role that sees demo results only; no file is ever served back; Caddy forwards only the demo's paths.
- **n8n workflows** *AP – Demo processing* (internal webhook, plus a sweep for missed calls) and *AP – Demo clean-up* (every 5 minutes). n8n runs at most 4 executions at once.
- **Deletion when a session ends:** its data and stored files, its emails permanently (not the trash), and the records of the n8n runs that handled its files. A refused demo email is deleted at once. The core decides what each delete answer means and retries the rest.
- **Privacy notice** (`/privacy`): what is received and why (legitimate interest), Mistral AI's 30-day abuse-monitoring retention as an independent controller, the providers involved, how long everything is kept.
- **n8n temporary files deleted after 90 minutes** (`scripts/purge_n8n_temp_files.sh`, cron every 15 minutes): n8n never deletes the attachments a triggered run downloads, even when the run is deleted.

### Fixed

- The German sample's default account is looked up by its full name (`Cost of Goods Sold:Materials`); a test checks every sample account against the chart of accounts.

## [0.5.0] - 2026-11-25

The pipeline runs on a server: one small Ubuntu machine with Docker Compose, HTTPS through Caddy, and nothing of n8n on the internet.

### Added

- **Server deployment** ([docs/deployment.md](docs/deployment.md)): the same compose file as on a workstation plus a server override (`docker-compose.prod.yml`), selected from the server's `.env`. Caddy obtains and renews the certificates. Only Caddy publishes ports beyond `127.0.0.1`; the firewall allows SSH, 80 and 443 only; SSH by key only.
- **n8n and the review page through an SSH tunnel only.** n8n's sign-in page under a custom domain can look like phishing to Google Safe Browsing and taint the whole domain, so nothing of n8n is served publicly: its workflows poll or are called from the server. A retired host name answers an empty 404 on every path.
- **Deploy config check** on every commit and in CI (`scripts/check_deploy_config.sh`): the merged compose files and the Caddyfile are valid, only Caddy publishes public ports, and Caddy never forwards a whole host.
- **Nightly backups** (`scripts/backup.sh --online`, 03:20 UTC): every database and the n8n data volume, kept 14 days; older copies are deleted only after a complete backup.
- **Install and move guide:** a new server step by step, and moving the data from another instance with a per-table fingerprint (row count and MD5 of every row) and SHA-256 sums of the stored files on both sides.

### Changed

- n8n gets 1 GB of memory on the server.

### Fixed

- The config check reads `.env.example` keys that contain digits.


A second intake channel (Google Drive), bank matching for bills in foreign currencies, and a larger test set with Arabic, Hindi and degraded scans to calibrate the confidence threshold.

### Added

- **Google Drive intake:** files dropped in the Drive folder *AP Automation/Inbox* go through the same extraction, checks, duplicate detection and review as email attachments. The folder is the queue: n8n lists it every minute, the core decides per file (process, wait or error), and each file moves to *Processed* or *Error* once it has an answer. A file being processed is leased for 15 minutes, so overlapping runs never call the OCR service twice. Failures use the same retry policy as email. The exact-duplicate check works across channels (the same file by email and by Drive is caught).
- **Foreign-currency bank matching:** a bill in EUR, JPY, GBP… is matched with a payment from the USD bank account at the ECB reference rate of the payment date (Frankfurter API, ECB only, fetched by the bank workflow), within 3 %. The bill payment settles the whole bill at the bank's actual rate (bank amount ÷ bill amount, at the 7 decimals QuickBooks keeps), and QuickBooks posts the exchange gain or loss. An exchange difference is never written off. A payment outside the tolerance, or a currency without a usable rate (e.g. AED, SAR), goes to review with the expected and actual amounts.
- **Daily summary: bank amounts not booked to the cent.** On a foreign-currency bill over 100,000 units, QuickBooks' 7-decimal rate can leave the bank account a few cents off the bank amount; each case is listed (bill, bank amount, amount booked, gap) to be fixed by a journal entry. A documented known limit.
- **Test set of 21 documents**, now including a UAE tax invoice in Arabic and English (AED, TRN), an Indian GST invoice in Hindi and English (INR, GSTIN, CGST + SGST, Indian digit grouping), and three degraded copies of earlier invoices: a 90 dpi fax-like copy, a skewed scan and a phone photo with perspective and shadow. Right-to-left Arabic and Devanagari are shaped with HarfBuzz.
- **Read-only calibration run** (`scripts/calibrate.sh`): each test document goes through the core's own extraction and checks, and is compared with its expected result. Nothing is stored.
- **Second confidence calibration** ([docs/calibration.md](docs/calibration.md)): 20 of the 20 documents with an expected result came out correct, the degraded copies included. Degraded copies scored within 0.004 of their originals and none was misread. The 0.85 threshold and the green badge from 0.95 are kept.

### Changed

- Bills in another currency than the bank account are no longer left aside (`fx_match_pending`): they are matched (rule version `m2`). Rules for bills in the bank account's currency are unchanged.
- One file-type rule for email and Drive (PDF or image), instead of one per channel.

## [0.3.0] - 2026-11-24

Human review, with decisions that take effect in QuickBooks, a fraud alert on bank-detail changes, error handling that never loses an email, a daily summary and a daily automated end-to-end test.

### Added

- **Review page** (its own container): each document, rendered on the server as page images, next to what was read; the reasons in plain language; a confidence badge; one decision panel offering only the actions the core allows. Security defaults: Argon2id passwords, lockout and rate limits, server-side sessions, CSRF protection, a strict CSP, access scoped per user. Reviewer and admin accounts can only sign in on the internal listener, never from the internet. Every page ends with the copyright footer.
- **Review actions:** approve, correct header fields, reject. Approving re-runs the checks a bill needs on the invoice as corrected. Ambiguous dates are confirmed in one click from the suggested reading.
- **Expense coding per supplier:** a reviewer chooses the account on a supplier's first invoice, and it becomes that supplier's default. The picker groups accounts by type and hides accounts a bill must never be coded to (by detail type, so it works on any chart of accounts). A standard US chart of accounts for the QuickBooks sandbox.
- **Review decisions take effect in QuickBooks:** confirm which bank payment pays a bill, or none of them; write off a small under-payment (a vendor credit applied by a bill payment of 0, so no money moves) or leave it open; try again after QuickBooks refused a request. The item stays open until QuickBooks confirms, and nothing is ever recorded twice.
- **Fraud alert on bank-detail changes** (`possible_bank_detail_change`): a document, a letter or an email that announces new bank details (wording in seven languages), or an invoice whose account differs from those on the supplier's earlier invoices, goes to review as possible fraud and is never posted automatically. It has no plain Approve, only "Verified with the supplier" (with a note saying how) or Reject. Full account numbers are never written to logs, messages or the stored extraction (only the last 4 characters); they remain only in the original document and its OCR text, and in n8n's execution data, pruned after 14 days. Such emails get the AP/Fraud alert label. A known supplier's bank details seen for the first time are reported as information in the daily summary, not sent to review.
- **Supplier onboarding and duplicate controls:** a supplier's first invoice goes to review (`new_vendor`). A second duplicate rule catches the same bill resent with another number (same supplier, total and currency, dates within 7 days).
- **Accounting policies in the core:** tax treatment (non-recoverable tax in the cost, recoverable tax as a receivable, withholding as a liability), service periods with a cut-off check, a capitalisation flag (`possible_capex`), and nothing posted into a closed period (the QuickBooks closing date is read before posting).
- **Error handling:** a temporary failure labels the email AP/Error and a re-check every 15 minutes retries it (5 attempts over about 17 hours). A permanent failure, or the last attempt, is given up and reported. An AP/Error email the core never heard of is picked up too. An error workflow logs every failed workflow run.
- **Signed "Run now" requests** start a workflow at once (HMAC with a timestamp, single use, rate-limited). The secret never reaches n8n.
- **Daily summary email** (08:00, Paris time): fraud alerts, invoices received and posted, payments, write-offs per currency, the touchless rate, open review items oldest first, bank details seen for the first time, email intake problems and the end-to-end test result.
- **Daily end-to-end test** (07:30): four test emails go through the real pipeline (posted, review, rejected, fraud alert), then the core checks every result, including no full bank account number in the log. Test data is kept apart from live figures (scope `e2e`), the test's QuickBooks bill is deleted only when it is provably the test's own, and a failure sends an alert at once.
- **Confidence calibration, first pass** ([docs/calibration.md](docs/calibration.md)): the 0.85 threshold and the badge cut-offs are kept. The score measures reading quality, not data correctness; the deterministic checks catch interpretation errors.
- **Engineering:** GitHub Actions CI (ruff, mypy strict, workflow-matches-builder check, tests) on every push; pre-commit hooks; n8n canvases generated by the builders, with a note per section and a layout check.

### Changed

- A bank-details change letter now raises a fraud alert for review instead of being rejected as not an invoice.
- Bills are coded to the supplier's chosen account instead of one default account. Taxes follow the tax treatment policy instead of all going to one tax account. Bills already posted are not changed.

### Fixed

- Automated decisions can never be recorded under a person's name: the review page takes the reviewer from the session, and the machine API only accepts automation labels.
- The core refuses to approve a possible fixed asset without an account, whoever sends the approval.
- The end-to-end checker compares the routing decision made at intake, not the current status a reviewer may have changed since.

## [0.2.0] - 2026-11-23

Invoices that pass every check go all the way to the accounting system, and are marked as paid when the matching bank payment appears.

### Added

- **QuickBooks Online posting:** each ready invoice becomes a bill (a credit note becomes a vendor credit) with the original PDF attached. The vendor is found or created, one per currency. Foreign-currency bills carry QuickBooks' exchange rate for the invoice date. Discounts, shipping and fees are separate bill lines.
- **Plaid bank feed:** outgoing transactions synced every hour, page by page, restarting cleanly after a failed page.
- **Payment matching** on amount (with a tolerance), date window and supplier name or invoice number. A match is accepted only when it is unique on both sides, and the bill payment is then recorded in QuickBooks. A payment that fits several bills is not applied; a payment within tolerance is recorded, and the difference is flagged for review.
- **Safe to re-run:** every QuickBooks write is reserved in the database first, searched before it is created and sent with a fixed request ID. A crash, a retry or two overlapping runs never create a second bill or payment.
- Supplier tax IDs keep their printed form; labels such as `EIN`, `VAT` or `USt-IdNr.` are removed from the copy used for duplicate checks.

## [0.1.0] - 2026-11-22

Email intake, OCR extraction, validation and routing.

### Added

- **Email intake** (n8n + Gmail): only emails sent to the AP alias are taken. PDF and image attachments are processed. Emails with no usable attachment are labelled and logged.
- **Extraction** with Mistral OCR and a strict JSON schema. The model copies values as printed; tested Python code interprets amounts, dates, currencies and signs.
- **Country-neutral invoice format:** ISO 8601 dates, ISO 4217 currencies, line items, generic tax lines (VAT, GST, sales tax, consumption tax, withholding), discounts, shipping, fees and rounding, credit notes.
- **Checks:** required fields, lines + adjustments + tax = total, sign, due date, duplicates, and a confidence score computed by code.
- **Routing:** `rejected` only when a person could do nothing with the document; everything fixable goes to `needs_review` with a reason. Ambiguous dates come with a suggested reading from the supplier's country.
- **Audit trail:** append-only processing log, original files kept with a retention date, idempotent processing.

[Unreleased]: https://github.com/dmichel-dev/ap-automation/compare/v0.6.0...HEAD
[0.6.0]: https://github.com/dmichel-dev/ap-automation/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/dmichel-dev/ap-automation/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/dmichel-dev/ap-automation/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/dmichel-dev/ap-automation/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/dmichel-dev/ap-automation/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/dmichel-dev/ap-automation/releases/tag/v0.1.0
