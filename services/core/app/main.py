"""AP core service: extraction, validation and persistence for every intake channel.

Day 1 / step 1: health endpoint only. Business endpoints are added in later steps.
"""

import psycopg
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from .config import get_settings

app = FastAPI(title="AP Automation core", version="0.1.0")


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
