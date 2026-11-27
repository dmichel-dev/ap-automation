# AP Automation

End-to-end **accounts payable invoice processing**. Invoices arrive by email or Google Drive, in any language or currency. OCR reads them, deterministic code checks them, and they're posted to **QuickBooks Online** as bills with the original file attached. They're then matched against the bank account (**Plaid**) and paid. Anything uncertain goes to a person. Every test company is fictional.

**Live demo: [demo.davidmichel.dev](https://demo.davidmichel.dev)**: send a sample invoice, your own file or an email, and see what the pipeline decides and the QuickBooks bill it would create.

## How it works

```
Email / Drive ─► Mistral OCR ─► common invoice format ─► checks
                                                          ├─► ready ─► QuickBooks bill + file ─► bank match (Plaid) ─► payment
                                                          ├─► needs review ─► review page: approve / correct / reject
                                                          └─► rejected, with a reason
```

- **The LLM only extracts data.** Every approval decision is deterministic, tested code, so a prompt hidden in a document can't approve anything.
- **n8n orchestrates, a Python core decides.** n8n runs the triggers and the Gmail, Drive, QuickBooks and Plaid calls, and routes on the core's answers.
- **Nothing is guessed:** failed checks, ambiguous dates, low confidence and first invoices from a supplier all go to human review.

## Main controls

- **Fraud alert:** an invoice or letter announcing new bank details, or showing an account that differs from earlier invoices, is never posted automatically.
- **Duplicates:** the same file is rejected; the same supplier and invoice number in a different file goes to review.
- **Totals and taxes:** lines, discounts, shipping, VAT/GST/withholding and totals must add up.
- **Human review:** a review page shows the document next to what was read, and records who approved, corrected or rejected it.
- **Safe re-runs:** a crash or retry never creates a second bill or payment.

## Stack

n8n · Python (FastAPI) · PostgreSQL · Mistral OCR · QuickBooks Online · Plaid · Docker Compose · Caddy

## Run it locally

```bash
cp .env.example .env        # then fill in the values (openssl rand -hex 32 for secrets)
docker volume create ap-automation_pg17_data   # once: the database volume is external
docker compose up -d --build
open http://localhost:5678  # n8n
docker compose exec review-web python -m app.web.users add --email you@example.com --name "Your Name" --role admin --scope live
open http://localhost:8081  # review page
```

## Documentation

[How it works in detail](docs/overview.md) · [checks and routing](docs/validation.md) · [human review](docs/review.md) · [bank matching](docs/matching.md) · [tests and verification](docs/testing.md) · [public demo](docs/demo.md) · [security](docs/security.md) · [deployment](docs/deployment.md) · [development](docs/development.md) · [roadmap](docs/roadmap.md) · [CHANGELOG](CHANGELOG.md)

## Copyright

Copyright (c) 2026 David Michel. All rights reserved.
