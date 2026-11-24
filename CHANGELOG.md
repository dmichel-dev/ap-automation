# Changelog

All notable changes to this project. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- **Foreign-currency bank matching:** a bill in EUR, JPY, GBP… is matched with a payment from the USD bank account at the ECB reference rate of the payment date (Frankfurter API, ECB only, fetched by the bank workflow), within 3 %. The bill payment settles the whole bill at the bank's actual rate (bank amount ÷ bill amount, at the 7 decimals QuickBooks keeps), and QuickBooks posts the exchange gain or loss. An exchange difference is never written off. A payment outside the tolerance, or a currency without a usable rate (e.g. AED, SAR), goes to review with the expected and actual amounts.
- **Daily summary: bank amounts not booked to the cent.** On a foreign-currency bill over 100,000 units, QuickBooks' 7-decimal rate can leave the bank account a few cents off the bank amount; each case is listed (bill, bank amount, amount booked, gap) to be fixed by a journal entry. A documented known limit.

### Changed

- Bills in another currency than the bank account are no longer left aside (`fx_match_pending`): they are matched (rule version `m2`). Rules for bills in the bank account's currency are unchanged.

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

[0.3.0]: https://github.com/dmichel-dev/ap-automation/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/dmichel-dev/ap-automation/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/dmichel-dev/ap-automation/releases/tag/v0.1.0
