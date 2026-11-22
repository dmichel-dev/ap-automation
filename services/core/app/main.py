"""AP core service: extraction, validation and persistence for every intake channel.

Endpoints so far:
  GET  /health   liveness + database check (no auth)
  POST /extract  file → common invoice format (no persistence; used by tests and debugging)
"""

import hmac
import logging
from functools import lru_cache
from typing import Annotated

import psycopg
from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from . import files
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


@app.post("/extract", response_model=CommonInvoice, dependencies=[Depends(require_token)])
def extract(
    file: Annotated[UploadFile, File(description="PDF, PNG, JPEG, WebP or TIFF")],
    provider: Annotated[ExtractionProvider, Depends(get_provider)],
) -> CommonInvoice:
    content = file.file.read(files.MAX_FILE_BYTES + 1)
    prepared = files.prepare(content)
    result = provider.extract(prepared.content, prepared.mime_type)
    return build_common_invoice(result, provider.name)
