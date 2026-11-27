# How it works

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
- **A public demo** at demo.davidmichel.dev: one click starts a 60-minute session, then a visitor sends one of 7 fictional samples (six languages, three scripts, four currencies), their own file, or an email to the session's own address, and sees each outcome in the review page's words, with the QuickBooks bill a ready invoice would create (nothing is posted). Everything runs in the demo's own scope and session, with per-address limits and an OCR budget. When the session ends, its data, its emails (permanently) and its n8n run records are deleted ([demo](demo.md)).
- **Failures are retried or handed to a person, never lost.** A temporary failure (OCR service down, core restarting) labels the email AP/Error, and a re-check every 15 minutes tries it again (5 attempts over about 17 hours). A permanent one (a file the OCR service refuses), or the last failed attempt, is given up and reported. An AP/Error email the core never heard of (it was down when the email failed) is picked up too. Failed workflow runs are logged by an error workflow. Details: [errors and retries](operations.md#errors-and-retries).
- **A daily summary email** lists fraud alerts, invoices received and posted, payments, write-offs, the touchless rate (documents posted with no review), open review items and intake problems, plus the result of the **daily end-to-end test** that sends four test emails through the real pipeline each morning and alerts at once if anything fails ([operations](operations.md#daily-summary)).
- **The confidence threshold was checked against real results.** The score measures how well the page was read, not whether the data is right; the only wrong extraction so far scored 0.982 and was caught by the totals check ([calibration](calibration.md)).
- **Safe to re-run.** Every write to QuickBooks is reserved in the database first, searched before it is created and sent with a fixed request ID, so a crash or a retry never creates a second bill, payment or write-off. A workflow can be started at once with a signed **Run now** request (HMAC with a timestamp, single use, rate-limited); the secret never reaches n8n.

## Components

| Component | Role |
|---|---|
| n8n Community Edition | Orchestration: intake triggers, routing, integrations |
| Python core (FastAPI) | Extraction, normalization, validation, persistence, posting content, bank matching, every decision |
| Review page (FastAPI + Jinja2, own container) | Sign-in, review list, document pages, approve / correct / reject, bank-match and payment-difference decisions, retries; loopback only for reviewer accounts |
| Mistral OCR (`mistral-ocr-latest`) | PDF/scan text and structured-data extraction |
| PostgreSQL 17 | n8n database + `ap` schema (invoices, lines, taxes, review queue, log, postings, bank transactions, matches) |
| QuickBooks Online (sandbox, US, multi-currency) | Vendors, bills and vendor credits with the original file, bill payments, write-offs of payment differences |
| Plaid (sandbox) | Bank transactions (`/transactions/sync`) for payment matching |
| Docker Compose | Runs everything locally and on the server |

More: [invoice format](common-invoice-format.md), [checks and routing](validation.md), [human review](review.md), [Drive intake](operations.md#google-drive-intake), [review page security](security.md), [data model](data-model.md), [bank matching](matching.md), [Plaid and QuickBooks facts verified live](integrations.md), [operations](operations.md), [confidence calibration](calibration.md), [deployment](deployment.md), [public demo](demo.md).
