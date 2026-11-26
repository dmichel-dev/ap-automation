"""POST /invoices/process pipeline: one entry point for every intake channel.

1. idempotency: same (channel, source_ref) → return the stored result
2. exact duplicate file (same SHA-256) → rejected, without calling the provider
3. extraction (outside any DB transaction: it can take a while)
4. checks + duplicate lookup + persistence, in ONE transaction under advisory locks

A public demo file (scope 'demo') also passes the demo's limits before step 2 (session live, files
left, size, pages) and takes its pages from the OCR budget before step 3, unless it is one of the
demo's samples, whose stored OCR result is used instead. Every comparison with earlier documents
stays within its session.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from . import bank_details, db, demo, demo_samples, files, intake_failures
from .extraction import build_common_invoice
from .models import CommonInvoice, Issue
from .providers.base import ExtractionProvider, ProviderError, ProviderResult
from .validation import Policy, Validation, validate

log = logging.getLogger(__name__)

EXTENSIONS = {
    "application/pdf": "pdf",
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
    "image/tiff": "tif",
}


@dataclass
class ProcessRequest:
    content: bytes
    file_name: str
    channel: str
    source_ref: str
    received_at: datetime | None = None
    execution_id: str | None = None
    email_text: str | None = None  # subject + body: checked for bank-change wording, never stored
    message_id: str | None = None  # the email (or other message) this file came from, for the re-check
    scope: db.Scope = "live"  # from the intake screening: the daily end-to-end test's emails are 'e2e'
    demo_session_id: str | None = None  # scope 'demo': the session the file was sent to


class ProcessOutcome(BaseModel):
    """Response to n8n. Amounts are decimal strings, dates ISO 8601."""

    invoice_id: str
    status: str  # ready | needs_review | rejected
    reasons: list[dict[str, Any]]
    review_id: int | None = None
    already_processed: bool = False
    summary: dict[str, Any] = {}
    suggested_dates: dict[str, Any] | None = None
    confidence: dict[str, Any] | None = None


@dataclass
class Config:
    database_url: str
    storage_dir: Path
    retention_days: int
    confidence_threshold: float
    bank_fingerprint_key: bytes  # keys the bank-account fingerprints (BANK_FINGERPRINT_KEY)
    policy: Policy = field(default_factory=Policy)
    demo: demo.Limits = field(default_factory=demo.Limits)


def _ctx(req: ProcessRequest) -> dict[str, Any]:
    return {"channel": req.channel, "source_ref": req.source_ref, "execution_id": req.execution_id, "scope": req.scope}


def _existing(row: dict[str, Any]) -> ProcessOutcome:
    return ProcessOutcome(
        invoice_id=str(row["id"]),
        status=row["status"],
        reasons=row["status_reasons"],
        review_id=row["review_id"],
        already_processed=True,
    )


def process(req: ProcessRequest, provider: ExtractionProvider, cfg: Config) -> ProcessOutcome:
    """Any answer (a status, or a file refused as unsupported) ends the email's re-check; a
    failure (provider or core error) leaves it to the re-check."""
    try:
        outcome = _process(req, provider, cfg)
    except files.UnsupportedFile:
        _resolve_failure(req, cfg)
        raise
    _resolve_failure(req, cfg)
    return outcome


def _resolve_failure(req: ProcessRequest, cfg: Config) -> None:
    if req.message_id:
        with db.connect(cfg.database_url) as conn:
            intake_failures.resolve(conn, req.channel, req.message_id, req.execution_id)


def _process(req: ProcessRequest, provider: ExtractionProvider, cfg: Config) -> ProcessOutcome:
    prepared = _prepare(req, cfg)
    sha256 = hashlib.sha256(req.content).hexdigest()
    received_at = req.received_at or datetime.now(UTC)
    source_lock, file_lock = _locks(req, sha256)

    # --- 1 + 2: idempotency and exact duplicate file (short transaction)
    with db.connect(cfg.database_url) as conn:
        db.lock(conn, source_lock, file_lock)
        admitted = _admit(conn, req, prepared, sha256, received_at, cfg)
    if isinstance(admitted, ProcessOutcome):
        return admitted

    # --- 3: extraction (no transaction held)
    extracted = _extract_or_reject(req, prepared, sha256, received_at, admitted, provider, cfg)
    if isinstance(extracted, ProcessOutcome):
        return extracted
    result, provider_name = extracted
    inv = build_common_invoice(result, provider_name)

    # --- 4: checks + persistence (one transaction)
    with db.connect(cfg.database_url) as conn:
        # One supplier at a time: the duplicate and new-supplier checks see each other's results
        supplier_lock = f"supplier:{db.supplier_key(inv)}"
        db.lock(conn, source_lock, file_lock, supplier_lock)
        if row := db.find_by_source(conn, req.channel, req.source_ref):  # lost a race
            return _existing(row)
        if same := db.find_same_file(conn, sha256, req.scope, req.demo_session_id):
            return _reject_duplicate_file(conn, req, prepared, sha256, received_at, same, cfg)

        duplicates = db.find_possible_duplicates(conn, inv, scope=req.scope, session=req.demo_session_id)
        supplier = demo_samples.supplier_status(conn, inv) if req.scope == "demo" else db.supplier_status(conn, inv)
        baseline = db.bank_baseline(conn, inv, req.scope, req.demo_session_id)
        bank = bank_details.assess(inv, req.email_text, baseline, cfg.bank_fingerprint_key)
        v = validate(
            inv,
            confidence_threshold=cfg.confidence_threshold,
            duplicates=duplicates,
            policy=cfg.policy,
            supplier=supplier,
            bank=bank,
        )
        v.checks["info"] = [i.model_dump() for i in v.info]
        invoice_id = str(uuid.uuid4())

        # Keep the original only if someone may still need it (review or posting)
        file_path = None
        if v.status != "rejected":
            file_path = _store_file(cfg.storage_dir, invoice_id, prepared.detected_type, req.content, received_at)
        try:
            db.insert_invoice(
                conn,
                invoice_id=invoice_id,
                channel=req.channel,
                source_ref=req.source_ref,
                received_at=received_at,
                file_name=req.file_name,
                mime_type=prepared.detected_type,
                size=len(req.content),
                sha256=sha256,
                file_path=file_path,
                file_delete_after=received_at + timedelta(days=cfg.retention_days) if file_path else None,
                inv=inv,
                validation=v,
                raw_extraction=bank_details.mask_annotation(result.raw),
                execution_id=req.execution_id,
                confidence_threshold=cfg.confidence_threshold,
                duplicate_of=duplicates[0].invoice_id if duplicates else None,
                expense_account_id=supplier.default_account_id if v.status == "ready" else None,
                bank_accounts=bank.accounts,
                scope=req.scope,
                demo_session_id=req.demo_session_id,
            )
            review_id = db.open_review(conn, invoice_id, v.reasons) if v.status == "needs_review" else None
            db.log(
                conn,
                step="validate",
                level="info" if v.status == "ready" else "warning",
                message=f"status {v.status}",
                invoice_id=invoice_id,
                details={
                    "reasons": [r.code for r in v.reasons],
                    "confidence": v.confidence,
                    "provider": provider_name,
                    "model": result.model,
                    "pages": result.pages,
                    "bank": {  # counts and sources only, never a number
                        "accounts": len(bank.accounts),
                        "notice": bank.notice_sources,
                        "new": len(bank.new_accounts),
                        "first_seen": bank.first_seen,
                    },
                },
                **_ctx(req),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            if file_path:
                (cfg.storage_dir / file_path).unlink(missing_ok=True)
            raise

    return _outcome(invoice_id, inv, v, review_id)


def _prepare(req: ProcessRequest, cfg: Config) -> files.PreparedFile:
    try:
        return files.prepare(req.content)
    except files.UnsupportedFile as exc:
        with db.connect(cfg.database_url) as conn:
            db.log(
                conn,
                step="intake",
                level="warning",
                message="file refused",
                details={"code": exc.code, "size": len(req.content)},
                **_ctx(req),
            )
        raise


def _locks(req: ProcessRequest, sha256: str) -> tuple[str, str]:
    return f"src:{req.channel}:{req.source_ref}", f"sha:{sha256}"


def _admit(
    conn: db.Conn,
    req: ProcessRequest,
    prepared: files.PreparedFile,
    sha256: str,
    received_at: datetime,
    cfg: Config,
) -> ProcessOutcome | int:
    """Steps 1 and 2, under the source and file locks: the stored answer, a rejection, or the go-ahead
    (the file's pages for the demo budget; 0 outside the demo)."""
    if row := db.find_by_source(conn, req.channel, req.source_ref):
        return _existing(row)
    try:
        pages = _admit_demo_file(conn, req, cfg)
    except demo.Refused as exc:
        return _reject(conn, req, prepared, sha256, received_at, _demo_refusal(exc), cfg)
    if same := db.find_same_file(conn, sha256, req.scope, req.demo_session_id):
        return _reject_duplicate_file(conn, req, prepared, sha256, received_at, same, cfg)
    db.log(
        conn,
        step="intake",
        level="info",
        message="file accepted",
        details={"detected_type": prepared.detected_type, "size": len(req.content)},
        **_ctx(req),
    )
    return pages


def _extract_or_reject(
    req: ProcessRequest,
    prepared: files.PreparedFile,
    sha256: str,
    received_at: datetime,
    pages: int,
    provider: ExtractionProvider,
    cfg: Config,
) -> tuple[ProviderResult, str] | ProcessOutcome:
    """Step 3, or a rejection when the demo's OCR budget is used up."""
    try:
        return _extract(req, prepared, sha256, pages, provider, cfg)
    except demo.Refused as exc:
        with db.connect(cfg.database_url) as conn:
            db.lock(conn, *_locks(req, sha256))
            if row := db.find_by_source(conn, req.channel, req.source_ref):
                return _existing(row)
            return _reject(conn, req, prepared, sha256, received_at, _demo_refusal(exc), cfg)


def _admit_demo_file(conn: db.Conn, req: ProcessRequest, cfg: Config) -> int:
    """A demo file's pages once it passed the demo's limits (demo.Refused otherwise); 0 for other scopes."""
    if req.scope != "demo":
        return 0
    if not req.demo_session_id:
        raise ValueError("a demo file needs its session")
    demo.take_file_slot(conn, cfg.demo, req.demo_session_id, datetime.now(UTC))
    return demo.check_file(req.content, cfg.demo)


def _demo_refusal(exc: demo.Refused) -> Issue:
    return Issue(code=f"demo_{exc.code}", field=None, message=demo.REFUSALS.get(exc.code, exc.code))


def _extract(
    req: ProcessRequest,
    prepared: files.PreparedFile,
    sha256: str,
    pages: int,
    provider: ExtractionProvider,
    cfg: Config,
) -> tuple[ProviderResult, str]:
    """(result, provider name). A demo sample's stored result costs nothing; any other demo file takes
    its pages from the budget first (demo.Refused when it is used up) and gives them back on failure."""
    if req.scope == "demo" and (stored := demo_samples.stored_result(sha256)):
        return stored, demo_samples.PROVIDER_NAME
    budget_day = _reserve_demo_pages(req, pages, cfg)
    try:
        return provider.extract(prepared.content, prepared.mime_type), provider.name
    except ProviderError as exc:
        with db.connect(cfg.database_url) as conn:
            if budget_day:
                demo.release_pages(conn, pages, budget_day)
            db.log(
                conn,
                step="extract",
                level="error",
                message="extraction failed",
                details={"kind": exc.kind, "retryable": exc.retryable},
                **_ctx(req),
            )
        raise


def _reserve_demo_pages(req: ProcessRequest, pages: int, cfg: Config) -> date | None:
    """The budget day the pages were taken from (None outside the demo)."""
    if req.scope != "demo":
        return None
    day = datetime.now(UTC).date()
    with db.connect(cfg.database_url) as conn:
        demo.reserve_pages(conn, cfg.demo, pages, day)
    return day


def _reject_duplicate_file(
    conn: db.Conn,
    req: ProcessRequest,
    prepared: files.PreparedFile,
    sha256: str,
    received_at: datetime,
    same: db.Row,
    cfg: Config,
) -> ProcessOutcome:
    reason = Issue(
        code="duplicate_file", field=None, message=f"exact same file as invoice {same['id']} ({same['status']})"
    )
    checks = {"duplicate_file": {"passed": False, "original": str(same["id"])}}
    original = str(same["id"])
    return _reject(conn, req, prepared, sha256, received_at, reason, cfg, checks, original, {"original": original})


def _reject(
    conn: db.Conn,
    req: ProcessRequest,
    prepared: files.PreparedFile,
    sha256: str,
    received_at: datetime,
    reason: Issue,
    cfg: Config,
    checks: dict[str, Any] | None = None,
    duplicate_of: str | None = None,
    details: dict[str, Any] | None = None,
) -> ProcessOutcome:
    """Rejected before extraction: nothing a person could check, no file kept."""
    v = Validation("rejected", [reason], checks or {reason.code: {"passed": False}}, None, None, 0.0)
    invoice_id = str(uuid.uuid4())
    db.insert_invoice(
        conn,
        invoice_id=invoice_id,
        channel=req.channel,
        source_ref=req.source_ref,
        received_at=received_at,
        file_name=req.file_name,
        mime_type=prepared.detected_type,
        size=len(req.content),
        sha256=sha256,
        file_path=None,
        file_delete_after=None,
        inv=None,
        validation=v,
        raw_extraction=None,
        execution_id=req.execution_id,
        confidence_threshold=cfg.confidence_threshold,
        duplicate_of=duplicate_of,
        scope=req.scope,
        demo_session_id=req.demo_session_id,
    )
    db.log(
        conn,
        step="validate",
        level="warning",
        message="status rejected",
        invoice_id=invoice_id,
        details={"reasons": [reason.code], **(details or {})},
        **_ctx(req),
    )
    conn.commit()
    return ProcessOutcome(invoice_id=invoice_id, status="rejected", reasons=[reason.model_dump()])


def _store_file(root: Path, invoice_id: str, detected_type: str, content: bytes, received_at: datetime) -> str:
    rel = Path(f"{received_at:%Y/%m}") / f"{invoice_id}.{EXTENSIONS[detected_type]}"
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = target.with_suffix(".part")
    tmp.write_bytes(content)
    tmp.replace(target)
    return rel.as_posix()


def _outcome(invoice_id: str, inv: CommonInvoice, v: Validation, review_id: int | None) -> ProcessOutcome:
    d = inv.dates
    return ProcessOutcome(
        invoice_id=invoice_id,
        status=v.status,
        reasons=[r.model_dump() for r in v.reasons],
        review_id=review_id,
        summary={
            "document_type": inv.document_type,
            "supplier_name": inv.supplier.name,
            "supplier_country": inv.supplier.country,
            "invoice_number": inv.invoice_number,
            "invoice_date": inv.invoice_date,
            "due_date": inv.due_date,
            "currency": inv.currency,
            "total": inv.total,
            "lines": len(inv.lines),
        },
        suggested_dates=(
            {
                "order": d.suggested_order,
                "invoice_date": d.suggested_invoice_date,
                "due_date": d.suggested_due_date,
                "basis": d.suggestion_basis,
            }
            if d.ambiguous
            else None
        ),
        confidence={
            "score": v.confidence,
            "ocr": v.confidence_ocr,
            "completeness": v.confidence_completeness,
            "tax_mode": v.tax_mode,
        },
    )
