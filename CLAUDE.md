# AP Automation: project brief for Claude Code

## Who I am and what this is
I'm David Michel, a freelancer with a finance background (audit, financial control) and full-stack experience. This repo is a **portfolio project**: a working, public demo of end-to-end **accounts payable (AP) invoice processing**, shown to clients worldwide (Upwork, Malt, EU/FR platforms). Quality, robustness and clear documentation matter more than feature count.

Work step by step. **Propose a short plan before each major step and wait for my OK.** Explain choices briefly. I don't want to write much code myself, but I want to understand the design.

## Architecture (every layer is pluggable)
Intake channels → **common invoice format** → shared processing core → bank connector → accounting connector → log/notify

- **Channels:** Email to `invoices@davidmichel.dev` (an alias of my Google Workspace account `david@davidmichel.dev`; process ONLY emails addressed to the alias); Google Drive folder `AP Automation/Inbox`; later, a web upload form (demo only).
- **Google auth:** one **Internal** OAuth app (Google Workspace, so tokens don't expire after 7 days), used for both Gmail and Drive. No service account.
- **Extraction:** structured e-invoices (Factur-X/ZUGFeRD, UBL/CII) are parsed directly (Day 4). PDFs and scans go to **Mistral OCR / Document AI** (`mistral-ocr-latest`, with a JSON-schema annotation for structured output; check the current Mistral docs). **No LLM fallback for now**: if validation fails, the invoice goes to human review. Keep a slot in the design for a fallback provider later.
- **Common invoice format (country-neutral, worldwide):** supplier name, supplier tax ID, invoice number, invoice and due dates (ISO 8601; ambiguous dates are flagged, not guessed), ISO 4217 currency, line items, generic tax lines (VAT/GST/sales tax/withholding/none), totals, converted amount in the home currency (USD, via the Frankfurter/ECB API, Day 4), original-language text kept, confidence score.
- **Core checks:** required fields present, line items + tax = total, duplicate check (supplier + invoice number), confidence threshold.
- **Bank (Day 2):** Plaid sandbox (US). Matching rules: amount tolerance, date window, fuzzy supplier name. `/sandbox/transactions/create` exists for custom test transactions.
- **Accounting (Day 2):** QuickBooks Online sandbox (US, Plus, **home currency USD, multi-currency ON**). Flow: vendor (find or create) → bill + PDF attachment → payment if matched; unmatched → unpaid bill, re-checked on later runs.
- **Routing:** auto-post only if all checks pass; otherwise human review (approve/correct/reject); unreadable or non-invoice → rejected with a reason and logged.
- **Security rule:** the LLM only *extracts data*. All approval decisions are deterministic code (protection against prompt injection in uploaded documents).

## Stack
- **n8n Community Edition** (self-hosted, Docker)
- **PostgreSQL** (n8n database + an `ap` schema for invoices, matches, review queue, error log)
- **Python service** (FastAPI, in Docker) for parsing/validation helpers: `pypdf`/`pdfplumber`, later `factur-x` and a UBL/CII parser
- **Docker Compose** for everything; later deployed to my OVH VPS (Ubuntu 24.04, 4 GB RAM) behind **Caddy** at `n8n.davidmichel.dev`

## Environment and constraints
- **Local dev machine:** Ubuntu 22.04, Docker 29. Ports **5432/5433/5434 and 3306 are already used** on the host. Port 5678 (n8n) is free. Don't publish Postgres to the host unless needed; if needed, use port **5440**. RAM is limited (~3 GB free), so keep the containers lean.
- **Secrets:** everything goes in `.env` (gitignored) with a committed `.env.example`. **Never commit secrets, n8n credentials or OAuth tokens.** Exported n8n workflow JSON must contain no credentials.
- **Git:** set this repo's commit email to `david@davidmichel.dev` (repo-level config only). Remote: `github.com/dmichel-dev/ap-automation` (private for now).
- **OAuth redirect URI** (already registered in Google and Intuit): `http://localhost:5678/rest/oauth2-credential/callback`
- **Test data:** fake invoices only, from **fictional companies** (never real company names or logos), in several languages, currencies and writing systems. Each test file has an expected result, so the outcome can be checked automatically.

## Decisions taken (2026-11-22)
- **Python core** (`services/core`): `POST /invoices/process` does extract → normalize → checks → duplicate lookup → persist, in one DB transaction, and returns the status. **Routing happens in n8n** after that response, as visible branches `ready` / `needs_review` / `rejected`.
- **From Day 2, integrations are n8n nodes** (Plaid, QuickBooks, notifications, daily summary). The canvas must show n8n skills, so don't move these into Python.
- **Confidence score** is computed by code (field presence, arithmetic, format validity), never self-reported by the model.
- **Tables:** `invoices`, `invoice_lines`, `invoice_tax_lines`, `review_queue`, `processing_log`.
- **Duplicates:** same file hash → `rejected`; same supplier + invoice number with a different file → `needs_review`.
- **Routing principle:** `rejected` only when a human could do nothing with it (not an invoice, unreadable, exact duplicate file). Everything a human can check or fix (wrong sign, missing field, totals mismatch…) → `needs_review`.
- **Ambiguous dates:** `needs_review`, with a suggested date from `supplier_country` (US = month first, CA = no suggestion, others = day first) for one-click confirmation.
- **Confidence** = OCR page average × required-field completeness; both parts stored. **Threshold 0.85 is provisional: calibrate it on the test set on Day 3.**
- **Totals check** includes discount, shipping, fee and rounding lines (table rows or totals-section adjustments).
- **Test emails:** sent from the connected Gmail connector, **only to `invoices@davidmichel.dev`**, and each one is shown to David before sending.
- **Git:** commit and **push to GitHub after each step**.
- **Releases:** a milestone is one completed Day. Tag it (`v0.1.0` = Day 1 … `v0.7.0` = Day 7; `v1.0.0` when the repo goes public), push the tag, and create a **draft** GitHub release with notes for clients (what it does, how it was verified, known limits). Never publish a release: David does that himself when the repo goes public.
- **Day 3 note:** emails to `invoices@` without a valid attachment must be logged and labelled, never silently ignored. (Basic version done on Day 1: `AP/Rejected` + log.)
- **Day 3 note:** a bank-details change request must raise a **fraud alert** (reason `possible_bank_detail_change`), not just be rejected as `not_invoice`.
- **Day 3 note:** the daily automated end-to-end test resends the same files, so it needs isolation (e.g. a test channel or cleanup); otherwise every run after the first is `duplicate_file`.
- **Day 4 note:** the test set must include **Arabic (RTL)** and **Hindi (Devanagari)** invoices.

## Day 1 scope (today)
1. Repo structure, `docker-compose.yml` (n8n + Postgres + Python service), `.env.example`, `.gitignore`, README skeleton.
2. Postgres `ap` schema: `invoices`, `invoice_lines`, `review_queue`, `processing_log`.
3. Email channel: Gmail trigger that only takes emails to `invoices@davidmichel.dev` with PDF/image attachments, and labels them as processed.
4. Mistral OCR extraction → common invoice format (JSON schema).
5. Validation + duplicate check → status `ready` / `needs_review` / `rejected`, all logged.
6. 3–5 sample test invoices (fictional), run end to end.
7. Export the workflow JSON (no credentials) and commit.

## Later days (for context only, don't build yet)
Day 2: Plaid + QuickBooks. Day 3: human review, error handling, daily summary, **daily automated end-to-end test with alert**, test set of 10. Day 4: Drive channel, Factur-X/UBL, multi-currency/FX, test set of 20. Day 5: deploy to VPS (Caddy, HTTPS, firewall). Day 6: public demo page (upload, `invoices+CODE@` email, Drive drop), live tracking page, privacy notice, rate limits, file deletion. Day 7: video, README, architecture diagram, case study.
