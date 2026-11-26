# AP Automation

End-to-end **accounts payable invoice processing**, from intake to accounting: multi-channel, multilingual and multi-currency.
Built with **n8n**, **Mistral OCR**, **PostgreSQL**, a small **Python (FastAPI)** core service, **Plaid** (bank) and **QuickBooks Online** (accounting).

> Status: work in progress. Email and Drive intake → OCR → validation → routing → human review → QuickBooks bill with the original file → bank matching (Plaid) → bill payment, with a fraud alert on bank-detail changes, error handling, a daily summary and a daily automated end-to-end test, verified on sandboxes and running on a server. A **public demo** lets anyone send invoices through it. All test invoices come from fictional companies. What changed in each version: [CHANGELOG](CHANGELOG.md).

## How it works

```
Intake channels ──► common invoice format ──► core checks ──► routing ──► bank match ──► accounting ──► log / notify
(Email, Drive,       (country-neutral JSON)    (required fields,  ready /       (Plaid)        (QuickBooks)
 upload form)                                   totals, duplicates, needs_review /
                                                confidence)         rejected
```

- **Channels** are pluggable. Each one hands files to the same core endpoint: email to `invoices@`, and a Google Drive folder (files dropped in **AP Automation/Inbox** are processed and moved to Processed or Error). The same file arriving by both is caught as a duplicate.
- **The LLM only extracts data.** Every approval decision is deterministic, unit-tested code, which protects against prompt injection hidden in uploaded documents.
- **n8n orchestrates, the Python core decides.** n8n runs the triggers and the calls (Gmail, QuickBooks, Plaid) and routes on the core's answers. Every rule lives in the core, typed and tested: which email attachments to process, validation and routing, the content of each bill, what QuickBooks' answers mean (reuse a vendor or create it, a duplicate bill), and whether a failure is retried or sent to review.
- Anything uncertain (failed check, ambiguous date, low confidence, possible duplicate, a supplier's first invoice, a bank payment that fits several bills) goes to **human review**. Nothing is guessed, including the expense account: each supplier's account is chosen once by a person, then reused.
- **Fraud alert on bank-detail changes.** An invoice, a letter or an email that announces new bank details, or an invoice whose account differs from those on the supplier's earlier invoices, goes to review as possible fraud and is never posted automatically. A reviewer must confirm it with the supplier through a known contact. Bank details read from a document are never trusted. Full account numbers are never written to logs, messages or the stored extraction (last 4 characters only); they remain only in the original document and its OCR text, and in n8n's execution data, pruned after 14 days. A known supplier's bank details seen for the first time are reported in the daily summary, not sent to review.
- **A review page for people** shows each document (rendered on the server as images) next to what was read, and records who approved, corrected or rejected it. Its decisions take effect in QuickBooks: confirm which bank payment pays a bill, write off a small under-payment (a vendor credit, no money moves), or retry after QuickBooks refused something. The item stays open until QuickBooks confirms. Every decision records who acted: the signed-in person on the page, or an automation label for automated runs, never a person's name. It is built with security defaults: Argon2id passwords, lockout and rate limits, server-side sessions, CSRF protection, a strict CSP, access scoped per user. Reviewer accounts can't sign in from the internet at all.
- **A public demo** at demo.davidmichel.dev: one click starts a 60-minute session, then a visitor sends one of 7 fictional samples (six languages, three scripts, four currencies), their own file, or an email to the session's own address, and sees each outcome in the review page's words, with the QuickBooks bill a ready invoice would create (nothing is posted). Everything runs in the demo's own scope and session, with per-address limits and an OCR budget. When the session ends, its data, its emails (permanently) and its n8n run records are deleted ([demo](docs/demo.md)).
- Details: [invoice format](docs/common-invoice-format.md), [checks and routing](docs/validation.md), [human review](docs/review.md), [Drive intake](docs/operations.md#google-drive-intake), [review page security](docs/security.md), [data model](docs/data-model.md), [bank matching](docs/matching.md), [Plaid and QuickBooks facts verified live](docs/integrations.md), [operations](docs/operations.md), [confidence calibration](docs/calibration.md), [deployment](docs/deployment.md), [public demo](docs/demo.md).
- **Failures are retried or handed to a person, never lost.** A temporary failure (OCR service down, core restarting) labels the email AP/Error, and a re-check every 15 minutes tries it again (5 attempts over about 17 hours). A permanent one (a file the OCR service refuses), or the last failed attempt, is given up and reported. An AP/Error email the core never heard of (it was down when the email failed) is picked up too. Failed workflow runs are logged by an error workflow. Details: [errors and retries](docs/operations.md#errors-and-retries).
- **A daily summary email** lists fraud alerts, invoices received and posted, payments, write-offs, the touchless rate (documents posted with no review), open review items and intake problems, plus the result of the **daily end-to-end test** that sends four test emails through the real pipeline each morning and alerts at once if anything fails ([operations](docs/operations.md#daily-summary)).
- **The confidence threshold was checked against real results.** The score measures how well the page was read, not whether the data is right; the only wrong extraction so far scored 0.982 and was caught by the totals check ([calibration](docs/calibration.md)).
- **Safe to re-run.** Every write to QuickBooks is reserved in the database first, searched before it is created and sent with a fixed request ID, so a crash or a retry never creates a second bill, payment or write-off. A workflow can be started at once with a signed **Run now** request (HMAC with a timestamp, single use, rate-limited); the secret never reaches n8n.

## Stack

| Component | Role |
|---|---|
| n8n Community Edition | Orchestration: intake triggers, routing, integrations |
| Python core (FastAPI) | Extraction, normalization, validation, persistence, posting content, bank matching, every decision |
| Review page (FastAPI + Jinja2, own container) | Sign-in, review list, document pages, approve / correct / reject, bank-match and payment-difference decisions, retries; loopback only for reviewer accounts |
| Mistral OCR (`mistral-ocr-latest`) | PDF/scan text and structured-data extraction |
| PostgreSQL 17 | n8n database + `ap` schema (invoices, lines, taxes, review queue, log, postings, bank transactions, matches) |
| QuickBooks Online (sandbox, US, multi-currency) | Vendors, bills and vendor credits with the original file, bill payments, write-offs of payment differences |
| Plaid (sandbox) | Bank transactions (`/transactions/sync`) for payment matching |
| Docker Compose | Runs everything locally and on the VPS |

## Quick start

```bash
cp .env.example .env        # then fill in the values (openssl rand -hex 32 for secrets)
docker volume create ap-automation_pg17_data   # once: the database volume is external (protected from `down -v`)
docker compose up -d --build
open http://localhost:5678  # n8n
docker compose exec review-web python -m app.web.users add --email you@example.com --name "Your Name" --role admin --scope live
open http://localhost:8081  # review page
```

## Tests and verification

Three levels, from fastest to most realistic:

| Level | What it covers | Command |
|---|---|---|
| Unit + integration (837) | Normalization, validation, totals, dates, duplicates, email screening, bank-detail changes (change wording in seven languages, comparison with the supplier's earlier accounts, masking everywhere: no full number stored outside the document's own text), posting content and decisions, matching, review decisions and their effect in QuickBooks (confirmed matches, write-offs, retries without duplicates), failed emails (temporary or permanent, backoff, given up, AP/Error emails with no record) and crash tests (OCR service and core failing mid-run, QuickBooks answers lost after the bill or payment was created: never a second invoice, bill or payment), signed "Run now" requests (refused without a valid signature, expired or replayed, rate limits), the review page's security (sign-in, lockout, sessions, CSRF, listeners, scope, document rendering, headers) and presentation (plain-language reasons for every code, confidence bands, decision panel, fraud items with no action pre-selected, the data label in the header, five sample suppliers with their own invoices), the daily summary's figures and the touchless rate, the daily end-to-end test's checks and its test-data isolation (deletes refused on live data), the full pipeline on a test database | `docker compose run --rm core-test` |
| Live provider (8) | Real Mistral OCR calls on fictional documents, including bank-change requests in English, French, German and Spanish | `docker compose run --rm core-test pytest -m live` |
| End to end (12 emails) | Gmail → n8n → core → database → Gmail labels → QuickBooks → bank matching | send the test set, then `scripts/run_e2e.sh --since <start time>` |
| Extraction calibration (21 documents) | The core's extraction and checks on every test document, including Arabic and Hindi invoices and degraded copies (fax-like, crooked scan, phone photo); confidence score and fields compared, nothing stored ([calibration](docs/calibration.md)) | `scripts/calibrate.sh` |
| Daily end to end (4 emails, every morning) | Same path, automated: one invoice posted to QuickBooks then deleted, one sent to review, one rejected, one fraud alert. Test data kept apart from live figures; a failure sends an alert at once and shows in the daily summary | workflow **AP – Daily e2e test** ([operations](docs/operations.md#daily-end-to-end-test)) |

The end-to-end test set (`test-data/invoices/`, generated by `test-data/generate.py`) has one expected result per file:

| Document | Expected |
|---|---|
| French invoice, EUR, 5 % discount, two VAT rates | `ready` |
| Japanese invoice, JPY, 10 % and reduced 8 % consumption tax | `ready` |
| German invoice whose printed total is wrong by €10 | `needs_review` (`total_mismatch`) |
| Spanish invoice, date `03/04/2026`, IRPF withholding | `needs_review` (`ambiguous_date`, 3 April suggested) |
| UK credit note, scanned image | `ready`, stored as a negative amount |
| Letter asking to change bank details, with a prompt-injection line | `needs_review` (`possible_bank_detail_change`): a fraud alert, never posted |
| Exact resend of the French invoice | `rejected` (`duplicate_file`), no OCR call |
| Email with only a `.docx` | no invoice; labelled `AP/Rejected` and logged |
| US invoice, USD, paid by an exact bank payment | `ready` → bill → paid in full |
| US invoice, $1,212.50, bank payment $1,212.00 | `ready` → bill → paid within tolerance, $0.50 left open |
| Two US invoices of $86.80, one bank payment of $86.80 | `ready` → bills → not paid; one review item each (`ambiguous_match`) |

Also in the set: US invoices for the review rules (prior-period service, possible fixed asset, new supplier, missing expense account), a UAE tax invoice in Arabic and English (AED, 5 % VAT), an Indian GST invoice in Hindi and English (INR, CGST + SGST), and three degraded copies of other invoices, used by the calibration only.

The bank side of the last three rows is in `test-data/bank/matching-transactions.json` (Plaid sandbox custom transactions).

First run (2026-11-22): 7/8 as expected. The Spanish invoice exposed a double-counted withholding tax; the fix is a deterministic normalization rule (see `docs/validation.md`), now covered by unit tests and a live re-check.

Day 2 run (2026-11-23): the four US documents 4/4 as expected, then posted to QuickBooks and matched against the bank as listed above; a second run changed nothing, and a simulated crash after a payment was created did not duplicate it.

Day 3 (2026-11-24): review decisions checked live in QuickBooks (three approved invoices posted, a write-off, a confirmed bank match); the core stopped while an email was being processed, and the AP/Error re-check recovered it with no lost email and no duplicate; the daily end-to-end test passed live (40 checks), including the fraud alert and the clean-up of its QuickBooks bill.

### Code quality

Every commit runs [pre-commit](https://pre-commit.com) (`.pre-commit-config.yaml`): **ruff** (lint and format), **mypy** (strict on the core service, run in the core's own image: `docker compose run --rm lint`) and a check that each committed workflow JSON is exactly what its builder generates. Install once with `pre-commit install`. Conventions: [`CLAUDE.md`](CLAUDE.md#coding-conventions); the main one is that business rules live only in the Python core, and n8n Code nodes only reshape data.

## Updating the n8n workflow

The workflows are generated from code (`n8n/build_email_intake.py`, `n8n/build_drive_intake.py`, `n8n/build_post_qbo.py`, `n8n/build_bank_sync.py`, `n8n/build_sync_reference.py`, `n8n/build_run_now.py`, `n8n/build_error_log.py`, `n8n/build_daily_summary.py`, `n8n/build_e2e_test.py`, `n8n/build_e2e_cleanup.py`, `n8n/build_demo_processing.py`, `n8n/build_demo_cleanup.py`) and committed without credentials. Each canvas is laid out by the builders too: nodes on a grid, grouped in sections with a note explaining each one (`Section` and `lay_out` in `n8n/nodes.py`), and `n8n/check_layout.py` fails the commit and CI if a node sits outside a section or anything overlaps. After a change made in the n8n editor, `scripts/export_workflow.sh` exports the live workflows, strips credential IDs and instance data, refuses to write a file if a secret scan matches, and shows the diff.

## Repository layout

```
docker-compose.yml   .env.example
db/init/             Postgres roles + migration runner (first start)
db/migrations/       ap schema migrations
services/core/       FastAPI core service; app/web/ is the review page and the demo pages (their own containers)
deploy/              Caddyfile (HTTPS front door: the demo pages only)
n8n/                 Workflow builders (shared nodes.py) + exported workflows (no credentials)
test-data/invoices/  Fictional test invoices + expected results
test-data/bank/      Bank transactions for the matching tests
config/              Reference setup data (standard US chart of accounts)
scripts/             Workflow export, end-to-end check, backup, Plaid sandbox setup, QuickBooks chart of accounts
docs/                Architecture and data-format documentation
```

## Roadmap

- [x] Day 1: Email intake, Mistral OCR extraction, validation, duplicate check
- [x] Day 2: QuickBooks bills, Plaid bank matching and bill payments
- [x] Day 3: Human review, fraud alert, error handling, daily summary, daily end-to-end test
- [x] Day 4: Google Drive intake, foreign-currency bank matching, test set of 20 invoices and confidence calibration
- [x] Day 5: VPS deployment (Caddy, HTTPS, backups, n8n reached through an SSH tunnel only)
- [x] Day 6: Public demo: samples, uploads and email, session page with QuickBooks bill preview, privacy notice, limits, deletion
- [ ] Day 7: Video, architecture diagram, case study

**After the 7 days (documented, not built):**

- **Coding rules and AI-suggested accounts:** a rules table (supplier, keywords, amount → account), then account suggestions from past decisions. A suggestion is only shown pre-selected; a person still confirms it, and approval stays deterministic.
- **Per-line account coding** (today: one account per invoice).
- **Line-item editing in the review:** add, change or remove lines, with the totals check re-run. Today a reviewer corrects header fields only, and an invoice with wrong lines is rejected.
- **Two-factor sign-in (TOTP)** for reviewer and admin accounts, with recovery codes. Until then these accounts can only sign in through the internal listener (SSH tunnel), never from the internet.
- **Approval thresholds and an approver role:** above an amount, a second person approves after the reviewer.
- **Segregation of duties:** whoever sets up a supplier or its bank details can't approve its invoices or payments; enforced by the roles in the database.
- **AP ageing report:** open bills by due-date bucket (current, 1–30, 31–60, 61–90, 90+ days), per supplier and currency.
- **Purchase orders and 3-way matching:** invoice ↔ PO ↔ goods receipt, with quantity and price tolerances.
- **Structured e-invoices:** Factur-X/ZUGFeRD and UBL/CII parsed directly, without OCR.
- **Home-currency conversion of invoices:** each invoice's amount in USD at validation (today ECB rates are used for bank matching only).
- **Early-payment discounts:** read terms such as "2/10 net 30" and flag bills worth paying early.

## Copyright

Copyright (c) 2026 David Michel. All rights reserved.
