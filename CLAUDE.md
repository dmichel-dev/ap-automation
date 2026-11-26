# AP Automation: project brief for Claude Code

## Who I am and what this is
I'm David Michel, a freelancer with a finance background (audit, financial control) and full-stack experience. This repo is a **portfolio project**: a working, public demo of end-to-end **accounts payable (AP) invoice processing**, shown to clients worldwide (Upwork, Malt, EU/FR platforms). Quality, robustness and clear documentation matter more than feature count.

Work step by step. **Propose a short plan before each major step and wait for my OK.** Explain choices briefly. I don't want to write much code myself, but I want to understand the design.

## Architecture (every layer is pluggable)
Intake channels → **common invoice format** → shared processing core → bank connector → accounting connector → log/notify

- **Channels:** Email to `invoices@davidmichel.dev` (an alias of my Google Workspace account `david@davidmichel.dev`; process ONLY emails addressed to the alias); Google Drive folder `AP Automation/Inbox`; later, a web upload form (demo only).
- **Google auth:** one **Internal** OAuth app (Google Workspace, so tokens don't expire after 7 days), used for both Gmail and Drive. No service account.
- **Extraction:** structured e-invoices (Factur-X/ZUGFeRD, UBL/CII) are parsed directly (after go-live, not before). PDFs and scans go to **Mistral OCR / Document AI** (`mistral-ocr-latest`, with a JSON-schema annotation for structured output; check the current Mistral docs). **No LLM fallback for now**: if validation fails, the invoice goes to human review. Keep a slot in the design for a fallback provider later.
- **Common invoice format (country-neutral, worldwide):** supplier name, supplier tax ID, invoice number, invoice and due dates (ISO 8601; ambiguous dates are flagged, not guessed), ISO 4217 currency, line items, generic tax lines (VAT/GST/sales tax/withholding/none), totals, converted amount in the home currency (USD; **not planned before go-live**: ECB rates via Frankfurter are used for foreign-currency bank matching only), original-language text kept, confidence score.
- **Core checks:** required fields present, line items + tax = total, duplicate check (supplier + invoice number), confidence threshold.
- **Bank (Day 2):** Plaid sandbox (US). Matching rules: amount tolerance, date window, fuzzy supplier name. `/sandbox/transactions/create` exists for custom test transactions.
- **Accounting (Day 2):** QuickBooks Online sandbox (US, Plus, **home currency USD, multi-currency ON**). Flow: vendor (find or create) → bill + PDF attachment → payment if matched; unmatched → unpaid bill, re-checked on later runs.
- **Routing:** auto-post only if all checks pass; otherwise human review (approve/correct/reject); unreadable or non-invoice → rejected with a reason and logged.
- **Security rule:** the LLM only *extracts data*. All approval decisions are deterministic code (protection against prompt injection in uploaded documents).

## Stack
- **n8n Community Edition** (self-hosted, Docker)
- **PostgreSQL** (n8n database + an `ap` schema for invoices, matches, review queue, error log)
- **Python service** (FastAPI, in Docker) for parsing/validation helpers: `pypdf`/`pdfplumber`; `factur-x` and a UBL/CII parser after go-live
- **Docker Compose** for everything; later deployed to my OVH VPS (Ubuntu 24.04, 4 GB RAM) behind **Caddy** at `n8n.davidmichel.dev`

## Coding conventions
- **DRY:** one implementation per rule or helper. Shared code goes in a module (e.g. `n8n/nodes.py` for the workflow builders, `tests/conftest.py` for test helpers), never copied.
- **Small, typed functions:** full type hints (mypy strict on `services/core/app`), cyclomatic complexity ≤ 10, one job per function with a name that says it.
- **Business rules live only in the Python core:** validation, routing, matching, posting content, what an accounting answer means, retry and review policy, email screening. n8n triggers, calls services and routes on the core's answers; its Code nodes only reshape data and never decide. A rule is never duplicated in a Code node.
- **Checks before every commit:** `pre-commit` runs ruff (lint + format), mypy (in the core's test image) and a check that each committed workflow JSON equals its builder's output. All tests must stay green.
- **Refactors don't change behaviour:** prove it (old vs new on stored real data where possible), and say so in the commit.
- **Commit messages:** Conventional Commits with a scope, describing the change: `feat(review): …`, `fix(posting): …`, `docs(readme): …`, `refactor(core): …`, `test(e2e): …`, `ci: …`, `chore: …`. Never "Day N" or "step N".
- **No internal details** in code comments, docs, test data or workflow notes: no "Day N" milestones, no machine-specific paths or local setup. Describe what the code does and why.
- **CI:** GitHub Actions (`.github/workflows/ci.yml`) runs ruff, mypy and the tests on every push; it must stay green. A new push cancels the previous run on the same branch (concurrency group), and pushes that only change Markdown/docs (`**.md`, `docs/**`, `LICENSE`) skip CI.
- **Local checks for docs-only changes** (Markdown, CLAUDE.md): the pre-commit hooks are enough; don't run the full test suite.
- **Copyright footer:** every web page served (review page, demo pages, tracking page) ends with the footer "© David Michel" linking to `https://davidmichel.dev`, from a shared base template, with a test. The README ends with "Copyright (c) 2026 David Michel. All rights reserved."
- **Presentable n8n canvases:** clear node names, a sticky note per section explaining it, a clean layout, all generated by the builders (never hand-edited in n8n only).

## Tests and CI
- **Targeted tests locally:** `docker compose run --rm core-test pytest -q tests/test_x.py -k name`. The full suite runs in CI; run it locally only before a release tag or a deploy.
- **One test session at a time:** never run two at once (they share the `ap_test` database).
- **After a push:** run `gh run watch <run-id> --exit-status` in the background (ID from `gh run list --commit <sha>`) and carry on. Wait in the foreground only before a tag or a deploy.

## Reading the code
- Read the workflow builders (`n8n/build_*.py`, `n8n/nodes.py`), not `n8n/workflows/*.json`. For large modules (e.g. `accounting.py`), search and read line ranges, not the whole file.

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

## Later days (for context only, don't build yet)
Day 5: deploy to the VPS (Caddy, HTTPS, firewall). Day 6: public demo page (sample, upload, `invoices+CODE@` email), live tracking page, privacy notice, rate limits, file deletion.
Day 7: video, README, architecture diagram, case study. After go-live: Factur-X/UBL.

## Before publication
Before the repo goes public, follow [docs/pre-publication.md](docs/pre-publication.md) (history rewrite, signed commits and tags, secret scan, license).
