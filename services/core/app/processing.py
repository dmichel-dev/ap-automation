"""POST /invoices/process pipeline: one entry point for every intake channel.

1. idempotency: same (channel, source_ref) → return the stored result
2. exact duplicate file (same SHA-256) → rejected, without calling the provider
3. extraction (outside any DB transaction: it can take a while)
4. checks + duplicate lookup + persistence, in ONE transaction under advisory locks
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from . import db, files
from .extraction import build_common_invoice
from .models import CommonInvoice, Issue
from .providers.base import ExtractionProvider, ProviderError
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
    policy: Policy = field(default_factory=Policy)


def _ctx(req: ProcessRequest) -> dict[str, Any]:
    return {"channel": req.channel, "source_ref": req.source_ref, "execution_id": req.execution_id}


def _existing(row: dict[str, Any]) -> ProcessOutcome:
    return ProcessOutcome(
        invoice_id=str(row["id"]),
        status=row["status"],
        reasons=row["status_reasons"],
        review_id=row["review_id"],
        already_processed=True,
    )


def process(req: ProcessRequest, provider: ExtractionProvider, cfg: Config) -> ProcessOutcome:
    try:
        prepared = files.prepare(req.content)
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
    sha256 = hashlib.sha256(req.content).hexdigest()
    received_at = req.received_at or datetime.now(UTC)
    source_lock, file_lock = f"src:{req.channel}:{req.source_ref}", f"sha:{sha256}"

    # --- 1 + 2: idempotency and exact duplicate file (short transaction)
    with db.connect(cfg.database_url) as conn:
        db.lock(conn, source_lock, file_lock)
        if row := db.find_by_source(conn, req.channel, req.source_ref):
            return _existing(row)
        if same := db.find_same_file(conn, sha256):
            return _reject_duplicate_file(conn, req, prepared, sha256, received_at, same, cfg)
        db.log(
            conn,
            step="intake",
            level="info",
            message="file accepted",
            details={"detected_type": prepared.detected_type, "size": len(req.content)},
            **_ctx(req),
        )

    # --- 3: extraction (no transaction held)
    try:
        result = provider.extract(prepared.content, prepared.mime_type)
    except ProviderError as exc:
        with db.connect(cfg.database_url) as conn:
            db.log(
                conn,
                step="extract",
                level="error",
                message="extraction failed",
                details={"kind": exc.kind, "retryable": exc.retryable},
                **_ctx(req),
            )
        raise
    inv = build_common_invoice(result, provider.name)

    # --- 4: checks + persistence (one transaction)
    with db.connect(cfg.database_url) as conn:
        # One supplier at a time: the duplicate and new-supplier checks see each other's results
        supplier_lock = f"supplier:{db.supplier_key(inv)}"
        db.lock(conn, source_lock, file_lock, supplier_lock)
        if row := db.find_by_source(conn, req.channel, req.source_ref):  # lost a race
            return _existing(row)
        if same := db.find_same_file(conn, sha256):
            return _reject_duplicate_file(conn, req, prepared, sha256, received_at, same, cfg)

        duplicates = db.find_possible_duplicates(conn, inv)
        supplier = db.supplier_status(conn, inv)
        v = validate(
            inv,
            confidence_threshold=cfg.confidence_threshold,
            duplicates=duplicates,
            policy=cfg.policy,
            supplier=supplier,
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
                raw_extraction=result.raw,
                execution_id=req.execution_id,
                confidence_threshold=cfg.confidence_threshold,
                duplicate_of=duplicates[0].invoice_id if duplicates else None,
                expense_account_id=supplier.default_account_id if v.status == "ready" else None,
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
                    "provider": provider.name,
                    "model": result.model,
                    "pages": result.pages,
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
    v = Validation(
        "rejected", [reason], {"duplicate_file": {"passed": False, "original": str(same["id"])}}, None, None, 0.0
    )
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
        duplicate_of=str(same["id"]),
    )
    db.log(
        conn,
        step="validate",
        level="warning",
        message="status rejected",
        invoice_id=invoice_id,
        details={"reasons": ["duplicate_file"], "original": str(same["id"])},
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
