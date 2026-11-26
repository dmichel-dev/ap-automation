"""What every web page of the project shares: the request guard, the security headers, cookies and
the template filters. Used by the review page (web/main.py) and the public demo pages (web/demo_site.py).

The guard, before any route runs:
  - the Host header must be one of the configured origins (no host-header tricks);
  - a listener that is never behind the proxy refuses requests carrying proxy headers;
  - POST: a declared body of at most the route's limit, and the Origin (or Sec-Fetch-Site) must show
    a same-origin request.
Every response carries the security headers (CSP without inline code, no framing, no caching).
"""

from collections.abc import Collection
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import Request
from fastapi.responses import RedirectResponse, Response
from jinja2 import Environment, FileSystemLoader, StrictUndefined

from .. import formatting
from . import display, reasons

HERE = Path(__file__).parent
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
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


# =====================================================================================
# Guard
# =====================================================================================
def plain(status: int, message: str) -> Response:
    return Response(message, status_code=status, media_type="text/plain")


def same_origin(request: Request, origins: Collection[str]) -> bool:
    site = request.headers.get("sec-fetch-site")
    if site is not None and site != "same-origin":
        return False
    origin = request.headers.get("origin")
    if origin is None:
        return site == "same-origin"
    return origin in origins and urlsplit(origin).netloc == request.headers.get("host")


def body_refusal(request: Request, max_body: int) -> Response | None:
    """Bodies are read into memory: their size must be declared and within the limit."""
    length = request.headers.get("content-length", "")
    if not length.isdigit():
        return plain(411, "a form must declare its length")
    return plain(413, "form too large") if int(length) > max_body else None


def refusal(
    request: Request, *, hosts: Collection[str], origins: Collection[str], no_proxy: bool, max_body: int
) -> Response | None:
    """The response that refuses this request, or None to let it through (/health always passes)."""
    if request.url.path == "/health":
        return None
    if request.headers.get("host", "") not in hosts:
        return plain(400, "unknown host")
    if no_proxy and any(h in request.headers for h in FORWARDING_HEADERS):
        return plain(400, "this address is not served through a proxy")
    if request.method not in UNSAFE_METHODS:
        return None
    if not same_origin(request, origins):
        return plain(403, "cross-site request refused")
    return body_refusal(request, max_body)


def client_ip(request: Request, trusted_proxies: Collection[str]) -> str:
    """The peer address; behind a trusted proxy, the address that proxy appended last."""
    peer = request.client.host if request.client else "unknown"
    if peer in trusted_proxies:
        forwarded = request.headers.get("x-forwarded-for", "").split(",")[-1].strip()
        return forwarded[:64] or peer
    return peer


def add_security_headers(response: Response) -> Response:
    for name, value in SECURITY_HEADERS.items():
        response.headers.setdefault(name, value)  # a route may set a stricter one (the download)
    return response


# =====================================================================================
# Cookies and redirects
# =====================================================================================
def redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def set_cookie(response: Response, name: str, value: str) -> None:
    # No Max-Age: the cookie ends with the browser session; the server enforces the time limits
    response.set_cookie(name, value, path="/", secure=True, httponly=True, samesite="strict")


def clear_cookie(response: Response, name: str) -> None:
    response.delete_cookie(name, path="/", secure=True, httponly=True, samesite="strict")


# =====================================================================================
# Templates
# =====================================================================================
def number(value: Any) -> str:
    """A quantity or rate without the database's trailing zeros (1.000000 → 1, 8.5000 → 8.5)."""
    if value is None:
        return ""
    try:
        return format(Decimal(str(value)).normalize(), "f")
    except InvalidOperation:
        return str(value)


def templates() -> Environment:
    """The pages' templates (one base template: every page ends with the copyright footer)."""
    env = Environment(loader=FileSystemLoader(HERE / "templates"), autoescape=True, undefined=StrictUndefined)
    env.filters["money"] = formatting.money  # with the currency code
    env.filters["amount"] = formatting.amount  # without it (tables under a currency)
    env.filters["percent"] = formatting.percent
    env.filters["day"] = formatting.day
    env.filters["moment"] = formatting.moment
    env.filters["detail"] = display.reason_detail
    env.globals["status"] = display.STATUSES.__getitem__
    env.filters["number"] = number
    env.filters["explain"] = reasons.explain
    env.filters["age"] = display.age
    env.filters["tone"] = display.tone
    env.filters["status_label"] = display.status_label
    env.filters["status_name"] = display.label
    return env
