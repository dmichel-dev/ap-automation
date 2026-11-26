"""The public demo pages: one-click start, the session page (cookie AND code), samples and uploads
queued, the cards after processing, the bill preview (own session only), refusals, the privacy
notice, the footer, links and the security headers. Real Postgres; processing by the core's own endpoint."""

import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import psycopg
import pytest
from app import db
from app.demo_samples import CATALOG
from app.main import app as core_app
from app.web.demo_site import DEMO_CSP, DEMO_WEBHOOK_HEADER, SESSION_COOKIE, WEBHOOK_TIMEOUT, create_app, router
from app.web.guard import SECURITY_HEADERS
from app.web.settings import DemoWebSettings
from fastapi.responses import FileResponse
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from pydantic import ValidationError

from .conftest import AUTH, query
from .test_demo import LIMITS, UPLOAD, demo_env, live_now, pdf_with_pages  # noqa: F401  (fixtures)
from .test_processing import env  # noqa: F401  (fixture)

ORIGIN = "https://demo.test"
FOOTER = '<a href="https://davidmichel.dev" target="_blank" rel="noopener noreferrer">© David Michel</a>'
NEW_TAB = {"target": "_blank", "rel": "noopener noreferrer"}
CREDITS_LINK = '<a href="/credits">Credits</a>'
STATIC = Path(__file__).parent.parent / "app" / "web" / "static"
CADDYFILE = Path("/deploy/Caddyfile")  # mounted read-only by the test service and CI


def site_settings(url: str, **overrides: Any) -> DemoWebSettings:
    values: dict[str, Any] = {
        "ap_database_url": url,
        "demo_origins": ORIGIN,
        "demo_enabled": True,
        "demo_hash_key": LIMITS.hash_key.decode(),
    } | overrides
    return DemoWebSettings(**values)


def visitor(url: str, peer: str = "203.0.113.9", **overrides) -> TestClient:
    """A browser on the demo: sends Origin, keeps cookies, doesn't follow redirects."""
    return TestClient(
        create_app(site_settings(url, **overrides)),
        base_url=ORIGIN,
        headers={"Origin": ORIGIN},
        client=(peer, 50000),
        follow_redirects=False,
    )


def start(c: TestClient) -> str:
    r = c.post("/start")
    assert r.status_code == 303, r.text
    code = r.headers["location"].removeprefix("/s/")
    assert re.fullmatch(r"[a-z2-9]{8}", code)
    return code


def process_queued(url: str) -> None:
    """What the workflow does: process every queued file through the core."""
    for row in query(url, "SELECT id FROM ap.demo_uploads WHERE status = 'queued' ORDER BY id"):
        r = TestClient(core_app).post(f"/demo/uploads/{row['id']}/process", headers=AUTH, json={})
        assert r.status_code == 200, r.text


@pytest.fixture
def site(demo_env):  # noqa: F811
    _state, _pcfg, cfg = demo_env
    return cfg.database_url


# =====================================================================================
# Start
# =====================================================================================
def test_start_page_starts_and_links_the_privacy_notice_without_listing_the_samples(site):
    r = visitor(site).get("/")
    assert r.status_code == 200
    assert "Start a demo session" in r.text and 'href="/privacy"' in r.text and "One click: no account" in r.text
    assert "Review: bank details changed? (sent after October)" not in r.text  # the session page lists them
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", r.text))
    assert "Today: someone types each invoice in by hand" in text
    assert "Automated: invoices are picked up where they arrive" in text
    assert text.index("Today:") < text.index("Automated:") < text.index("Start a demo session")


def test_the_samples_fold_closed_under_a_summary_with_their_count(site):
    c = visitor(site)
    page = c.get(f"/s/{start(c)}").text
    fold = page[page.index('<details class="demo-box demo-fold">') : page.index("</details>")]
    assert "<summary>Send a sample invoice (7)</summary>" in fold  # closed: no `open` attribute
    assert fold.count("/samples/") == 7 and "<script" not in fold and "onclick" not in fold


def test_the_samples_fold_stays_open_after_a_send_and_closes_on_a_plain_visit(site):
    c = visitor(site)
    code = start(c)
    sent = c.post(f"/s/{code}/samples/fr-eur-discount")
    assert sent.headers["location"] == f"/s/{code}?sent=sample#invoices"
    assert '<details class="demo-box demo-fold" open>' in c.get(sent.headers["location"]).text
    assert '<details class="demo-box demo-fold">' in c.get(f"/s/{code}").text  # no signal: closed
    uploaded = c.post(f"/s/{code}/uploads", files={"file": ("mine.pdf", UPLOAD, "application/pdf")})
    assert uploaded.headers["location"] == f"/s/{code}#invoices"  # an upload leaves the fold as it was


def test_sample_titles_name_their_currency_by_iso_code_only():
    titles = [s.title for s in CATALOG]
    assert "French invoice, EUR, with a discount" in titles and "Arabic tax invoice, AED" in titles
    assert "Hindi GST invoice, INR" in titles
    words = ("euro", "dirham", "rupee", "dollar", "UAE", "India")
    assert not [t for t in titles if any(w.lower() in t.lower() for w in words)]


def test_one_click_starts_a_session_with_a_secure_cookie(site):
    c = visitor(site)
    r = c.post("/start")
    assert r.status_code == 303
    cookie = r.headers["set-cookie"]
    assert cookie.startswith(f"{SESSION_COOKIE}=") and "HttpOnly" in cookie and "Secure" in cookie
    assert "samesite=strict" in cookie.lower() and "Path=/" in cookie and "Domain" not in cookie
    page = c.get(r.headers["location"])
    assert page.status_code == 200 and "Your demo session" in page.text
    assert "invoices+" + r.headers["location"].removeprefix("/s/") + "@davidmichel.dev" in page.text


def test_a_browser_with_a_live_session_goes_back_to_it(site):
    c = visitor(site)
    code = start(c)
    again = c.post("/start")
    assert again.headers["location"] == f"/s/{code}" and "set-cookie" not in again.headers
    back = c.get("/").text
    assert "Back to your session" in back and "demo-facts" not in back  # the start page's facts go
    assert query(site, "SELECT count(*) AS n FROM ap.demo_sessions")[0]["n"] == 1


def test_the_code_alone_opens_nothing(site):
    code = start(visitor(site))
    stranger = visitor(site, peer="198.51.100.20")
    r = stranger.get(f"/s/{code}")
    assert r.status_code == 404 and "Start a new demo session" in r.text
    assert stranger.get(f"/s/{code}/items").status_code == 404
    assert stranger.post(f"/s/{code}/samples/fr-eur-discount").status_code == 404
    assert query(site, "SELECT count(*) AS n FROM ap.demo_uploads")[0]["n"] == 0


def test_another_sessions_cookie_does_not_open_this_page(site):
    first, second = visitor(site), visitor(site, peer="198.51.100.21")
    code = start(first)
    start(second)
    assert second.get(f"/s/{code}").status_code == 404


def test_a_cross_site_start_is_refused(site):
    evil = TestClient(create_app(site_settings(site)), base_url=ORIGIN, headers={"Origin": "https://evil.test"})
    assert evil.post("/start").status_code == 403
    assert query(site, "SELECT count(*) AS n FROM ap.demo_sessions")[0]["n"] == 0


def test_too_many_starts_from_one_address(site):
    for _ in range(LIMITS.starts_per_hour):
        start(visitor(site))  # a new browser each time, same address
    r = visitor(site).post("/start")
    assert r.status_code == 429 and int(r.headers["retry-after"]) > 0
    assert "Too many demo sessions" in r.text


def test_paused_demo_offers_no_start(site):
    c = visitor(site, demo_enabled=False)
    assert "The demo is paused" in c.get("/").text
    assert c.post("/start").status_code == 503


# =====================================================================================
# Samples, uploads, cards
# =====================================================================================
def test_a_sample_goes_from_queued_to_ready_with_its_bill(site, live_now):  # noqa: F811
    c = visitor(site)
    code = start(c)
    assert c.post(f"/s/{code}/samples/fr-eur-discount").status_code == 303
    queued = c.get(f"/s/{code}/items").text
    assert "In progress" in queued and 'data-pending="yes"' in queued

    process_queued(site)
    page = c.get(f"/s/{code}").text
    assert "Ateliers Verlibreux SARL" in page and "Ready to post" in page and 'data-pending="no"' in page
    assert "All checks passed" in page and "coded to Office Supplies" in page
    assert "confidence 98% (OCR × fields found)" in page
    assert "Sent</span>" in page  # the sample's button is gone
    [invoice] = query(site, "SELECT id FROM ap.invoices")
    bill = c.get(f"/s/{code}/bills/{invoice['id']}")
    assert bill.status_code == 200
    assert "existing vendor" in bill.text and "AV-2026-0147" in bill.text and "1,598.61 EUR" in bill.text
    assert "nothing is sent" in bill.text and "&#34;DocNumber&#34;: &#34;AV-2026-0147&#34;" in bill.text


def test_a_review_card_says_why_in_the_review_page_words(site):
    c = visitor(site)
    code = start(c)
    c.post(f"/s/{code}/samples/de-eur-wrong-total")
    process_queued(site)
    page = c.get(f"/s/{code}").text
    assert "Needs review" in page and "The lines and tax don&#39;t add up to the invoice total." in page
    assert "total 875.13 ≠ lines + adjustments + tax 865.13" in page
    assert "Sent to review: Total doesn&#39;t add up" in page and "See the bill preview" not in page


def test_a_bill_of_another_session_is_not_found(site):
    first, second = visitor(site), visitor(site, peer="198.51.100.22")
    first_code, second_code = start(first), start(second)
    first.post(f"/s/{first_code}/samples/us-hosting-october")
    process_queued(site)
    [invoice] = query(site, "SELECT id FROM ap.invoices")
    assert first.get(f"/s/{first_code}/bills/{invoice['id']}").status_code == 200
    assert second.get(f"/s/{second_code}/bills/{invoice['id']}").status_code == 404
    assert second.get(f"/s/{second_code}/bills/not-a-uuid").status_code == 422


# =====================================================================================
# Read-only review page
# =====================================================================================
def review_session(site, *samples: str) -> tuple[TestClient, str, dict[str, str]]:
    """A visitor whose session sent these samples (processed), and each sample's invoice id."""
    c = visitor(site)
    code = start(c)
    for key in samples:
        assert c.post(f"/s/{code}/samples/{key}").status_code == 303
        process_queued(site)
    rows = query(
        site, "SELECT u.sample, i.id::text AS id FROM ap.demo_uploads u JOIN ap.invoices i ON i.id = u.invoice_id"
    )
    return c, code, {r["sample"]: r["id"] for r in rows}


def test_a_review_card_opens_the_review_page_read_only(site):
    c, code, ids = review_session(site, "de-eur-wrong-total")
    url = f"/s/{code}/review/{ids['de-eur-wrong-total']}"
    assert f'href="{url}">See the review page</a>' in c.get(f"/s/{code}").text
    r = c.get(url)
    assert r.status_code == 200
    page = r.text
    # The review page's own content and design: its stylesheet, not the demo's palette
    assert 'class="decision"' in page and "Extracted data (OCR)" in page and "/static/demo.css" not in page
    assert "The lines and tax don&#39;t add up to the invoice total." in page
    assert "This is the page a reviewer works from." in page and "nothing is posted." in page
    # Read-only: no form, no token, every control inside a disabled fieldset
    assert "<form" not in page and 'name="csrf"' not in page and "<fieldset disabled>" in page
    assert f'<a class="back" href="/s/{code}#invoices">Your invoices</a>' in page
    # A sample's pages are shown, as images drawn by the demo
    assert f'<img src="{url}/pages/1"' in page
    image = c.get(f"{url}/pages/1")
    assert image.status_code == 200 and image.headers["content-type"] == "image/png"
    assert image.content.startswith(b"\x89PNG")
    assert c.get(f"{url}/pages/99").status_code == 404


def test_the_review_page_opens_only_for_its_session_and_its_review_invoices(site):
    c, code, ids = review_session(site, "us-hosting-october", "de-eur-wrong-total")
    review_url = f"/s/{code}/review/{ids['de-eur-wrong-total']}"
    assert c.get(review_url).status_code == 200
    # The code alone (no cookie) and another session's cookie open nothing
    stranger = visitor(site, peer="198.51.100.30")
    assert stranger.get(review_url).status_code == 404
    other = visitor(site, peer="198.51.100.31")
    other_code = start(other)
    assert other.get(review_url).status_code == 404
    assert other.get(f"/s/{other_code}/review/{ids['de-eur-wrong-total']}").status_code == 404
    assert other.get(f"/s/{other_code}/review/{ids['de-eur-wrong-total']}/pages/1").status_code == 404
    # A ready invoice has no review page
    assert c.get(f"/s/{code}/review/{ids['us-hosting-october']}").status_code == 404
    # A non-demo invoice opens nothing, even with this session's id on it
    with db.connect(site) as conn:
        conn.execute("UPDATE ap.review_queue SET scope = 'live' WHERE invoice_id = %s", (ids["de-eur-wrong-total"],))
        conn.execute(
            "UPDATE ap.invoices SET scope = 'live', demo_session_id = NULL WHERE id = %s", (ids["de-eur-wrong-total"],)
        )
    assert c.get(review_url).status_code == 404
    assert c.get(f"{review_url}/pages/1").status_code == 404
    assert c.get(f"/s/{code}/review/not-a-uuid").status_code == 422


def test_a_visitors_own_file_is_never_shown_on_the_review_page(site):
    c, code, ids = review_session(site, "de-eur-wrong-total")
    with db.connect(site) as conn:  # as if the visitor had uploaded it: no sample behind it
        conn.execute("UPDATE ap.demo_uploads SET sample = NULL, kind = 'upload'")
    url = f"/s/{code}/review/{ids['de-eur-wrong-total']}"
    page = c.get(url).text
    assert "Your file isn&#39;t shown here" in page and "<img" not in page
    assert c.get(f"{url}/pages/1").status_code == 404


def test_the_review_page_shows_no_bank_details(site):
    c, code, ids = review_session(site, "us-hosting-october", "us-hosting-november-other-account")
    invoice = ids["us-hosting-november-other-account"]
    page = c.get(f"/s/{code}/review/{invoice}").text
    assert "Verified with the supplier" in page and "<fieldset disabled>" in page  # the fraud panel, switched off
    assert "Bank details on this document" not in page
    [row] = query(site, "SELECT bank_accounts, checks FROM ap.invoices WHERE id = %s", invoice)
    assert row["bank_accounts"] and row["checks"]["bank_details"]["new"]
    stored = [v for a in row["bank_accounts"] for k, v in a.items() if k != "account_holder" and isinstance(v, str)]
    # Nothing of the stored bank details beyond the masked reason the session card already shows
    card = c.get(f"/s/{code}").text
    assert stored and {v for v in stored if v in page} <= {v for v in stored if v in card}
    assert not any(v in page for v in stored if v not in card)


def demo_role_grants(role: str) -> list[str]:
    """The rights the demo role script gives in the schema (from its schema USAGE grant to the end of
    its SQL), as statements for `role`."""
    script = Path("/db-init/02_demo_role.sh").read_text()
    start = "GRANT USAGE ON SCHEMA ap"
    body = start + script.split(start, 1)[1].split("\nSQL\n", 1)[0]
    sql = "\n".join(line for line in body.splitlines() if not line.strip().startswith("--"))
    return [x.strip().replace(':"demo_user"', role) for x in sql.split(";") if x.strip()]


def test_the_demo_role_reads_the_review_views_and_still_no_invoice(site):
    """The grants of db/init/02_demo_role.sh, applied to a role of this test: the review views are
    readable, the invoices table (bank details included) is not. Needs a superuser (CI)."""
    with db.connect(site) as conn:
        if not db.one(conn, "SELECT rolsuper FROM pg_roles WHERE rolname = current_user")["rolsuper"]:
            pytest.skip("creating a role needs a superuser (the CI database)")
        conn.execute("CREATE ROLE ap_demo_test NOLOGIN")  # rolled back at the end, like every grant
        for statement in demo_role_grants("ap_demo_test"):
            conn.execute(statement)
        conn.execute("SET ROLE ap_demo_test")
        for view in ("demo_review_items", "demo_review_lines", "demo_review_tax_lines"):
            conn.execute(f"SELECT * FROM ap.{view} LIMIT 1")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SELECT bank_accounts FROM ap.invoices LIMIT 1")
        conn.rollback()


def test_unknown_sample_is_not_found(site):
    c = visitor(site)
    code = start(c)
    assert c.post(f"/s/{code}/samples/no-such-sample").status_code == 404


def test_upload_is_queued_with_its_bytes_only_until_processed(site):
    c = visitor(site)
    code = start(c)
    r = c.post(f"/s/{code}/uploads", files={"file": ("my-invoice.pdf", UPLOAD, "application/pdf")})
    assert r.status_code == 303, r.text
    [row] = query(site, "SELECT kind, file_name, size, status FROM ap.demo_uploads")
    assert (row["kind"], row["file_name"], row["size"], row["status"]) == (
        "upload",
        "my-invoice.pdf",
        len(UPLOAD),
        "queued",
    )
    assert "my-invoice.pdf" in c.get(f"/s/{code}").text


class Webhook:
    """Stands in for n8n's webhook: records what the page sent, or fails."""

    def __init__(self, fail: bool = False):
        self.calls: list[dict[str, Any]] = []
        self.fail = fail

    def post(self, url: str, **kwargs: Any) -> httpx.Response:
        self.calls.append({"url": url, **kwargs})
        if self.fail:
            raise httpx.ConnectError("n8n is down")
        return httpx.Response(200, request=httpx.Request("POST", url))


def test_a_queued_file_is_handed_to_n8n_by_its_id_only(site, monkeypatch):
    hook = Webhook()
    monkeypatch.setattr(httpx, "post", hook.post)
    c = visitor(site, demo_webhook_url="http://n8n:5678/webhook/demo", demo_webhook_key="k3y")
    code = start(c)
    assert c.post(f"/s/{code}/samples/us-hosting-october").status_code == 303
    [upload] = query(site, "SELECT id FROM ap.demo_uploads")
    [call] = hook.calls
    assert call["url"] == "http://n8n:5678/webhook/demo"
    assert call["json"] == {"upload_id": upload["id"]}
    assert call["headers"] == {DEMO_WEBHOOK_HEADER: "k3y"} and call["timeout"] == WEBHOOK_TIMEOUT


def test_n8n_down_still_queues_the_file_for_the_sweep(site, monkeypatch):
    hook = Webhook(fail=True)
    monkeypatch.setattr(httpx, "post", hook.post)
    c = visitor(site, demo_webhook_url="http://n8n:5678/webhook/demo", demo_webhook_key="k3y")
    code = start(c)
    assert c.post(f"/s/{code}/samples/us-hosting-october").status_code == 303
    assert len(hook.calls) == 1
    assert query(site, "SELECT status FROM ap.demo_uploads")[0]["status"] == "queued"


def test_no_webhook_set_means_no_call(site, monkeypatch):
    hook = Webhook()
    monkeypatch.setattr(httpx, "post", hook.post)
    c = visitor(site)
    assert c.post(f"/s/{start(c)}/samples/us-hosting-october").status_code == 303
    assert hook.calls == []


def test_upload_over_the_page_limit_is_refused_with_the_reason(site):
    c = visitor(site)
    code = start(c)
    r = c.post(
        f"/s/{code}/uploads", files={"file": ("long.pdf", pdf_with_pages(LIMITS.max_pages + 1), "application/pdf")}
    )
    assert r.status_code == 422 and "more pages than the demo accepts" in r.text
    assert query(site, "SELECT count(*) AS n FROM ap.demo_uploads")[0]["n"] == 0


def test_a_body_larger_than_the_file_limit_is_refused_before_it_is_read(site):
    c = visitor(site)
    code = start(c)
    big = b"%PDF-1.4\n" + b"0" * (LIMITS.max_file_bytes + 70 * 1024)
    assert c.post(f"/s/{code}/uploads", files={"file": ("big.pdf", big, "application/pdf")}).status_code == 413


def test_a_full_session_offers_no_more_sends(site):
    c = visitor(site, demo_files_per_session=1)
    code = start(c)
    c.post(f"/s/{code}/samples/us-hosting-october")
    page = c.get(f"/s/{code}").text
    assert "used all its files" in page and 'action="/s/' + code + "/samples/" not in page
    refused = c.post(f"/s/{code}/samples/fr-eur-discount")
    assert refused.status_code == 409 and "used all its files" in refused.text


# =====================================================================================
# Every page
# =====================================================================================
@pytest.fixture
def every_page(site):
    """A visitor with a session holding a ready and a needs-review invoice, and the path of each kind of page."""
    c, code, ids = review_session(site, "us-hosting-october", "de-eur-wrong-total")
    return c, [
        "/",
        "/privacy",
        "/credits",
        f"/s/{code}",
        f"/s/{code}/bills/{ids['us-hosting-october']}",
        f"/s/{code}/review/{ids['de-eur-wrong-total']}",
        "/s/zzzzzzzz",
    ]


class Links(HTMLParser):
    """Every <a href> on a page, with its attributes."""

    def __init__(self) -> None:
        super().__init__()
        self.found: list[dict[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a" and dict(attrs).get("href"):
            self.found.append({name: value or "" for name, value in attrs})


def links(html: str) -> list[dict[str, str]]:
    parser = Links()
    parser.feed(html)
    return parser.found


def opens_elsewhere(href: str) -> bool:
    """A link to another site, davidmichel.dev included (the demo is its own origin)."""
    parts = urlsplit(href)
    return parts.scheme in ("http", "https") and parts.netloc != urlsplit(ORIGIN).netloc


def test_links_to_other_sites_open_in_a_new_tab_and_demo_links_stay(every_page):
    c, pages = every_page
    seen = 0
    for path in pages:
        for a in links(c.get(path).text):
            target = {key: a.get(key) for key in NEW_TAB}
            if opens_elsewhere(a["href"]):
                seen += 1
                assert target == NEW_TAB, (path, a)
            else:
                assert "target" not in a, (path, a)
    assert seen >= len(pages)  # the footer link at least, on every page


def test_every_page_has_the_footer_and_the_security_headers(every_page):
    c, pages = every_page
    for path in pages:
        r = c.get(path)
        assert FOOTER in r.text and CREDITS_LINK in r.text, path
        assert re.search(r"<script(?![^>]*\bsrc=)", r.text) is None and " style=" not in r.text, path
        expected = SECURITY_HEADERS | {"Content-Security-Policy": DEMO_CSP}
        for name, value in expected.items():
            assert r.headers.get(name) == value, (path, name)
        assert "connect-src 'self'" in r.headers["content-security-policy"]
        assert "font-src 'self'" in r.headers["content-security-policy"]


def test_the_stylesheet_uses_only_fonts_the_demo_serves(site):
    c = visitor(site)
    css = c.get("/static/demo.css")
    assert css.status_code == 200
    urls = re.findall(r"url\(\"?([^\")]+)\"?\)", css.text)
    assert len(urls) == 3 and "@import" not in css.text
    for url in urls:
        assert url.startswith("/static/fonts/"), url
        font = c.get(url)
        assert font.status_code == 200 and font.headers["content-type"] == "font/woff2", url
    for name in ("OFL-bricolage-grotesque", "OFL-inter", "OFL-jetbrains-mono", "MIT-tabler-icons"):
        assert (STATIC / "licenses" / f"{name}.txt").is_file(), name


def test_credits_link_each_licence_text_the_demo_serves(site):
    c = visitor(site)
    page = c.get("/credits")
    assert page.status_code == 200
    licences = sorted({a["href"] for a in links(page.text) if a["href"].startswith("/static/licenses/")})
    assert licences == sorted(f"/static/licenses/{f.name}" for f in (STATIC / "licenses").iterdir())
    for href in licences:
        r = c.get(href)
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain"), href


def caddy_demo_paths() -> list[str]:
    """The path patterns Caddy forwards to demo-web (its @demo matcher)."""
    [line] = [ln.split() for ln in CADDYFILE.read_text().splitlines() if ln.strip().startswith("@demo path ")]
    return line[2:]


def caddy_forwards(path: str) -> bool:
    return any(path == p or (p.endswith("*") and path.startswith(p[:-1])) for p in caddy_demo_paths())


def test_caddy_forwards_every_demo_page_and_not_the_health_check():
    for route in router.routes:
        assert isinstance(route, APIRoute)
        path = re.sub(r"\{[^}]+\}", "x", route.path)
        assert caddy_forwards(path) == (route.path != "/health"), route.path
    assert caddy_forwards("/static/fonts/inter-latin-wght.woff2") and caddy_forwards(
        "/static/licenses/MIT-tabler-icons.txt"
    )


def test_icons_are_decorative(every_page):
    c, pages = every_page
    for path in pages:
        for svg in re.findall(r"<svg[^>]*>", c.get(path).text):
            assert 'aria-hidden="true"' in svg, (path, svg)


def test_privacy_notice_names_the_processor_the_deletion_and_the_contact(site):
    text = visitor(site).get("/privacy").text
    assert "Mistral AI" in text and "60 minutes" in text and "mailto:contact@davidmichel.dev" in text


def test_no_page_sends_a_document_back():
    """The pages show processing results only: no route serves a file. The only image route draws a
    sample's pages (the demo's own fictional files); a visitor's file is never shown (tested above)."""
    routes = [r for r in router.routes if isinstance(r, APIRoute)]
    assert len(routes) == len(router.routes)
    paths = {r.path for r in routes}
    assert paths == {
        "/health",
        "/",
        "/start",
        "/s/{code}",
        "/s/{code}/items",
        "/s/{code}/samples/{key}",
        "/s/{code}/uploads",
        "/s/{code}/bills/{invoice_id}",
        "/s/{code}/review/{invoice_id}",
        "/s/{code}/review/{invoice_id}/pages/{page}",
        "/privacy",
        "/credits",
    }
    assert all(r.response_class is not FileResponse for r in routes)


def test_unknown_host_is_refused(site):
    c = TestClient(create_app(site_settings(site)), base_url="https://other.test")
    assert c.get("/").status_code == 400


@pytest.mark.parametrize("origins", ["http://demo.example.com", ""])
def test_settings_refuse_plain_http_and_empty_origins(origins):
    with pytest.raises(ValidationError):
        DemoWebSettings(ap_database_url="postgresql://x@y/z", demo_origins=origins)


def test_loopback_http_origin_is_accepted_for_a_workstation():
    assert DemoWebSettings(ap_database_url="postgresql://x@y/z", demo_origins="http://localhost:8082").hosts == {
        "localhost:8082"
    }


def test_session_limits_follow_the_settings(site):
    assert create_app(site_settings(site, demo_files_per_session=2)).state.limits.files_per_session == 2
