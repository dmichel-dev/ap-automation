# Development

## Code quality

Every commit runs [pre-commit](https://pre-commit.com) (`.pre-commit-config.yaml`): **ruff** (lint and format), **mypy** (strict on the core service, run in the core's own image: `docker compose run --rm lint`) and a check that each committed workflow JSON is exactly what its builder generates. Install once with `pre-commit install`. Conventions: [`CLAUDE.md`](../CLAUDE.md#coding-conventions); the main one is that business rules live only in the Python core, and n8n Code nodes only reshape data. Tests: [testing](testing.md).

## Updating the n8n workflows

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
