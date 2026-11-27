# Roadmap

What each version changed in detail: [CHANGELOG](../CHANGELOG.md).

- [x] v0.1.0: Email intake, Mistral OCR extraction, validation, duplicate check
- [x] v0.2.0: QuickBooks bills, Plaid bank matching and bill payments
- [x] v0.3.0: Human review, fraud alert, error handling, daily summary, daily end-to-end test
- [x] v0.4.0: Google Drive intake, foreign-currency bank matching, test set of 20 invoices and confidence calibration
- [x] v0.5.0: Server deployment (Caddy, HTTPS, backups, n8n reached through an SSH tunnel only)
- [x] v0.6.0: Public demo: samples, uploads and email, session page with QuickBooks bill preview, privacy notice, limits, deletion
- [ ] Video, architecture diagram, case study

## After go-live (documented, not built)

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
