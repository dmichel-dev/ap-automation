"""The public demo pages (demo.davidmichel.dev): start a session, send invoices, follow them, see the
bill that would be posted, and the privacy notice.

Start: uvicorn --factory app.web.demo_site:create_app   (settings: DemoWebSettings, from the environment)

No account and no sign-in form: one click starts a session (demo.start_session), the browser gets the
session token as a cookie, and a session page opens only with that cookie AND the code in its URL.
Every rule (limits, budget, ownership, samples) is the core's (demo.py, demo_samples.py); this module
reads the results and shapes them for the page. It never reads the file store and never sends a
file back: a page shows processing results only.

The request guard is the review page's (guard.py). Processing happens elsewhere: an upload or a
sample click is queued here and processed by the workflow (demo_uploads).
"""

import json
import logging
import math
import mimetypes
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import httpx
from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from .. import db, demo, demo_samples, formatting, review
from ..intake import sub_address
from . import display, forms, guard, reasons, render
from .settings import DemoWebSettings

log = logging.getLogger("app.web.demo")
DEMO_WEBHOOK_HEADER = "X-Demo-Key"  # the header name in n8n's header-auth credential
WEBHOOK_TIMEOUT = 2.0  # seconds: n8n answers at once, before processing
SESSION_COOKIE = "__Host-ap_demo"  # __Host-: Secure, Path=/, no Domain
MAX_FORM = 16 * 1024  # a click on a button
UPLOAD_OVERHEAD = 64 * 1024  # multipart framing around the file
SENT_PARAM, SENT_SAMPLE = "sent", "sample"  # the redirect after a sample send: the sample list opens
FRAUD_CODES = frozenset({"possible_bank_detail_change"})
UPLOAD_ERRORS = {"provider_server": "The OCR service didn't answer. Try again in a few minutes."}
ENDED = "This demo session has ended, or it opens only in the browser that started it."
PDF = "application/pdf"
READ_ONLY_REVIEW = (
    "This is the page a reviewer works from. They would check the invoice against the original, then approve it "
    "with an expense account, correct a field, or reject it. In the demo, these controls are switched off and "
    "nothing is posted."
)
OWN_FILE_NOT_SHOWN = (
    "Your file isn't shown here: the demo never displays a visitor's document. In the live system, the original "
    "appears on this side, next to the data read from it."
)
# The review page's policy, plus fetch to this origin (the session page refreshes its cards) and the
# demo's self-hosted fonts
DEMO_CSP = guard.SECURITY_HEADERS["Content-Security-Policy"] + "; connect-src 'self'; font-src 'self'"
REFUSAL_STATUS = {"too_many_starts": 429, "demo_full": 503, "demo_paused": 503, "file_limit": 409}

router = APIRouter()
# The slim image's MIME table lacks WOFF2: serve the demo's fonts with their own type, not as bytes
mimetypes.add_type("font/woff2", ".woff2")
TEMPLATES = guard.templates()


class SessionGone(Exception):
    """No live session for this cookie and code: the page answers as for an unknown address."""


# =====================================================================================
# Helpers
# =====================================================================================
def settings(request: Request) -> DemoWebSettings:
    s: DemoWebSettings = request.app.state.settings
    return s


def _limits(request: Request) -> demo.Limits:
    limits: demo.Limits = request.app.state.limits
    return limits


def _connect(s: DemoWebSettings) -> db.Conn:
    return db.connect(s.ap_database_url.get_secret_value())


def _page(name: str, status: int = 200, **context: Any) -> HTMLResponse:
    context.setdefault("error", None)
    return HTMLResponse(TEMPLATES.get_template(name).render(**context), status_code=status)


def _refused(name: str, exc: demo.Refused, **context: Any) -> HTMLResponse:
    response = _page(name, REFUSAL_STATUS.get(exc.code, 422), error=demo.REFUSALS.get(exc.code, exc.code), **context)
    if exc.retry_after:
        response.headers["Retry-After"] = str(exc.retry_after)
    return response


def _session(request: Request, code: str) -> demo.Session:
    with _connect(settings(request)) as conn:
        session = demo.session_for_page(conn, request.cookies.get(SESSION_COOKIE), code, datetime.now(UTC))
    if session is None:
        raise SessionGone
    return session


def _minutes_left(session: demo.Session, now: datetime) -> int:
    return max(0, math.ceil((session.expires_at - now).total_seconds() / 60))


# =====================================================================================
# What a card on the session page says (display only: the statuses and reasons are the core's)
# =====================================================================================
# A sample was read by OCR once, when it was made; its saved result is used, so it costs nothing
SAMPLE_SOURCE = "OCR result saved for this sample"


def _reason_lines(found: list[dict[str, Any]], currency: str | None) -> list[dict[str, str | None]]:
    lines: list[dict[str, str | None]] = []
    for r in found:
        known = r.get("code") in reasons.REASONS
        sentence = reasons.explain(r["code"]).sentence if known else r.get("message") or "Not processed."
        detail = display.reason_detail(r.get("message"), currency) if known else None
        lines.append({"sentence": sentence, "detail": detail or None})
    return lines


Step = tuple[str, str, bool]  # (state, text, followed by the confidence score's info button)


def _read_step(item: db.Row) -> Step:
    source = f" ({SAMPLE_SOURCE})" if item["sample"] else ""
    if item["confidence"] is None:
        return ("done", f"Read{source}: {item['lines']} lines", False)
    return ("done", f"Read{source}: {item['lines']} lines · confidence {formatting.percent(item['confidence'])}", True)


def _invoice_steps(item: db.Row) -> list[Step]:
    steps = [("done", f"Received {display.channel(item['channel'])}".strip(), False)]
    if item["status"] == "rejected":
        return [*steps, ("stop", "Not processed further", False)]
    steps.append(_read_step(item))
    if item["status"] == "ready":
        steps.append(("done", "All checks passed", False))
    else:
        labels = ", ".join(reasons.explain(r["code"]).label for r in item["reasons"])
        steps.append(("stop", f"Sent to review: {labels}", False))
    if item["has_preview"] and item["account_names"]:
        coded = ", ".join(item["account_names"].values())
        steps.append(("done", f"Bill prepared for QuickBooks, coded to {coded}", False))
    return steps


def _invoice_card(item: db.Row) -> dict[str, Any]:
    codes = {r.get("code") for r in item["reasons"]}
    status = display.STATUSES["fraud" if codes & FRAUD_CODES else item["status"]]
    return {
        "title": item["supplier_name"] or item["file_name"],
        "sub": _invoice_sub(item),
        "amount": formatting.money(item["total"], item["currency"]) if item["total"] is not None else None,
        "tone": status.tone,
        "label": status.label,
        "reasons": _reason_lines(item["reasons"], item["currency"]) if item["status"] != "ready" else [],
        "steps": _invoice_steps(item),
        "bill": item["id"] if item["has_preview"] and item["account_names"] else None,
        "review": item["id"] if item["status"] == "needs_review" else None,
        "pending": False,
    }


def _invoice_sub(item: db.Row) -> str:
    kind = "Credit note" if item["document_type"] == "credit_note" else "Invoice"
    number = f"{kind} {item['invoice_number']}" if item["invoice_number"] else item["file_name"]
    return f"{number} · {'sample' if item['sample'] else 'your file'}"


def _upload_card(item: db.Row) -> dict[str, Any]:
    failed = item["status"] == "failed"
    reading = f"Reading ({SAMPLE_SOURCE})" if item["sample"] else "Reading with Mistral OCR (a few seconds)"
    now = {"queued": "Waiting to be read", "processing": reading}.get(item["status"], "")
    error = UPLOAD_ERRORS.get(item["error"] or "", "This file couldn't be processed.")
    last = ("stop", error, False) if failed else ("now", now, False)
    status = display.STATUSES["not_processed" if failed else "in_progress"]
    return {
        "title": item["file_name"],
        "sub": "sample" if item["sample"] else "your file",
        "amount": None,
        "tone": status.tone,
        "label": status.label,
        "reasons": [],
        "steps": [("done", f"Received {display.channel('upload')}", False), last],
        "bill": None,
        "review": None,
        "pending": not failed,
    }


def _session_context(request: Request, session: demo.Session) -> dict[str, Any]:
    s, limits, now = settings(request), _limits(request), datetime.now(UTC)
    with _connect(s) as conn:
        items = demo.session_items(conn, session.id)
        # A sample already sent, from this page or by email, can't be sent again
        sent = demo.samples_sent(conn, session.id) | {i["sample"] for i in items if i["sample"]}
        used = session.files_used + demo.files_pending(conn, session.id)
    cards = [_invoice_card(i) if i["kind"] == "invoice" else _upload_card(i) for i in items]
    return {
        "session": session,
        "cards": cards,
        "pending": any(c["pending"] for c in cards),
        "samples": [(sample, sample.key in sent) for sample in demo_samples.CATALOG],
        "samples_open": request.query_params.get(SENT_PARAM) == SENT_SAMPLE,
        "used": min(used, limits.files_per_session),
        "limits": limits,
        "full": used >= limits.files_per_session,
        "minutes_left": _minutes_left(session, now),
        "threshold": formatting.percent(s.confidence_threshold),
        "address": sub_address(s.intake_email_alias, session.code),
    }


# =====================================================================================
# Pages
# =====================================================================================
@router.get("/health")
def health(request: Request) -> Response:
    with _connect(settings(request)) as conn:
        conn.execute("SELECT 1")
    return JSONResponse({"status": "ok"})


def _start_context(request: Request) -> dict[str, Any]:
    with _connect(settings(request)) as conn:
        current = demo.session_for_token(conn, request.cookies.get(SESSION_COOKIE), datetime.now(UTC))
    limits = _limits(request)
    return {"limits": limits, "paused": not limits.open, "current": current}


@router.get("/")
def start_page(request: Request) -> Response:
    return _page("demo_start.html", **_start_context(request))


def _start(request: Request) -> demo.Started:
    s = settings(request)
    with _connect(s) as conn:
        started = demo.start_session(
            conn, _limits(request), guard.client_ip(request, s.trusted_proxies), datetime.now(UTC)
        )
        conn.commit()
    return started


@router.post("/start")
async def start(request: Request) -> Response:
    """One click, one session. A browser that already has a live session goes back to it."""
    context = await run_in_threadpool(_start_context, request)
    if context["current"]:
        return guard.redirect(f"/s/{context['current'].code}")
    try:
        started = await run_in_threadpool(_start, request)
    except demo.Refused as exc:
        return _refused("demo_start.html", exc, **context)
    response = guard.redirect(f"/s/{started.session.code}")
    guard.set_cookie(response, SESSION_COOKIE, started.token)
    return response


@router.get("/s/{code}")
def session_page(request: Request, code: str) -> Response:
    return _page("demo_session.html", **_session_context(request, _session(request, code)))


@router.get("/s/{code}/items")
def session_items(request: Request, code: str) -> Response:
    """The cards alone, for the page's refresh while a file is being processed."""
    return _page("demo_items.html", **_session_context(request, _session(request, code)))


def _queue(request: Request, session: demo.Session, file_name: str, content: bytes, sample: str | None) -> None:
    s = settings(request)
    with _connect(s) as conn:
        upload_id = demo.queue_file(conn, _limits(request), session, file_name, content, datetime.now(UTC), sample)
        conn.commit()
    notify_n8n(s, upload_id)


def notify_n8n(s: DemoWebSettings, upload_id: int) -> None:
    """Asks n8n to process the queued file now. Only its ID is sent (n8n reads nothing else from the
    page). A failure costs a short wait: the clean-up's sweep picks up missed uploads."""
    if not s.demo_webhook_url:
        return
    try:
        r = httpx.post(
            s.demo_webhook_url,
            json={"upload_id": upload_id},
            headers={DEMO_WEBHOOK_HEADER: s.demo_webhook_key.get_secret_value()},
            timeout=WEBHOOK_TIMEOUT,
        )
        r.raise_for_status()
    except httpx.HTTPError as exc:
        log.warning("n8n not told about upload %s (%s); the sweep will process it", upload_id, type(exc).__name__)


async def _queued(request: Request, session: demo.Session, name: str, content: bytes, sample: str | None) -> Response:
    try:
        await run_in_threadpool(_queue, request, session, name, content, sample)
    except demo.Refused as exc:
        context = await run_in_threadpool(_session_context, request, session)
        return _refused("demo_session.html", exc, **context)
    # After a sample, the page comes back with its sample list still open, so the next one is one click away
    landing = f"?{SENT_PARAM}={SENT_SAMPLE}" if sample else ""
    return guard.redirect(f"/s/{session.code}{landing}#invoices")


@router.post("/s/{code}/samples/{key}")
async def send_sample(request: Request, code: str, key: str) -> Response:
    session = await run_in_threadpool(_session, request, code)
    found = demo_samples.sample(key)
    if found is None:
        raise SessionGone  # an unknown sample answers like an unknown page
    return await _queued(request, session, f"{found.key}.pdf", found.file.read_bytes(), found.key)


@router.post("/s/{code}/uploads")
async def upload(request: Request, code: str) -> Response:
    session = await run_in_threadpool(_session, request, code)
    limit = _limits(request).max_file_bytes
    form = await request.form(max_files=1, max_fields=1)
    file = form.get("file")
    if not isinstance(file, UploadFile):
        return guard.plain(400, "choose a file first")
    content = await file.read(limit + 1)
    if len(content) > limit:
        context = await run_in_threadpool(_session_context, request, session)
        return _refused("demo_session.html", demo.Refused("file_too_large"), **context)
    return await _queued(request, session, file.filename or "file", content, None)


def _bill_context(request: Request, code: str, invoice_id: UUID) -> dict[str, Any]:
    session = _session(request, code)
    with _connect(settings(request)) as conn:
        bill = demo.session_bill(conn, session.id, str(invoice_id))
    if bill is None:
        raise SessionGone
    return {"session": session, "bill": bill, "preview": bill["preview"], **_bill_lines(bill["preview"])}


def _bill_lines(preview: dict[str, Any]) -> dict[str, Any]:
    """The preview's lines with their account names, the total, and the request as sent (pretty)."""
    document = preview.get("document") or {}
    names = preview.get("account_names") or {}
    lines = [
        {
            "account": names.get(ln["AccountBasedExpenseLineDetail"]["AccountRef"]["value"], "–"),
            "description": ln.get("Description") or "",
            "amount": Decimal(str(ln["Amount"])),
        }
        for ln in document.get("Line", [])
    ]
    return {
        "lines": lines,
        "total": sum((ln["amount"] for ln in lines), Decimal(0)),
        "request": json.dumps(document, indent=2, ensure_ascii=False),
    }


@router.get("/s/{code}/bills/{invoice_id}")
def bill_page(request: Request, code: str, invoice_id: UUID) -> Response:
    context = _bill_context(request, code, invoice_id)
    context["minutes_left"] = _minutes_left(context["session"], datetime.now(UTC))
    return _page("demo_bill.html", **context)


def _review_context(request: Request, code: str, invoice_id: UUID) -> dict[str, Any]:
    """The review page's own context (review_item.html), read-only, for a needs-review invoice of this
    session. The original is shown only for a sample: a visitor's own file is never shown back."""
    s = settings(request)
    session = _session(request, code)
    with _connect(s) as conn:
        found = demo.session_review(conn, session.id, str(invoice_id))
    if found is None:
        raise SessionGone
    data, sample = found["data"], demo_samples.sample(found["sample"] or "")
    base = f"/s/{code}/review/{invoice_id}"
    return {
        "session": session,
        "minutes_left": _minutes_left(session, datetime.now(UTC)),
        "item": data,
        "inv": data["invoice"],
        "account_groups": [],  # read-only: no chart of accounts in the demo
        "pages": _sample_pages(sample) if sample else 0,
        "pages_href": base + "/pages",
        "original_note": None if sample else OWN_FILE_NOT_SHOWN,
        "back_href": f"/s/{code}#invoices",
        "back_label": "Your invoices",
        "read_only_note": READ_ONLY_REVIEW,
        "confidence": display.confidence_badge(
            data["invoice"]["confidence"], s.confidence_threshold, s.review_confidence_high
        ),
        "chosen": None,
        "correctable": forms.CORRECTABLE,
        "choose_account": review.account_choice_required(data),
        "settle_choices": forms.SETTLE_CHOICES,
        "done": None,
    }


def _sample_pages(sample: demo_samples.Sample) -> int | None:
    try:
        return render.page_count(sample.file, PDF)
    except render.RenderError:
        return None


@router.get("/s/{code}/review/{invoice_id}")
def review_page(request: Request, code: str, invoice_id: UUID) -> Response:
    return _page("demo_review.html", **_review_context(request, code, invoice_id))


@router.get("/s/{code}/review/{invoice_id}/pages/{page}")
def review_sample_page(request: Request, code: str, invoice_id: UUID, page: int) -> Response:
    """A page of a sample's original, as an image. Only for an invoice that came from a sample: for a
    visitor's own file this is a 404, like any page that isn't theirs."""
    session = _session(request, code)
    with _connect(settings(request)) as conn:
        found = demo.session_review(conn, session.id, str(invoice_id))
    sample = demo_samples.sample((found or {}).get("sample") or "")
    if sample is None or not 1 <= page <= (_sample_pages(sample) or 0):
        raise SessionGone
    png = render.render_page(sample.file, PDF, page - 1)
    return Response(png, media_type="image/png", headers={"Cache-Control": "private, max-age=3600"})


@router.get("/privacy")
def privacy() -> Response:
    return _page("demo_privacy.html")


@router.get("/credits")
def credits() -> Response:
    return _page("demo_credits.html")


# =====================================================================================
# Application
# =====================================================================================
def _max_body(request: Request, limits: demo.Limits) -> int:
    return limits.max_file_bytes + UPLOAD_OVERHEAD if request.url.path.endswith("/uploads") else MAX_FORM


def create_app(config: DemoWebSettings | None = None) -> FastAPI:
    s = config or DemoWebSettings()  # read from the environment
    limits = s.demo_limits()
    app = FastAPI(title="AP Automation demo", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings, app.state.limits = s, limits
    TEMPLATES.globals["site_url"] = s.origins[0]  # the public address, for link previews (absolute URLs)

    @app.middleware("http")
    async def check_request(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        refused = guard.refusal(
            request, hosts=s.hosts, origins=s.origins, no_proxy=False, max_body=_max_body(request, limits)
        )
        response = refused or await call_next(request)
        response.headers.setdefault("Content-Security-Policy", DEMO_CSP)
        return guard.add_security_headers(response)

    @app.exception_handler(SessionGone)
    def _gone(_: Request, __: SessionGone) -> Response:
        return _page("demo_error.html", 404, title="Session not found", message=ENDED)

    app.mount("/static", StaticFiles(directory=guard.HERE / "static"), name="static")
    app.include_router(router)
    log.info("demo pages started: origins %s, demo %s", ", ".join(s.origins), "open" if limits.open else "paused")
    return app
