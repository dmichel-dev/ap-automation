"""AP core service: extraction, validation and persistence for every intake channel.

Endpoints:
  GET  /health            liveness + database check (no auth)
  POST /intake/email/screen  email metadata → which attachments to process (the alias guard)
  POST /intake/failures      a failed email → retried later or given up (core decides)
  POST /intake/failures/due  emails under AP/Error → the ones the re-check processes now
  POST /intake/failures/retry  operator: fresh attempts for given-up emails (logged)
  GET  /intake/failures/summary  given up / retrying / recovered, for the daily summary
  POST /invoices/process  file + source → extract, check, store; returns the status for n8n routing
  POST /extract           file → common invoice format (no persistence; tests and debugging)

Daily end-to-end test (e2e_test.py; n8n puts the messages in Gmail and does the QuickBooks calls):
  POST /e2e/runs                      start a run
  GET  /e2e/qbo-objects               QuickBooks objects of test postings still to delete
  POST /e2e/qbo-objects/{id}/check    the object read back → delete / skip / keep (core decides)
  POST /e2e/qbo-objects/{id}/deleted  QuickBooks' answer to the delete
  POST /e2e/runs/{id}/reset           earlier test invoices deleted (scope e2e only) → the run's messages
  POST /e2e/runs/{id}/sent            the messages' Gmail IDs
  POST /e2e/runs/{id}/verify          what Gmail shows → every check, pass or fail, and the alert to send
  POST /e2e/runs/{id}/abort           the workflow could not finish: the run fails, with the alert

Accounting and bank (n8n does the QuickBooks/Plaid calls; the core keeps state and rules):
  GET  /invoices/{id}/file               original file, for the accounting attachment
  POST /invoices/{id}/exclude-from-posting
  POST /postings/claim                   invoices to post now, with the document to send (leased)
  POST /postings/{invoice_id}/lookup/{kind}  QuickBooks search result → next step (vendor / document)
  POST /postings/{invoice_id}/result     outcome of a posting / attachment (failures classified here)
  POST /postings/{invoice_id}/retry      operator: fresh attempts for a failed posting (logged)
  PUT  /vendor-links                     supplier + currency → accounting vendor
  PUT  /bank/accounts/{item_key}         the bank account used for matching
  GET  /bank/sync-state/{item_key}       cursor for Plaid /transactions/sync
  POST /bank/sync-page/{item_key}        one page of /transactions/sync
  POST /matching/run/{item_key}          deterministic matching → payments to record (leased)
  POST /matches/{id}/payment             payment recorded in the accounting system
  POST /matches/{id}/lookup              QuickBooks BillPayment search → reuse / create
  POST /matches/{id}/payment-failed      failure facts → retried or stopped (core decides)
"""

import hmac
import logging
from dataclasses import asdict
from datetime import date, datetime
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal

import psycopg
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from . import (
    accounting,
    actors,
    daily_summary,
    db,
    e2e_test,
    files,
    intake,
    intake_failures,
    matching,
    postings,
    processing,
    qbo_reference,
    review,
    run_now,
    settlement,
)
from .config import get_settings
from .extraction import build_common_invoice
from .models import CommonInvoice
from .providers import ExtractionProvider, MistralOcrProvider, ProviderError
from .tax_treatment import TaxTreatment
from .validation import Policy

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
    return JSONResponse(
        {"error": f"provider_{exc.kind}", "message": str(exc), "retryable": exc.retryable}, status_code=status
    )


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
    bank_key = s.bank_fingerprint_key.get_secret_value()
    if not bank_key:
        raise RuntimeError("BANK_FINGERPRINT_KEY is not set: see .env.example")
    return processing.Config(
        database_url=s.ap_database_url.get_secret_value(),
        storage_dir=Path(s.file_storage_dir),
        retention_days=s.file_retention_days,
        confidence_threshold=s.confidence_threshold,
        bank_fingerprint_key=bank_key.encode(),
        policy=Policy(
            home_currency=s.home_currency,
            cutoff_materiality=Decimal(s.cutoff_materiality),
            capex_threshold=Decimal(s.capex_threshold),
        ),
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
    email_text: Annotated[
        str | None, Form(max_length=intake.EMAIL_TEXT_MAX, description="subject + body; checked, never stored")
    ] = None,
    message_id: Annotated[str | None, Form(max_length=200, description="the email, for the re-check")] = None,
    scope: Annotated[Literal["live", "e2e"], Form(description="the screening's answer")] = "live",
) -> processing.ProcessOutcome:
    """The shared core for every channel: extract → normalize → check → store. Idempotent per source_ref."""
    content = file.file.read(files.MAX_FILE_BYTES + 1)
    req = processing.ProcessRequest(
        content=content,
        file_name=(file.filename or "unnamed")[:255],
        channel=channel,
        source_ref=source_ref,
        received_at=received_at,
        execution_id=n8n_execution_id,
        email_text=email_text,
        message_id=message_id or None,
        scope=scope,
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
# Accounting postings, bank sync, matching
# =====================================================================================
def get_accounting_config() -> accounting.AccountingConfig:
    s = get_settings()
    return accounting.AccountingConfig(
        database_url=s.ap_database_url.get_secret_value(),
        storage_dir=Path(s.file_storage_dir),
        home_currency=s.home_currency,
        withholding_account_id=s.qbo_withholding_account_id,
        tax_recoverable_account_id=s.qbo_tax_recoverable_account_id,
        tax_treatment=TaxTreatment.load(Path(s.tax_treatment_file) if s.tax_treatment_file else None),
        bank_account_id=s.qbo_bank_account_id,
        write_off_account_id=s.qbo_write_off_account_id,
        max_attempts=s.posting_max_attempts,
        lease_minutes=s.posting_lease_minutes,
        qbo_base=f"{s.qbo_api_host.rstrip('/')}/v3/company/{s.qbo_realm_id}" if s.qbo_realm_id else "",
    )


def get_rules() -> matching.Rules:
    s = get_settings()
    return matching.Rules(
        abs_tolerance=Decimal(s.match_amount_abs_tolerance),
        pct_tolerance=Decimal(s.match_amount_pct_tolerance),
        days_before_invoice=s.match_days_before_invoice,
        days_after_due=s.match_days_after_due,
        default_terms_days=s.match_default_terms_days,
        min_name_similarity=s.match_name_min_similarity,
    )


JsonDict = dict[str, Any]
AcctCfg = Annotated[accounting.AccountingConfig, Depends(get_accounting_config)]
Auth = [Depends(require_token)]


@app.exception_handler(accounting.NotFound)
def _not_found(_: Request, exc: accounting.NotFound) -> JSONResponse:
    return JSONResponse({"error": "not_found", "message": str(exc)}, status_code=404)


@app.exception_handler(accounting.Conflict)
def _conflict(_: Request, exc: accounting.Conflict) -> JSONResponse:
    return JSONResponse({"error": "conflict", "message": str(exc)}, status_code=409)


def _json(row: db.Row) -> JsonDict:
    """DB row → JSON-safe dict (Decimal as exact string, dates as ISO)."""
    out: JsonDict = db.jsonable(row)
    return out


@app.get("/invoices/{invoice_id}/file", dependencies=Auth)
def invoice_file(invoice_id: str, cfg: AcctCfg) -> FileResponse:
    with db.connect(cfg.database_url) as conn:
        path, mime, name = accounting.stored_file(conn, cfg.storage_dir, invoice_id)
    return FileResponse(path, media_type=mime, filename=name)


class Exclusion(BaseModel):
    reason: Literal["superseded_test_data", "manual"]
    note: str | None = Field(default=None, max_length=200)


@app.post("/invoices/{invoice_id}/exclude-from-posting", dependencies=Auth)
def exclude_from_posting(invoice_id: str, body: Exclusion, cfg: AcctCfg) -> JsonDict:
    with db.connect(cfg.database_url) as conn:
        return _json(accounting.exclude_from_posting(conn, invoice_id, body.reason, body.note))


QBO_MINOR_VERSION = "75"


def _qbo_target(cfg: accounting.AccountingConfig) -> JsonDict:
    """Where the workflow sends QuickBooks calls (company URL and API minor version)."""
    return {"qbo_base": cfg.qbo_base, "minorversion": QBO_MINOR_VERSION}


@app.get("/accounting/target", dependencies=Auth)
def accounting_target(cfg: AcctCfg) -> JsonDict:
    """Where the workflow sends QuickBooks calls, needed before the first one (preferences)."""
    return _qbo_target(cfg)


class Lookup(BaseModel):
    """A QuickBooks search response, passed through unchanged by the workflow."""

    status_code: int = Field(ge=0, le=599)
    body: Any = None
    vendor_id: str | None = Field(default=None, max_length=50)  # document lookups only


class Claim(BaseModel):
    preferences: Lookup  # QuickBooks GET /preferences response, passed through unchanged


@app.post("/postings/claim", dependencies=Auth)
def claim_postings(body: Claim, cfg: AcctCfg, limit: int = 10) -> JsonDict:
    close = postings.book_close(body.preferences.status_code, body.preferences.body)
    with db.connect(cfg.database_url) as conn:
        items = accounting.claim_postings(conn, cfg, close, max(1, min(limit, 50)))
    return {
        "count": len(items),
        **_qbo_target(cfg),
        "book_close": {
            "known": close.known,
            "closed_through": close.closed_through.isoformat() if close.closed_through else None,
        },
        "items": items,
    }


class FailureFacts(BaseModel):
    """What the workflow saw when a step failed; the core decides what it means (retry, stop, review)."""

    error_code: str | None = Field(default=None, max_length=100)  # a failure named by the workflow
    error_message: str | None = Field(default=None, max_length=2000)
    http_status: int | None = Field(default=None, ge=0, le=599)  # 0 = no response (network error)
    fault_code: str | None = Field(default=None, max_length=20)  # QuickBooks Fault.Error[0].code
    execution_id: str | None = Field(default=None, max_length=100)


class PostingResult(FailureFacts):
    outcome: Literal["posted", "attached", "failed"]
    external_id: str | None = Field(default=None, max_length=50)
    vendor_external_id: str | None = Field(default=None, max_length=50)
    exchange_rate: Decimal | None = Field(default=None, gt=0)
    exchange_rate_date: date | None = None
    attachment_external_id: str | None = Field(default=None, max_length=50)


@app.post("/postings/{invoice_id}/result", dependencies=Auth)
def posting_result(invoice_id: str, body: PostingResult, cfg: AcctCfg) -> JsonDict:
    if body.outcome == "posted" and not (body.external_id and body.vendor_external_id):
        raise HTTPException(422, "posted requires external_id and vendor_external_id")
    if body.outcome == "attached" and not body.attachment_external_id:
        raise HTTPException(422, "attached requires attachment_external_id")
    with db.connect(cfg.database_url) as conn:
        posting, to_delete = accounting.record_result(conn, cfg, invoice_id, body.model_dump())
    accounting.delete_stored_file(cfg.storage_dir, to_delete)  # after COMMIT
    return _json(posting)


class EmailAttachment(BaseModel):
    key: str = Field(pattern=r"^attachment_\d+$")
    mime_type: str | None = Field(default=None, max_length=200)
    file_name: str | None = Field(default=None, max_length=500)
    size: str | int | None = None


class EmailToScreen(BaseModel):
    recipients: list[str] = Field(default=[], max_length=500)
    delivered_to: str = Field(default="", max_length=2000)
    attachments: list[EmailAttachment] = Field(default=[], max_length=100)
    text: str = Field(default="", max_length=intake.EMAIL_TEXT_MAX)  # subject + body: checked, never stored
    subject: str = Field(default="", max_length=2000)  # names the end-to-end run, for test emails
    message_id: str | None = Field(default=None, max_length=200)  # for the re-check
    execution_id: str | None = Field(default=None, max_length=100)


@app.post("/intake/email/screen", dependencies=Auth)
def screen_email(body: EmailToScreen, cfg: AcctCfg) -> JsonDict:
    """Which attachments of an email go to processing (metadata and text; nothing is stored).
    An email with nothing to process is done: its re-check, if any, ends here."""
    screening = intake.screen_email(
        body.recipients,
        body.delivered_to,
        [intake.Attachment(a.key, a.mime_type, a.file_name, a.size) for a in body.attachments],
        get_settings().intake_email_alias,
        text=body.text,
        subject=body.subject,
    )
    if screening.scope != "e2e" and not (body.message_id and screening.route != "process"):
        return asdict(screening)  # nothing to look up or record
    with db.connect(cfg.database_url) as conn:
        screening = e2e_test.confirm_scope(conn, screening)
        if body.message_id and screening.route != "process":
            intake_failures.resolve(conn, "email", body.message_id, body.execution_id)
    return asdict(screening)


Channel = Literal["email", "drive", "upload"]


class IntakeFailure(BaseModel):
    """What the workflow saw when an email failed: the core decides what it means."""

    channel: Channel = "email"
    message_id: str = Field(min_length=1, max_length=200)
    stage: Literal["fetch", "screen", "process"]
    status_code: int | None = Field(default=None, ge=0, le=599)  # None or 0 = no answer
    error: str | None = Field(default=None, max_length=100)  # the core's error code, if it answered
    execution_id: str | None = Field(default=None, max_length=100)
    scope: Literal["live", "e2e"] | None = None  # the screening's answer, if the email got that far


@app.post("/intake/failures", dependencies=Auth)
def intake_failure(body: IntakeFailure, cfg: AcctCfg) -> JsonDict:
    with db.connect(cfg.database_url) as conn:
        return intake_failures.record(
            conn,
            body.channel,
            body.message_id,
            body.stage,
            body.status_code,
            body.error,
            body.execution_id,
            body.scope or "live",
        )


class FoundFailures(BaseModel):
    channel: Channel = "email"
    found: list[Annotated[str, Field(min_length=1, max_length=200)]] = Field(default=[], max_length=500)
    execution_id: str | None = Field(default=None, max_length=100)


@app.post("/intake/failures/due", dependencies=Auth)
def intake_failures_due(body: FoundFailures, cfg: AcctCfg) -> JsonDict:
    with db.connect(cfg.database_url) as conn:
        due = intake_failures.claim_due(conn, body.channel, body.found, body.execution_id)
    return {"count": len(due), "due": due}


class IntakeRetry(BaseModel):
    channel: Channel = "email"
    message_id: str | None = Field(default=None, max_length=200)
    error_code: str | None = Field(default=None, max_length=100)
    note: str | None = Field(default=None, max_length=200)


@app.post("/intake/failures/retry", dependencies=Auth)
def intake_failures_retry(body: IntakeRetry, cfg: AcctCfg) -> JsonDict:
    if not (body.message_id or body.error_code):
        raise HTTPException(422, "name an email (message_id) or an error code (error_code)")
    with db.connect(cfg.database_url) as conn:
        ids = intake_failures.retry(conn, body.channel, body.message_id, body.error_code, body.note)
    return {"count": len(ids), "retried": ids}


@app.get("/intake/failures/summary", dependencies=Auth)
def intake_failures_summary(cfg: AcctCfg, hours: int = 24) -> JsonDict:
    with db.connect(cfg.database_url) as conn:
        return intake_failures.summary(conn, max(1, min(hours, 24 * 31)))


@app.get("/summary/daily", dependencies=Auth)
def summary_daily(cfg: AcctCfg, hours: int = 24) -> JsonDict:
    """The daily summary email, ready to send: recipients, subject, text and HTML (plus the figures)."""
    with db.connect(cfg.database_url) as conn:
        return daily_summary.daily(conn, _summary_recipients(), max(1, min(hours, 24 * 31)))


@app.post("/postings/{invoice_id}/lookup/{kind}", dependencies=Auth)
def posting_lookup(invoice_id: str, kind: Literal["vendor", "document"], body: Lookup, cfg: AcctCfg) -> JsonDict:
    with db.connect(cfg.database_url) as conn:
        decision = accounting.lookup_decision(conn, invoice_id, kind, body.status_code, body.body, body.vendor_id)
    return asdict(decision)


class Retry(BaseModel):
    note: str | None = Field(default=None, max_length=200)


@app.post("/postings/{invoice_id}/retry", dependencies=Auth)
def posting_retry(invoice_id: str, body: Retry, cfg: AcctCfg) -> JsonDict:
    with db.connect(cfg.database_url) as conn:
        return _json(accounting.retry_posting(conn, invoice_id, body.note))


class VendorLink(BaseModel):
    supplier_key: str = Field(min_length=1, max_length=200)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    external_id: str = Field(min_length=1, max_length=50)
    display_name: str = Field(min_length=1, max_length=100)


@app.put("/vendor-links", dependencies=Auth)
def vendor_link(body: VendorLink, cfg: AcctCfg) -> JsonDict:
    with db.connect(cfg.database_url) as conn:
        return _json(
            accounting.link_vendor(conn, body.supplier_key, body.currency, body.external_id, body.display_name)
        )


class BankAccount(BaseModel):
    account_id: str = Field(min_length=1, max_length=100)
    account_currency: str = Field(pattern=r"^[A-Z]{3}$")


@app.put("/bank/accounts/{item_key}", dependencies=Auth)
def bank_account(item_key: str, body: BankAccount, cfg: AcctCfg) -> JsonDict:
    with db.connect(cfg.database_url) as conn:
        return _json(accounting.register_account(conn, item_key, body.account_id, body.account_currency))


@app.get("/bank/sync-state/{item_key}", dependencies=Auth)
def bank_sync_state(item_key: str, cfg: AcctCfg) -> JsonDict:
    with db.connect(cfg.database_url) as conn:
        return _json(accounting.sync_state(conn, item_key))


class SyncPage(BaseModel):
    added: list[JsonDict] = []
    modified: list[JsonDict] = []
    removed: list[JsonDict] = []
    next_cursor: str
    has_more: bool


@app.post("/bank/sync-page/{item_key}", dependencies=Auth)
def bank_sync_page(item_key: str, body: SyncPage, cfg: AcctCfg) -> JsonDict:
    with db.connect(cfg.database_url) as conn:
        return accounting.store_sync_page(
            conn, item_key, body.added, body.modified, body.removed, body.next_cursor, body.has_more
        )


@app.post("/matching/run/{item_key}", dependencies=Auth)
def matching_run(item_key: str, cfg: AcctCfg, rules: Annotated[matching.Rules, Depends(get_rules)]) -> JsonDict:
    with db.connect(cfg.database_url) as conn:
        result = accounting.run_matching(conn, cfg, item_key, rules)
        return {**result, "write_offs_due": settlement.write_offs_due(conn, cfg), **_qbo_target(cfg)}


class PaymentRecorded(BaseModel):
    external_payment_id: str = Field(min_length=1, max_length=50)
    execution_id: str | None = Field(default=None, max_length=100)


@app.post("/matches/{match_id}/lookup", dependencies=Auth)
def match_payment_lookup(match_id: int, body: Lookup, cfg: AcctCfg) -> JsonDict:
    with db.connect(cfg.database_url) as conn:
        return asdict(accounting.payment_lookup_decision(conn, match_id, body.status_code, body.body))


@app.post("/matches/{match_id}/payment-failed", dependencies=Auth)
def match_payment_failed(match_id: int, body: FailureFacts, cfg: AcctCfg) -> JsonDict:
    with db.connect(cfg.database_url) as conn:
        return _json(accounting.record_payment_failure(conn, cfg, match_id, body.model_dump()))


@app.post("/matches/{match_id}/payment", dependencies=Auth)
def match_payment(match_id: int, body: PaymentRecorded, cfg: AcctCfg) -> JsonDict:
    with db.connect(cfg.database_url) as conn:
        return _json(accounting.record_payment(conn, match_id, body.external_payment_id, body.execution_id))


# Write-offs of payment differences (decided in review, recorded by the bank workflow)
@app.post("/write-offs/{write_off_id}/lookup/{kind}", dependencies=Auth)
def write_off_lookup(write_off_id: int, kind: Literal["credit", "apply"], body: Lookup, cfg: AcctCfg) -> JsonDict:
    with db.connect(cfg.database_url) as conn:
        return asdict(settlement.write_off_lookup(conn, write_off_id, kind, body.status_code, body.body))


@app.post("/write-offs/{write_off_id}/credit", dependencies=Auth)
def write_off_credit(write_off_id: int, body: PaymentRecorded, cfg: AcctCfg) -> JsonDict:
    """The vendor credit exists; the answer is the bill payment of 0 that applies it to the bill."""
    with db.connect(cfg.database_url) as conn:
        return settlement.record_write_off_credit(conn, cfg, write_off_id, body.external_payment_id)


@app.post("/write-offs/{write_off_id}/applied", dependencies=Auth)
def write_off_applied(write_off_id: int, body: PaymentRecorded, cfg: AcctCfg) -> JsonDict:
    with db.connect(cfg.database_url) as conn:
        return _json(
            settlement.record_write_off_applied(conn, write_off_id, body.external_payment_id, body.execution_id)
        )


@app.post("/write-offs/{write_off_id}/failed", dependencies=Auth)
def write_off_failed(write_off_id: int, body: FailureFacts, cfg: AcctCfg) -> JsonDict:
    with db.connect(cfg.database_url) as conn:
        return _json(settlement.record_write_off_failure(conn, cfg, write_off_id, body.model_dump()))


# "Run now": a workflow's webhook asks before it starts (signature, rate limits)
class RunRequest(BaseModel):
    """A signed request (run_now.py): the token itself never travels."""

    timestamp: str = Field(default="", max_length=20)
    signature: str = Field(default="", max_length=128)
    target: str = Field(default="", max_length=40)


@app.post("/run-now/authorize", dependencies=Auth)
def run_now_authorize(body: RunRequest, cfg: AcctCfg) -> JsonDict:
    token = get_settings().run_now_token.get_secret_value()
    with db.connect(cfg.database_url) as conn:
        answer = run_now.authorize(conn, token, run_now.Signed(body.timestamp, body.signature, body.target))
    return {"decision": "run" if answer.outcome == "run" else "refuse", **asdict(answer)}


# =====================================================================================
# QuickBooks reference data (accounts, vendors) and review decisions
# =====================================================================================
@app.exception_handler(qbo_reference.SyncError)
def _sync_error(_: Request, exc: qbo_reference.SyncError) -> JSONResponse:
    return JSONResponse({"error": "sync_refused", "message": str(exc)}, status_code=422)


@app.exception_handler(review.Blocked)
def _blocked(_: Request, exc: review.Blocked) -> JSONResponse:
    problems = [p.model_dump() for p in exc.problems]
    return JSONResponse({"error": "blocked", "message": str(exc), "problems": problems}, status_code=409)


@app.post("/reference/{entity}", dependencies=Auth)
def store_reference(entity: Literal["accounts", "vendors"], body: Lookup, cfg: AcctCfg) -> JsonDict:
    """A QuickBooks query response (all accounts or all vendors), passed through by the sync workflow."""
    name: qbo_reference.Entity = "Account" if entity == "accounts" else "Vendor"
    with db.connect(cfg.database_url) as conn:
        return qbo_reference.store(conn, name, body.status_code, body.body)


@app.get("/accounting/accounts", dependencies=Auth)
def coding_accounts(cfg: AcctCfg) -> list[JsonDict]:
    """The account picker: accounts an invoice can be coded to (from the last sync)."""
    with db.connect(cfg.database_url) as conn:
        return [_json(r) for r in qbo_reference.coding_accounts(conn)]


@app.get("/review/items", dependencies=Auth)
def review_items(
    cfg: AcctCfg,
    scope: review.Scope = "live",
    status: Literal["open", "approved", "corrected", "rejected", "verified", "written_off", "all"] = "open",
) -> list[JsonDict]:
    with db.connect(cfg.database_url) as conn:
        return review.list_items(conn, scope, status)


@app.get("/review/items/{review_id}", dependencies=Auth)
def review_item(review_id: int, cfg: AcctCfg) -> JsonDict:
    with db.connect(cfg.database_url) as conn:
        return review.detail(conn, review_id)


@app.exception_handler(actors.NotAutomation)
def _not_automation(_: Request, exc: actors.NotAutomation) -> JSONResponse:
    return JSONResponse({"error": "not_automation", "message": str(exc)}, status_code=422)


# The bearer token is held by automation only (people decide on the review page): every decision sent here
# is recorded under an automation label, never a person's name (actors.py).
@app.post("/review/items/{review_id}/approve", dependencies=Auth)
def review_approve(review_id: int, body: review.Decision, cfg: AcctCfg) -> JsonDict:
    actors.require_automation(body.reviewer)
    with db.connect(cfg.database_url) as conn:
        review.approve(conn, review_id, body)
        conn.commit()
        return review.detail(conn, review_id)


@app.post("/review/items/{review_id}/verify", dependencies=Auth)
def review_verify(review_id: int, body: review.Decision, cfg: AcctCfg) -> JsonDict:
    """Fraud items: confirmed with the supplier through a known contact; the note says how."""
    actors.require_automation(body.reviewer)
    with db.connect(cfg.database_url) as conn:
        review.approve(conn, review_id, body, action="verify")
        conn.commit()
        return review.detail(conn, review_id)


@app.post("/review/items/{review_id}/correct", dependencies=Auth)
def review_correct(review_id: int, body: review.Correction, cfg: AcctCfg) -> JsonDict:
    actors.require_automation(body.reviewer)
    with db.connect(cfg.database_url) as conn:
        result = review.correct(conn, review_id, body)
        conn.commit()
        return result


@app.post("/review/items/{review_id}/reject", dependencies=Auth)
def review_reject(review_id: int, body: review.Rejection, cfg: AcctCfg) -> JsonDict:
    actors.require_automation(body.reviewer)
    with db.connect(cfg.database_url) as conn:
        _, file_path = review.reject(conn, review_id, body)
        conn.commit()
    accounting.delete_stored_file(cfg.storage_dir, file_path)
    with db.connect(cfg.database_url) as conn:
        return review.detail(conn, review_id)


# Posting and matching items: decisions that take effect in QuickBooks (settlement.py)
def _settle(review_id: int, action: str, body: settlement.Settlement, cfg: accounting.AccountingConfig) -> JsonDict:
    actors.require_automation(body.reviewer)
    with db.connect(cfg.database_url) as conn:
        settlement.decide(conn, review_id, action, body)
        conn.commit()
        return review.detail(conn, review_id)


@app.post("/review/items/{review_id}/confirm-match", dependencies=Auth)
def review_confirm_match(review_id: int, body: settlement.MatchChoice, cfg: AcctCfg) -> JsonDict:
    return _settle(review_id, "confirm_match", body, cfg)


@app.post("/review/items/{review_id}/reject-match", dependencies=Auth)
def review_reject_match(review_id: int, body: settlement.Settlement, cfg: AcctCfg) -> JsonDict:
    return _settle(review_id, "reject_match", body, cfg)


@app.post("/review/items/{review_id}/write-off", dependencies=Auth)
def review_write_off(review_id: int, body: settlement.Settlement, cfg: AcctCfg) -> JsonDict:
    return _settle(review_id, "write_off", body, cfg)


@app.post("/review/items/{review_id}/keep-open", dependencies=Auth)
def review_keep_open(review_id: int, body: settlement.Settlement, cfg: AcctCfg) -> JsonDict:
    return _settle(review_id, "keep_open", body, cfg)


@app.post("/review/items/{review_id}/retry", dependencies=Auth)
def review_retry(review_id: int, body: settlement.Settlement, cfg: AcctCfg) -> JsonDict:
    return _settle(review_id, "retry", body, cfg)


# =====================================================================================
# Daily end-to-end test
# =====================================================================================
@app.exception_handler(e2e_test.Blocked)
def _e2e_blocked(_: Request, exc: e2e_test.Blocked) -> JSONResponse:
    return JSONResponse({"error": "e2e_blocked", "message": str(exc)}, status_code=409)


class E2eStart(BaseModel):
    execution_id: str | None = Field(default=None, max_length=100)


@app.post("/e2e/runs", dependencies=Auth)
def e2e_start(body: E2eStart, cfg: AcctCfg) -> JsonDict:
    """A new run, and where the workflow sends QuickBooks calls."""
    with db.connect(cfg.database_url) as conn:
        run = e2e_test.start(conn, body.execution_id)
    return {"run_id": run["id"], **_qbo_target(cfg)}


@app.get("/e2e/qbo-objects", dependencies=Auth)
def e2e_qbo_objects(run_id: int, cfg: AcctCfg) -> JsonDict:
    with db.connect(cfg.database_url) as conn:
        objects = e2e_test.cleanup_list(conn, run_id)
    return {"objects": [db.jsonable(o) for o in objects], **_qbo_target(cfg)}


class QboAnswer(BaseModel):
    """A QuickBooks response, passed through unchanged by the workflow."""

    status_code: int = Field(ge=0, le=599)
    body: Any = None
    execution_id: str | None = Field(default=None, max_length=100)


@app.post("/e2e/qbo-objects/{object_id}/check", dependencies=Auth)
def e2e_qbo_check(object_id: int, body: QboAnswer, cfg: AcctCfg) -> JsonDict:
    with db.connect(cfg.database_url) as conn:
        return e2e_test.qbo_check(conn, object_id, body.status_code, body.body, body.execution_id)


@app.post("/e2e/qbo-objects/{object_id}/deleted", dependencies=Auth)
def e2e_qbo_deleted(object_id: int, body: QboAnswer, cfg: AcctCfg) -> JsonDict:
    with db.connect(cfg.database_url) as conn:
        return {"deleted": e2e_test.qbo_deleted(conn, object_id, body.status_code, body.body, body.execution_id)}


@app.post("/e2e/runs/{run_id}/reset", dependencies=Auth)
def e2e_reset(run_id: int, body: E2eStart, cfg: AcctCfg) -> JsonDict:
    """The earlier runs' test invoices are deleted (scope e2e only), then the run's messages are handed out."""
    with db.connect(cfg.database_url) as conn:
        done = e2e_test.reset(conn, run_id, body.execution_id)
        now = db.one(conn, "SELECT now() AS t")["t"]
    for path in done.files:  # after COMMIT
        accounting.delete_stored_file(cfg.storage_dir, path)
    return {
        "deleted_invoices": done.invoices,
        "messages": e2e_test.messages(run_id, get_settings().intake_email_alias, now),
    }


class E2eMessage(BaseModel):
    scenario: str = Field(max_length=50)
    gmail_id: str = Field(min_length=1, max_length=200)
    labels: list[Annotated[str, Field(max_length=200)]] = Field(default=[], max_length=50)
    trashed: bool = False  # moved to Gmail's trash after its labels were read


class E2eMessages(BaseModel):
    messages: list[E2eMessage] = Field(max_length=10)
    execution_id: str | None = Field(default=None, max_length=100)


@app.post("/e2e/runs/{run_id}/sent", dependencies=Auth)
def e2e_sent(run_id: int, body: E2eMessages, cfg: AcctCfg) -> JsonDict:
    with db.connect(cfg.database_url) as conn:
        e2e_test.record_sent(conn, run_id, [{"scenario": m.scenario, "gmail_id": m.gmail_id} for m in body.messages])
    return {"recorded": len(body.messages)}


def _summary_recipients() -> list[str]:
    recipients = [a.strip() for a in get_settings().daily_summary_to.split(",") if a.strip()]
    if not recipients:
        raise HTTPException(500, "DAILY_SUMMARY_TO is empty")
    return recipients


def _run_result(conn: db.Conn, run: db.Row) -> JsonDict:
    """A finished run, with the alert email to send at once if it failed."""
    now = db.one(conn, "SELECT now() AS t")["t"]
    alert = daily_summary.e2e_alert(run, _summary_recipients(), now) if run["status"] == "failed" else None
    return {"status": run["status"], "checks": run["checks"], "failures": run["failures"], "alert": alert}


@app.post("/e2e/runs/{run_id}/verify", dependencies=Auth)
def e2e_verify(run_id: int, body: E2eMessages, cfg: AcctCfg) -> JsonDict:
    """Every check of the run."""
    with db.connect(cfg.database_url) as conn:
        run = e2e_test.verify(conn, run_id, [m.model_dump() for m in body.messages], body.execution_id)
        return _run_result(conn, run)


class E2eAbort(BaseModel):
    step: str = Field(min_length=1, max_length=100)  # the workflow node that failed
    message: str = Field(default="", max_length=500)
    execution_id: str | None = Field(default=None, max_length=100)


@app.post("/e2e/runs/{run_id}/abort", dependencies=Auth)
def e2e_abort(run_id: int, body: E2eAbort, cfg: AcctCfg) -> JsonDict:
    """The workflow could not finish the run: it fails, with the alert to send."""
    with db.connect(cfg.database_url) as conn:
        run = e2e_test.abort(conn, run_id, body.step, body.message, body.execution_id)
        return _run_result(conn, run)
