"""AP core service: extraction, validation and persistence for every intake channel.

Endpoints:
  GET  /health            liveness + database check (no auth)
  POST /invoices/process  file + source → extract, check, store; returns the status for n8n routing
  POST /extract           file → common invoice format (no persistence; tests and debugging)

Day 2 (n8n does the QuickBooks/Plaid calls; the core keeps state and rules):
  GET  /invoices/{id}/file               original file, for the accounting attachment
  POST /invoices/{id}/exclude-from-posting
  POST /postings/claim                   invoices to post now, with the document to send (leased)
  POST /postings/{invoice_id}/result     outcome of a posting / attachment
  POST /postings/{invoice_id}/retry      operator: fresh attempts for a failed posting (logged)
  PUT  /vendor-links                     supplier + currency → accounting vendor
  PUT  /bank/accounts/{item_key}         the bank account used for matching
  GET  /bank/sync-state/{item_key}       cursor for Plaid /transactions/sync
  POST /bank/sync-page/{item_key}        one page of /transactions/sync
  POST /matching/run/{item_key}          deterministic matching → payments to record (leased)
  POST /matches/{id}/payment             payment recorded in the accounting system
"""

import hmac
import logging
from datetime import date, datetime
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

import psycopg
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from pydantic import BaseModel, Field

from . import accounting, db, files, matching, processing
from .config import get_settings
from .extraction import build_common_invoice
from .models import CommonInvoice
from .providers import ExtractionProvider, MistralOcrProvider, ProviderError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

app = FastAPI(title="AP Automation core", version="0.1.0")
_bearer = HTTPBearer(auto_error=False)


def require_token(creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)]) -> None:
    expected = get_settings().core_api_token.get_secret_value()
    if creds is None or not hmac.compare_digest(creds.credentials.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="invalid or missing bearer token")


@lru_cache
def get_provider() -> ExtractionProvider:
    s = get_settings()
    return MistralOcrProvider(s.mistral_api_key.get_secret_value(), model=s.mistral_ocr_model)


@app.exception_handler(files.UnsupportedFile)
def _unsupported_file(_: Request, exc: files.UnsupportedFile) -> JSONResponse:
    status = 413 if exc.code == "file_too_large" else 415
    return JSONResponse({"error": exc.code, "message": str(exc)}, status_code=status)


@app.exception_handler(ProviderError)
def _provider_error(_: Request, exc: ProviderError) -> JSONResponse:
    # 503 = temporary, n8n may retry later; 502 = permanent provider problem
    status = 503 if exc.retryable else 502
    return JSONResponse({"error": f"provider_{exc.kind}", "message": str(exc), "retryable": exc.retryable},
                        status_code=status)


@app.get("/health")
def health() -> JSONResponse:
    """Liveness + database reachability. Unauthenticated, returns no sensitive data."""
    settings = get_settings()
    try:
        with psycopg.connect(settings.ap_database_url.get_secret_value(), connect_timeout=3) as conn:
            conn.execute("SELECT 1")
        db = "ok"
    except psycopg.Error:
        db = "unreachable"
    status = 200 if db == "ok" else 503
    return JSONResponse({"status": "ok" if status == 200 else "degraded", "db": db}, status_code=status)


def get_processing_config() -> processing.Config:
    s = get_settings()
    return processing.Config(
        database_url=s.ap_database_url.get_secret_value(), storage_dir=Path(s.file_storage_dir),
        retention_days=s.file_retention_days, confidence_threshold=s.confidence_threshold,
    )


@app.post("/invoices/process", response_model=processing.ProcessOutcome, dependencies=[Depends(require_token)])
def process_invoice(
    file: Annotated[UploadFile, File(description="PDF, PNG, JPEG, WebP or TIFF")],
    channel: Annotated[Literal["email", "drive", "upload"], Form()],
    source_ref: Annotated[str, Form(min_length=1, max_length=500, description="e.g. gmail:<messageId>:<attachmentId>")],
    provider: Annotated[ExtractionProvider, Depends(get_provider)],
    cfg: Annotated[processing.Config, Depends(get_processing_config)],
    received_at: Annotated[datetime | None, Form()] = None,
    n8n_execution_id: Annotated[str | None, Form(max_length=100)] = None,
) -> processing.ProcessOutcome:
    """The shared core for every channel: extract → normalize → check → store. Idempotent per source_ref."""
    content = file.file.read(files.MAX_FILE_BYTES + 1)
    req = processing.ProcessRequest(
        content=content, file_name=(file.filename or "unnamed")[:255], channel=channel, source_ref=source_ref,
        received_at=received_at, execution_id=n8n_execution_id,
    )
    return processing.process(req, provider, cfg)


@app.post("/extract", response_model=CommonInvoice, dependencies=[Depends(require_token)])
def extract(
    file: Annotated[UploadFile, File(description="PDF, PNG, JPEG, WebP or TIFF")],
    provider: Annotated[ExtractionProvider, Depends(get_provider)],
) -> CommonInvoice:
    content = file.file.read(files.MAX_FILE_BYTES + 1)
    prepared = files.prepare(content)
    result = provider.extract(prepared.content, prepared.mime_type)
    return build_common_invoice(result, provider.name)


# =====================================================================================
# Day 2: accounting postings, bank sync, matching
# =====================================================================================
def get_accounting_config() -> accounting.AccountingConfig:
    s = get_settings()
    return accounting.AccountingConfig(
        database_url=s.ap_database_url.get_secret_value(), storage_dir=Path(s.file_storage_dir),
        home_currency=s.home_currency, expense_account_id=s.qbo_expense_account_id,
        tax_account_id=s.qbo_tax_account_id, bank_account_id=s.qbo_bank_account_id,
        max_attempts=s.posting_max_attempts, lease_minutes=s.posting_lease_minutes,
        qbo_base=f"{s.qbo_api_host.rstrip('/')}/v3/company/{s.qbo_realm_id}" if s.qbo_realm_id else "",
    )


def get_rules() -> matching.Rules:
    s = get_settings()
    return matching.Rules(
        abs_tolerance=Decimal(s.match_amount_abs_tolerance), pct_tolerance=Decimal(s.match_amount_pct_tolerance),
        days_before_invoice=s.match_days_before_invoice, days_after_due=s.match_days_after_due,
        default_terms_days=s.match_default_terms_days, min_name_similarity=s.match_name_min_similarity,
    )


AcctCfg = Annotated[accounting.AccountingConfig, Depends(get_accounting_config)]
Auth = [Depends(require_token)]


@app.exception_handler(accounting.NotFound)
def _not_found(_: Request, exc: accounting.NotFound) -> JSONResponse:
    return JSONResponse({"error": "not_found", "message": str(exc)}, status_code=404)


@app.exception_handler(accounting.Conflict)
def _conflict(_: Request, exc: accounting.Conflict) -> JSONResponse:
    return JSONResponse({"error": "conflict", "message": str(exc)}, status_code=409)


def _json(row: dict | None) -> dict | None:
    """DB row → JSON-safe dict (Decimal as exact string, dates as ISO)."""
    if row is None:
        return None
    def conv(v):
        if isinstance(v, (Decimal, UUID)):
            return str(v)
        return v.isoformat() if isinstance(v, (date, datetime)) else v
    return {k: conv(v) for k, v in row.items()}


@app.get("/invoices/{invoice_id}/file", dependencies=Auth)
def invoice_file(invoice_id: str, cfg: AcctCfg) -> FileResponse:
    with db.connect(cfg.database_url) as conn:
        path, mime, name = accounting.stored_file(conn, cfg.storage_dir, invoice_id)
    return FileResponse(path, media_type=mime, filename=name)


class Exclusion(BaseModel):
    reason: Literal["superseded_test_data", "manual"]
    note: str | None = Field(default=None, max_length=200)


@app.post("/invoices/{invoice_id}/exclude-from-posting", dependencies=Auth)
def exclude_from_posting(invoice_id: str, body: Exclusion, cfg: AcctCfg) -> dict:
    with db.connect(cfg.database_url) as conn:
        return _json(accounting.exclude_from_posting(conn, invoice_id, body.reason, body.note))


@app.post("/postings/claim", dependencies=Auth)
def claim_postings(cfg: AcctCfg, limit: int = 10) -> dict:
    with db.connect(cfg.database_url) as conn:
        items = accounting.claim_postings(conn, cfg, max(1, min(limit, 50)))
    return {"count": len(items), "qbo_base": cfg.qbo_base, "minorversion": "75", "items": items}


class PostingResult(BaseModel):
    outcome: Literal["posted", "attached", "failed"]
    external_id: str | None = Field(default=None, max_length=50)
    vendor_external_id: str | None = Field(default=None, max_length=50)
    exchange_rate: Decimal | None = Field(default=None, gt=0)
    exchange_rate_date: date | None = None
    attachment_external_id: str | None = Field(default=None, max_length=50)
    error_code: str | None = Field(default=None, max_length=100)
    error_message: str | None = Field(default=None, max_length=2000)
    retryable: bool = True
    execution_id: str | None = Field(default=None, max_length=100)


@app.post("/postings/{invoice_id}/result", dependencies=Auth)
def posting_result(invoice_id: str, body: PostingResult, cfg: AcctCfg) -> dict:
    if body.outcome == "posted" and not (body.external_id and body.vendor_external_id):
        raise HTTPException(422, "posted requires external_id and vendor_external_id")
    if body.outcome == "attached" and not body.attachment_external_id:
        raise HTTPException(422, "attached requires attachment_external_id")
    with db.connect(cfg.database_url) as conn:
        posting, to_delete = accounting.record_result(conn, cfg, invoice_id, body.model_dump())
    accounting.delete_stored_file(cfg.storage_dir, to_delete)      # after COMMIT
    return _json(posting)


class Retry(BaseModel):
    note: str | None = Field(default=None, max_length=200)


@app.post("/postings/{invoice_id}/retry", dependencies=Auth)
def posting_retry(invoice_id: str, body: Retry, cfg: AcctCfg) -> dict:
    with db.connect(cfg.database_url) as conn:
        return _json(accounting.retry_posting(conn, invoice_id, body.note))


class VendorLink(BaseModel):
    supplier_key: str = Field(min_length=1, max_length=200)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    external_id: str = Field(min_length=1, max_length=50)
    display_name: str = Field(min_length=1, max_length=100)


@app.put("/vendor-links", dependencies=Auth)
def vendor_link(body: VendorLink, cfg: AcctCfg) -> dict:
    with db.connect(cfg.database_url) as conn:
        return _json(accounting.link_vendor(conn, body.supplier_key, body.currency, body.external_id,
                                            body.display_name))


class BankAccount(BaseModel):
    account_id: str = Field(min_length=1, max_length=100)
    account_currency: str = Field(pattern=r"^[A-Z]{3}$")


@app.put("/bank/accounts/{item_key}", dependencies=Auth)
def bank_account(item_key: str, body: BankAccount, cfg: AcctCfg) -> dict:
    with db.connect(cfg.database_url) as conn:
        return _json(accounting.register_account(conn, item_key, body.account_id, body.account_currency))


@app.get("/bank/sync-state/{item_key}", dependencies=Auth)
def bank_sync_state(item_key: str, cfg: AcctCfg) -> dict:
    with db.connect(cfg.database_url) as conn:
        return _json(accounting.sync_state(conn, item_key))


class SyncPage(BaseModel):
    added: list[dict] = []
    modified: list[dict] = []
    removed: list[dict] = []
    next_cursor: str
    has_more: bool


@app.post("/bank/sync-page/{item_key}", dependencies=Auth)
def bank_sync_page(item_key: str, body: SyncPage, cfg: AcctCfg) -> dict:
    with db.connect(cfg.database_url) as conn:
        return accounting.store_sync_page(conn, item_key, body.added, body.modified, body.removed,
                                          body.next_cursor, body.has_more)


@app.post("/matching/run/{item_key}", dependencies=Auth)
def matching_run(item_key: str, cfg: AcctCfg, rules: Annotated[matching.Rules, Depends(get_rules)]) -> dict:
    with db.connect(cfg.database_url) as conn:
        return accounting.run_matching(conn, cfg, item_key, rules)


class PaymentRecorded(BaseModel):
    external_payment_id: str = Field(min_length=1, max_length=50)
    execution_id: str | None = Field(default=None, max_length=100)


@app.post("/matches/{match_id}/payment", dependencies=Auth)
def match_payment(match_id: int, body: PaymentRecorded, cfg: AcctCfg) -> dict:
    with db.connect(cfg.database_url) as conn:
        return _json(accounting.record_payment(conn, match_id, body.external_payment_id, body.execution_id))
