# AP Automation

End-to-end **accounts payable invoice processing**, from intake to accounting: multi-channel, multilingual and multi-currency.
Built with **n8n**, **Mistral OCR**, **PostgreSQL** and a small **Python (FastAPI)** core service. Plaid and QuickBooks Online come on Day 2.

> Status: work in progress (Day 1 of 7). All test invoices come from fictional companies.

## How it works

```
Intake channels ──► common invoice format ──► core checks ──► routing ──► bank match ──► accounting ──► log / notify
(Email, Drive,       (country-neutral JSON)    (required fields,  ready /       (Plaid)        (QuickBooks)
 upload form)                                   totals, duplicates, needs_review /
                                                confidence)         rejected
```

- **Channels** are pluggable. Each one hands files to the same core endpoint.
- **The LLM only extracts data.** Every approval decision is deterministic, unit-tested code, which protects against prompt injection hidden in uploaded documents.
- Anything uncertain (failed check, ambiguous date, low confidence, possible duplicate) goes to **human review**. Nothing is guessed.

## Stack

| Component | Role |
|---|---|
| n8n Community Edition | Orchestration: intake triggers, routing, integrations |
| Python core (FastAPI) | Extraction, normalization, validation, persistence |
| Mistral OCR (`mistral-ocr-latest`) | PDF/scan text and structured-data extraction |
| PostgreSQL 16 | n8n database + `ap` schema (invoices, lines, taxes, review queue, log) |
| Docker Compose | Runs everything locally and on the VPS |

## Quick start

```bash
cp .env.example .env        # then fill in the values (openssl rand -hex 32 for secrets)
docker compose up -d --build
open http://localhost:5678  # n8n
```

## Repository layout

```
docker-compose.yml   .env.example
db/init/             Postgres roles and ap schema (run on first start)
services/core/       FastAPI core service
n8n/workflows/       Exported n8n workflows (no credentials)
test-data/invoices/  Fictional test invoices + expected results
scripts/             Export and end-to-end test scripts
docs/                Architecture and data-format documentation
```

## Roadmap

- [ ] Day 1: Email intake, Mistral OCR extraction, validation, duplicate check
- [ ] Day 2: Plaid bank matching, QuickBooks bills and payments
- [ ] Day 3: Human review, error handling, daily summary, daily end-to-end test
- [ ] Day 4: Google Drive intake, Factur-X/UBL e-invoices, FX conversion
- [ ] Day 5: VPS deployment (Caddy, HTTPS)
- [ ] Day 6: Public demo page
- [ ] Day 7: Video, architecture diagram, case study
