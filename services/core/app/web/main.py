"""The review page: sign in, list review items, look at the document, decide.

Start: uvicorn --factory app.web.main:create_app   (settings from the environment, see settings.py)

Every request passes the guard first:
  - the Host header must be one of the configured origins (no host-header tricks);
  - internal listener: a request carrying proxy headers is refused (it can only be reached directly,
    through the SSH tunnel, never through the reverse proxy);
  - POST: a declared body of at most MAX_BODY; the Origin (or Sec-Fetch-Site) must show a same-origin
    request; and the form's CSRF token must match the session's (or, to sign in, the pre-session cookie's);
  - every response carries the security headers (CSP without inline code, no framing, no caching).
"""

import logging
from collections.abc import Awaitable, Callable
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated, Any, Literal, get_args
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi import Path as PathParam
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, StrictUndefined
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import FormData

from .. import accounting, db, qbo_reference, review, settlement
from ..accounting import Conflict, NotFound
from . import accounts, display, forms, reasons, render
from .accounts import Viewer
from .security import email_digest, new_token, same_token
from .settings import WebSettings

log = logging.getLogger("app.web")
HERE = Path(__file__).parent
SESSION_COOKIE = "__Host-ap_review"  # __Host-: Secure, Path=/, no Domain (no subdomain can set it)
LOGIN_COOKIE = "__Host-ap_login"  # CSRF token of the sign-in form, before there is a session
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
MAX_BODY = 64 * 1024  # the largest form (a correction) is a few hundred bytes
FORWARDING_HEADERS = ("forwarded", "x-forwarded-for", "x-forwarded-host", "x-forwarded-proto", "x-real-ip")
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
    "form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    # same-origin, not no-referrer: with no-referrer, browsers send "Origin: null" on form posts, which the
    # same-origin check rightly refuses. The page's address still never reaches another site.
    "Referrer-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Cache-Control": "no-store",
}
GENERIC_SIGN_IN_ERROR = "Email or password incorrect, or too many failed attempts. Try again later."
NOT_FOUND = "This review item doesn't exist, or isn't one you can see."
DONE = {
    "approve": "Approved. The invoice is ready and will be posted to QuickBooks on the next posting run.",
    "verify": "Verified with the supplier. The invoice is ready and will be posted on the next posting run.",
    "correct": "Correction saved. Check the item again, then approve or reject it.",
    "reject": "Rejected. The invoice won't be posted and its stored file was deleted.",
    "confirm_match": "Match confirmed. The bill payment is recorded in QuickBooks on the next bank run; "
    "this item closes once it is.",
    "reject_match": "Done. None of these transactions will be proposed again for this bill; it stays unpaid.",
    "write_off": "Write-off decided. It is recorded in QuickBooks on the next bank run; this item closes once it is.",
    "keep_open": "Done. The difference stays open on the bill.",
    "retry": "Retry requested. QuickBooks is tried again on the next run; this item closes once it works.",
}
# Account picker groups, in this order (labels for a reviewer); a type not listed here comes last
ACCOUNT_GROUPS = {
    "Expense": "Expenses",
    "Other Expense": "Other expenses",
    "Cost of Goods Sold": "Cost of goods sold",
    "Fixed Asset": "Fixed assets (capitalised)",
    "Other Current Asset": "Other current assets (e.g. prepaid)",
}
NO_ACTION_ON_FRAUD = "Possible fraud: choose “Verified with the supplier” or “Reject”, then confirm. Nothing was saved."
# Decision panel choices on posting and matching items: (action, title, what it does)
SETTLE_CHOICES = (
    ("confirm_match", "Confirm the payment", "The chosen bank transaction pays this bill"),
    ("reject_match", "None of these", "No candidate pays it; they won't be proposed again"),
    ("write_off", "Write off the difference", "A vendor credit closes the bill; no money moves"),
    ("keep_open", "Leave it open", "The difference stays open on the bill"),
    ("retry", "Try again in QuickBooks", "After fixing the cause; nothing is created twice"),
)
ListStatus = Literal["open", "approved", "corrected", "verified", "rejected", "written_off", "all"]

router = APIRouter()


# =====================================================================================
# Templates
# =====================================================================================
def _money(value: Any, currency: str | None = None) -> str:
    if value is None:
        return "–"
    try:
        amount = f"{Decimal(str(value)):,.2f}"
    except InvalidOperation:
        return str(value)
    return f"{amount} {currency}" if currency else amount


def _number(value: Any) -> str:
    """A quantity or rate without the database's trailing zeros (1.000000 → 1, 8.5000 → 8.5)."""
    if value is None:
        return ""
    try:
        return format(Decimal(str(value)).normalize(), "f")
    except InvalidOperation:
        return str(value)


TEMPLATES = Environment(loader=FileSystemLoader(HERE / "templates"), autoescape=True, undefined=StrictUndefined)
TEMPLATES.filters["money"] = _money
TEMPLATES.filters["number"] = _number
TEMPLATES.filters["explain"] = reasons.explain
TEMPLATES.filters["age"] = display.age
TEMPLATES.filters["tone"] = display.tone
TEMPLATES.filters["status_label"] = display.status_label


def _page(name: str, status: int = 200, **context: Any) -> HTMLResponse:
    context.setdefault("viewer", None)
    return HTMLResponse(TEMPLATES.get_template(name).render(**context), status_code=status)


# =====================================================================================
# Guard (every request)
# =====================================================================================
def _plain(status: int, message: str) -> Response:
    return Response(message, status_code=status, media_type="text/plain")


def _same_origin(request: Request, s: WebSettings) -> bool:
    site = request.headers.get("sec-fetch-site")
    if site is not None and site != "same-origin":
        return False
    origin = request.headers.get("origin")
    if origin is None:
        return site == "same-origin"
    return origin in s.origins and urlsplit(origin).netloc == request.headers.get("host")


def _body_refusal(request: Request) -> Response | None:
    """Form bodies are read into memory: their size must be declared and small."""
    length = request.headers.get("content-length", "")
    if not length.isdigit():
        return _plain(411, "a form must declare its length")
    return _plain(413, "form too large") if int(length) > MAX_BODY else None


def _refusal(request: Request, s: WebSettings) -> Response | None:
    if request.url.path == "/health":
        return None
    if request.headers.get("host", "") not in s.hosts:
        return _plain(400, "unknown host")
    if s.review_listener == "internal" and any(h in request.headers for h in FORWARDING_HEADERS):
        return _plain(400, "this address is not served through a proxy")
    if request.method not in UNSAFE_METHODS:
        return None
    if not _same_origin(request, s):
        return _plain(403, "cross-site request refused")
    return _body_refusal(request)


def client_ip(request: Request, s: WebSettings) -> str:
    """The peer address; on the public listener, the address the trusted proxy appended last."""
    peer = request.client.host if request.client else "unknown"
    if s.review_listener == "public" and peer in s.trusted_proxies:
        forwarded = request.headers.get("x-forwarded-for", "").split(",")[-1].strip()
        return forwarded[:64] or peer
    return peer


# =====================================================================================
# Sessions and CSRF
# =====================================================================================
class SignInRequired(Exception):
    pass


class CsrfRefused(Exception):
    pass


class TooManyDecisions(Exception):
    pass


def settings(request: Request) -> WebSettings:
    s: WebSettings = request.app.state.settings
    return s


def _connect(s: WebSettings) -> db.Conn:
    return db.connect(s.ap_database_url.get_secret_value())


def _viewer(s: WebSettings, token: str | None) -> Viewer | None:
    with _connect(s) as conn:
        return accounts.session_viewer(conn, token, s.review_listener)


def current_viewer(request: Request) -> Viewer:
    viewer = _viewer(settings(request), request.cookies.get(SESSION_COOKIE))
    if viewer is None:
        raise SignInRequired
    return viewer


CurrentViewer = Annotated[Viewer, Depends(current_viewer)]


async def _form(request: Request) -> FormData:
    return await request.form(max_files=0, max_fields=40)


def _checked_post(s: WebSettings, token: str | None, csrf: str, count: bool) -> Viewer:
    """The session's viewer, if the form's CSRF token matches; counts the decision (rate limit)."""
    with _connect(s) as conn:
        viewer = accounts.session_viewer(conn, token, s.review_listener)
        if viewer is None or token is None:
            raise SignInRequired
        if not same_token(viewer.csrf, csrf):
            raise CsrfRefused
        if count and accounts.decisions_exceeded(accounts.count_decision(conn, token)):
            raise TooManyDecisions
    return viewer


async def _posted(request: Request, count: bool = True) -> tuple[Viewer, FormData]:
    form = await _form(request)
    token = request.cookies.get(SESSION_COOKIE)
    viewer = await run_in_threadpool(_checked_post, settings(request), token, forms.text(form, "csrf"), count)
    return viewer, form


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def _set_cookie(response: Response, name: str, value: str) -> None:
    # No Max-Age: the cookie ends with the browser session; the server enforces the time limits
    response.set_cookie(name, value, path="/", secure=True, httponly=True, samesite="strict")


def _clear_cookie(response: Response, name: str) -> None:
    response.delete_cookie(name, path="/", secure=True, httponly=True, samesite="strict")


# =====================================================================================
# Sign in / out
# =====================================================================================
def _login_form(error: str | None = None, status: int = 200, retry: int | None = None) -> HTMLResponse:
    token = new_token()
    response = _page("login.html", status, csrf=token, error=error)
    _set_cookie(response, LOGIN_COOKIE, token)
    if retry is not None:
        response.headers["Retry-After"] = str(retry)
    return response


@router.get("/login")
def login_page(request: Request) -> Response:
    if _viewer(settings(request), request.cookies.get(SESSION_COOKIE)) is not None:
        return _redirect("/items")
    return _login_form()


def _sign_in(
    s: WebSettings, ip: str, email: str, password: str, agent: str | None, old: str | None
) -> int | accounts.SignIn:
    """The sign-in result, or the seconds to wait if this address used up its attempts."""
    with _connect(s) as conn:
        wait = accounts.retry_after(conn, ip)
        if wait is not None:
            return wait
        accounts.end_session(conn, old)  # never carry an earlier session over a sign-in
        return accounts.sign_in(conn, s.review_listener, email, password, ip, agent)


@router.post("/login")
async def login(request: Request) -> Response:
    form = await _form(request)
    if not same_token(request.cookies.get(LOGIN_COOKIE), forms.text(form, "csrf")):
        return _plain(403, "the sign-in form expired: reload the page")
    s, email = settings(request), forms.text(form, "email")
    ip = client_ip(request, s)
    args = (s, ip, email, forms.text(form, "password"), request.headers.get("user-agent"))
    result = await run_in_threadpool(_sign_in, *args, request.cookies.get(SESSION_COOKIE))
    if isinstance(result, int):
        log.warning("sign-in refused: too many attempts from %s", ip)
        minutes = -(-result // 60)
        return _login_form(f"Too many sign-in attempts from your address. Try again in {minutes} min.", 429, result)
    log.info("sign-in %s from %s (email %s)", result.outcome, ip, email_digest(email))
    if result.token is None:
        return _login_form(GENERIC_SIGN_IN_ERROR, 401)
    response = _redirect("/items")
    _set_cookie(response, SESSION_COOKIE, result.token)
    _clear_cookie(response, LOGIN_COOKIE)
    return response


def _sign_out(s: WebSettings, token: str | None) -> None:
    with _connect(s) as conn:
        accounts.end_session(conn, token)


@router.post("/logout")
async def logout(request: Request) -> Response:
    await _posted(request, count=False)
    await run_in_threadpool(_sign_out, settings(request), request.cookies.get(SESSION_COOKIE))
    response = _redirect("/login")
    _clear_cookie(response, SESSION_COOKIE)
    return response


# =====================================================================================
# Review items
# =====================================================================================
@router.get("/")
def home() -> Response:
    return _redirect("/items")


@router.get("/health")
def health(request: Request) -> Response:
    with _connect(settings(request)) as conn:
        conn.execute("SELECT 1")
    return JSONResponse({"status": "ok"})


@router.get("/items")
def items(request: Request, viewer: CurrentViewer, status: ListStatus = "open") -> Response:
    s = settings(request)
    with _connect(s) as conn:
        rows = review.list_items(conn, viewer.scope, status)
    return _page(
        "items.html",
        viewer=viewer,
        data_label=display.data_label(s.review_environment, viewer.scope),
        items=rows,
        current=status,
        statuses=get_args(ListStatus),
    )


def _page_count(s: WebSettings, conn: db.Conn, data: dict[str, Any]) -> int | None:
    """Pages to show; 0 if the file is no longer stored; None if it can't be shown."""
    if not data["invoice"]["has_file"]:
        return 0
    try:
        path, mime, _ = accounting.stored_file(conn, Path(s.file_storage_dir), data["invoice"]["id"])
        return render.page_count(path, mime)
    except (NotFound, render.RenderError):
        return None


def account_groups(accounts: list[db.Row], default_id: str | None) -> list[tuple[str, list[db.Row]]]:
    """The picker: the supplier's default first (its own group), then every account by type, the
    default not repeated. Accounts keep the order they come in (by name within a type)."""
    default = [a for a in accounts if a["id"] == default_id]
    groups: dict[str, list[db.Row]] = {}
    for a in accounts:
        if a["id"] != default_id:
            groups.setdefault(a["type"], []).append(a)
    order = list(ACCOUNT_GROUPS)
    by_type = sorted(groups.items(), key=lambda g: order.index(g[0]) if g[0] in order else len(order))
    named = [(ACCOUNT_GROUPS.get(kind, kind), rows) for kind, rows in by_type]
    return ([("Supplier's default", default)] if default else []) + named


def _item_page(
    s: WebSettings,
    conn: db.Conn,
    viewer: Viewer,
    review_id: int,
    status: int = 200,
    chosen: str | None = None,
    **message: str | None,
) -> HTMLResponse:
    """The item page; `chosen` keeps the decision panel on the action just tried (after an error)."""
    data = review.detail(conn, review_id, viewer.scope)
    deciding = bool({"approve", "verify"} & set(data["actions"]))
    default = data["supplier_default_account"]
    return _page(
        "item.html",
        status,
        viewer=viewer,
        data_label=display.data_label(s.review_environment, viewer.scope),
        item=data,
        inv=data["invoice"],
        account_groups=account_groups(qbo_reference.coding_accounts(conn), default and default["id"])
        if deciding
        else [],
        pages=_page_count(s, conn, data),
        confidence=display.confidence_badge(
            data["invoice"]["confidence"], s.confidence_threshold, s.review_confidence_high
        ),
        chosen=chosen,
        correctable=forms.CORRECTABLE,
        choose_account=review.account_choice_required(data),
        settle_choices=SETTLE_CHOICES,
        error=message.get("error"),
        done=message.get("done"),
    )


@router.get("/items/{review_id}")
def item(request: Request, review_id: int, viewer: CurrentViewer, done: str | None = None) -> Response:
    s = settings(request)
    with _connect(s) as conn:
        return _item_page(s, conn, viewer, review_id, done=DONE.get(done or ""))


def _stored_file(s: WebSettings, viewer: Viewer, review_id: int) -> tuple[Path, str, str]:
    """The item's original file, if the viewer may see the item (else NotFound, like an unknown one)."""
    with _connect(s) as conn:
        invoice_id = review.item_invoice_id(conn, review_id, viewer.scope)
        return accounting.stored_file(conn, Path(s.file_storage_dir), str(invoice_id))


@router.get("/items/{review_id}/pages/{page}")
def page_image(
    request: Request, review_id: int, page: Annotated[int, PathParam(ge=1, le=render.MAX_PAGES)], viewer: CurrentViewer
) -> Response:
    path, mime, _ = _stored_file(settings(request), viewer, review_id)
    try:
        png = render.render_page(path, mime, page - 1)
    except render.RenderError:
        return _plain(404, "this page can't be shown")
    return Response(png, media_type="image/png", headers={"Content-Disposition": f'inline; filename="page-{page}.png"'})


def _download_name(name: str | None, review_id: int, mime: str) -> str:
    extension = {"application/pdf": ".pdf", "image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}
    safe = "".join(c for c in (name or "") if c.isalnum() or c in "._- ").strip(" .")[:100]
    return safe if safe else f"review-{review_id}{extension.get(mime, '')}"


@router.get("/items/{review_id}/original")
def original(request: Request, review_id: int, viewer: CurrentViewer) -> Response:
    """Download only (never shown inline), sandboxed in case a browser opens it anyway."""
    path, mime, name = _stored_file(settings(request), viewer, review_id)
    return FileResponse(
        path,
        media_type=mime,
        filename=_download_name(name, review_id, mime),
        content_disposition_type="attachment",
        headers={"Content-Security-Policy": "sandbox; default-src 'none'"},
    )


def _apply(conn: db.Conn, viewer: Viewer, review_id: int, action: review.Action, form: FormData) -> str | None:
    """Runs the decision in the core; returns a stored file to delete after COMMIT (reject)."""
    if action in review.SETTLEMENT_ACTIONS:
        settlement.decide(conn, review_id, action, forms.settled(form, viewer, action), viewer.scope)
        return None
    if action == "correct":
        invoice = review.detail(conn, review_id, viewer.scope)["invoice"]
        review.correct(conn, review_id, forms.correction(form, viewer, invoice), viewer.scope)
        return None
    if action == "reject":
        _, file_path = review.reject(conn, review_id, forms.rejection(form, viewer), viewer.scope)
        return file_path
    kind: Literal["approve", "verify"] = "verify" if action == "verify" else "approve"
    review.approve(conn, review_id, forms.decision(form, viewer), kind, viewer.scope)
    return None


def _decide(s: WebSettings, viewer: Viewer, review_id: int, action: review.Action, form: FormData) -> Response:
    with _connect(s) as conn:
        try:
            file_path = _apply(conn, viewer, review_id, action, form)
        except review.AccountRequired:
            conn.rollback()  # a field left empty on the page: said in the page's words
            return _item_page(s, conn, viewer, review_id, 422, action, error=forms.NO_ACCOUNT_CHOSEN)
        except (Conflict, forms.FormError) as e:
            conn.rollback()
            status = 422 if isinstance(e, forms.FormError) else 409
            return _item_page(s, conn, viewer, review_id, status, action, error=str(e))
        conn.commit()
    accounting.delete_stored_file(Path(s.file_storage_dir), file_path)
    log.info("review item %s: %s by review user %s", review_id, action, viewer.user_id)
    return _redirect(f"/items/{review_id}?done={action}")


@router.post("/items/{review_id}/{action}")
async def decide(request: Request, review_id: int, action: review.Action) -> Response:
    viewer, form = await _posted(request)
    return await run_in_threadpool(_decide, settings(request), viewer, review_id, action, form)


def _decide_in_panel(s: WebSettings, viewer: Viewer, review_id: int, form: FormData) -> Response:
    try:
        action = forms.chosen_action(form)
    except forms.FormError as e:
        with _connect(s) as conn:
            fraud = "verify" in review.detail(conn, review_id, viewer.scope)["actions"]
            return _item_page(s, conn, viewer, review_id, 422, error=NO_ACTION_ON_FRAUD if fraud else str(e))
    return _decide(s, viewer, review_id, action, forms.fields_for(form, action))


@router.post("/items/{review_id}")
async def decide_in_panel(request: Request, review_id: int) -> Response:
    """The item page's decision panel: one form, one chosen action, decided as on that action's route."""
    viewer, form = await _posted(request)
    return await run_in_threadpool(_decide_in_panel, settings(request), viewer, review_id, form)


# =====================================================================================
# Application
# =====================================================================================
def _error_handlers(app: FastAPI) -> None:
    @app.exception_handler(SignInRequired)
    def _sign_in_required(_: Request, __: SignInRequired) -> Response:
        response = _redirect("/login")
        _clear_cookie(response, SESSION_COOKIE)
        return response

    @app.exception_handler(CsrfRefused)
    def _csrf(_: Request, __: CsrfRefused) -> Response:
        return _plain(403, "the form expired or was not sent from this page: reload the page")

    @app.exception_handler(TooManyDecisions)
    def _too_many(_: Request, __: TooManyDecisions) -> Response:
        return Response("too many decisions in a minute", status_code=429, headers={"Retry-After": "60"})

    @app.exception_handler(NotFound)
    def _not_found(_: Request, __: NotFound) -> Response:
        return _page("error.html", 404, title="Not found", message=NOT_FOUND)


def create_app(config: WebSettings | None = None) -> FastAPI:
    s = config or WebSettings()  # read from the environment
    app = FastAPI(title="AP review", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = s

    @app.middleware("http")
    async def guard(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        response = _refusal(request, s) or await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)  # a route may set a stricter one (the download)
        return response

    _error_handlers(app)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    app.include_router(router)
    log.info("review page started: %s listener, origins %s", s.review_listener, ", ".join(s.origins))
    return app
