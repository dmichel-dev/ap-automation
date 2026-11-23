"""The review page: sign-in, sessions, CSRF, rate limits, listeners, scope, document display,
decisions and security headers. Real Postgres; documents rendered by the real renderer."""

import io
import re
from io import BytesIO

import pytest
from app import db
from app.web import accounts, render
from app.web.main import LOGIN_COOKIE, SECURITY_HEADERS, SESSION_COOKIE, create_app
from app.web.security import IP_MAX_ATTEMPTS, PasswordPolicyError
from app.web.settings import WebSettings
from app.web.users import main as users_cli
from fastapi.testclient import TestClient
from PIL import Image
from pydantic import ValidationError

from .conftest import FIXTURES, known_supplier, query, seed_accounts
from .test_processing import FakeProvider, env, post  # noqa: F401  (env is a fixture)

INTERNAL = "https://localhost:8081"
PUBLIC = "https://review.test"
PASSWORD = "correct horse battery"
PDF = (FIXTURES / "us_invoice_quillmoor.pdf").read_bytes()
EVIL = "https://evil.test"


# =====================================================================================
# Helpers
# =====================================================================================
def settings(url, storage, listener="internal", origins=INTERNAL, proxies=""):
    return WebSettings(
        ap_database_url=url,
        file_storage_dir=str(storage),
        review_listener=listener,
        review_origins=origins,
        review_trusted_proxies=proxies,
    )


def browser(s, origin, peer="10.1.2.3"):
    """A browser on this origin: sends Origin on every request, keeps cookies, doesn't follow redirects."""
    return TestClient(
        create_app(s), base_url=origin, headers={"Origin": origin}, client=(peer, 50000), follow_redirects=False
    )


def add_user(url, email="rita@example.test", role="reviewer", scope="live", name="Rita Reviewer"):
    with db.connect(url) as conn:
        return accounts.add_user(conn, email, name, role, scope, PASSWORD)


def form_token(html):
    found = re.search(r'name="csrf" value="([^"]+)"', html)
    assert found, "no CSRF field on the page"
    return found.group(1)


def sign_in(c, email="rita@example.test", password=PASSWORD):
    return c.post("/login", data={"csrf": form_token(c.get("/login").text), "email": email, "password": password})


def csrf(c):
    return form_token(c.get("/items").text)


def plant(c, name, value):
    """Puts a cookie in this browser's jar, as if the site had set it (the jar files a dotless host under .local)."""
    host = c.base_url.host
    c.cookies.set(name, value, domain=host if "." in host else host + ".local")


def q(url, sql, *args):
    return query(url, sql, *args)


@pytest.fixture
def web(clean_db, tmp_path):
    """Internal listener (all roles), a reviewer account, signed out."""
    add_user(clean_db)
    return browser(settings(clean_db, tmp_path), INTERNAL), clean_db


@pytest.fixture
def public(clean_db, tmp_path):
    return settings(clean_db, tmp_path, listener="public", origins=PUBLIC, proxies="10.9.9.9")


@pytest.fixture
def items(env, cfg):  # noqa: F811
    """Two review items with the real test PDF: one live, one in the demo scope; a reviewer signed in."""
    q(cfg.database_url, "DELETE FROM ap.supplier_defaults")
    seed_accounts(cfg.database_url)
    live = post(content=PDF).json()
    env[0]["provider"] = FakeProvider(invoice_number="QM-2000", supplier_name="Other Paper Co")
    demo = post(content=PDF + b"\n%demo copy\n", source_ref="gmail:m2:a2").json()
    for table, key in (("invoices", "id"), ("review_queue", "invoice_id")):
        q(cfg.database_url, f"UPDATE ap.{table} SET scope = 'demo' WHERE {key} = %s", demo["invoice_id"])
    add_user(cfg.database_url)
    c = browser(settings(cfg.database_url, cfg.storage_dir), INTERNAL)
    assert sign_in(c).status_code == 303
    return c, cfg.database_url, live["review_id"], demo["review_id"]


def review_row(url, review_id):
    return q(url, "SELECT * FROM ap.review_queue WHERE id = %s", review_id)[0]


# =====================================================================================
# Settings: the listener mode fails closed
# =====================================================================================
def test_listener_defaults_to_public(clean_db, monkeypatch):
    monkeypatch.delenv("REVIEW_LISTENER", raising=False)
    s = WebSettings(ap_database_url=clean_db, review_origins=PUBLIC)
    assert s.review_listener == "public"


@pytest.mark.parametrize(
    "listener, origins, proxies",
    [
        ("internal", "https://review.example.com", ""),  # internal serves loopback origins only
        ("internal", INTERNAL, "10.9.9.9"),  # internal is never behind a proxy
        ("public", "http://review.test", ""),  # public is HTTPS only
        ("public", PUBLIC, "not-an-ip"),
        ("public", "https://review.test/path", ""),
        ("public", "", ""),
    ],
)
def test_contradictory_settings_stop_the_process(clean_db, listener, origins, proxies):
    with pytest.raises(ValidationError):
        settings(clean_db, "/tmp", listener=listener, origins=origins, proxies=proxies)


# =====================================================================================
# Sign-in
# =====================================================================================
def test_sign_in_sets_a_hardened_session_cookie(web):
    c, url = web
    r = sign_in(c)
    assert r.status_code == 303 and r.headers["location"] == "/items"
    cookie = next(h for h in r.headers.get_list("set-cookie") if h.startswith(SESSION_COOKIE + "="))
    flags = {f.strip().lower() for f in cookie.split(";")[1:]}
    assert {"secure", "httponly", "samesite=strict", "path=/"} <= flags
    assert not any(f.startswith(("domain", "max-age", "expires")) for f in flags)  # ends with the browser session
    token = cookie.split(";")[0].split("=", 1)[1]
    stored = q(url, "SELECT token_hash, listener FROM ap.review_sessions")[0]
    assert stored["listener"] == "internal" and bytes(stored["token_hash"]) != token.encode()  # only a digest
    assert c.get("/items").status_code == 200


def test_wrong_password_and_unknown_email_get_the_same_answer(web):
    c, url = web
    wrong, unknown = sign_in(c, password="not the password"), sign_in(c, email="nobody@example.test")
    assert wrong.status_code == unknown.status_code == 401
    assert "Email or password incorrect" in wrong.text and "Email or password incorrect" in unknown.text
    assert SESSION_COOKIE not in c.cookies
    attempts = q(url, "SELECT outcome, email_hash FROM ap.login_attempts ORDER BY id")
    assert [a["outcome"] for a in attempts] == ["bad_credentials", "bad_credentials"]
    assert all("@" not in a["email_hash"] for a in attempts)  # the typed address is not stored


def test_the_account_locks_after_five_failures_even_for_the_right_password(web):
    c, url = web
    for _ in range(5):
        sign_in(c, password="not the password")
    user = q(url, "SELECT failed_logins, lock_count, locked_until > now() AS locked FROM ap.review_users")[0]
    assert user == {"failed_logins": 0, "lock_count": 1, "locked": True}
    r = sign_in(c)
    assert r.status_code == 401 and SESSION_COOKIE not in c.cookies
    assert q(url, "SELECT outcome FROM ap.login_attempts ORDER BY id DESC LIMIT 1")[0]["outcome"] == "locked"
    q(url, "UPDATE ap.review_users SET locked_until = now() - interval '1 second'")  # the lock ends
    assert sign_in(c).status_code == 303
    assert q(url, "SELECT lock_count FROM ap.review_users")[0]["lock_count"] == 0  # reset by a success


def test_each_new_lock_lasts_twice_as_long_up_to_four_hours():
    minutes = [accounts.lock_duration(n).total_seconds() / 60 for n in range(6)]
    assert minutes == [15, 30, 60, 120, 240, 240]
    assert accounts.lock_duration(10_000).total_seconds() == 4 * 3600


def test_too_many_attempts_from_one_address_are_refused_without_being_recorded(web):
    c, url = web
    for _ in range(IP_MAX_ATTEMPTS):
        sign_in(c, email="nobody@example.test")
    r = sign_in(c)  # even the right password
    assert r.status_code == 429 and 0 < int(r.headers["retry-after"]) <= 600
    assert q(url, "SELECT count(*) AS n FROM ap.login_attempts")[0]["n"] == IP_MAX_ATTEMPTS
    other = browser(settings(url, "/tmp"), INTERNAL, peer="10.1.2.4")
    assert sign_in(other).status_code == 303  # another address isn't affected


def test_sign_in_needs_the_form_token(web):
    c, _url = web
    c.get("/login")
    r = c.post("/login", data={"csrf": "forged", "email": "rita@example.test", "password": PASSWORD})
    assert r.status_code == 403
    c.cookies.delete(LOGIN_COOKIE)
    r = c.post("/login", data={"csrf": "", "email": "rita@example.test", "password": PASSWORD})
    assert r.status_code == 403


def test_a_session_cookie_present_before_sign_in_is_never_reused(web):
    # Session fixation: whatever session cookie the browser holds when signing in is ended and replaced
    c, url = web
    sign_in(c)
    earlier = c.cookies.get(SESSION_COOKIE)
    victim = browser(settings(url, "/tmp"), INTERNAL, peer="10.1.2.5")
    token = form_token(victim.get("/login").text)
    plant(victim, SESSION_COOKIE, earlier)
    r = victim.post("/login", data={"csrf": token, "email": "rita@example.test", "password": PASSWORD})
    assert r.status_code == 303 and victim.cookies.get(SESSION_COOKIE) not in (None, earlier)
    assert q(url, "SELECT count(*) AS n FROM ap.review_sessions")[0]["n"] == 1  # the earlier one was ended
    assert c.get("/items").headers["location"] == "/login"


# =====================================================================================
# Sessions
# =====================================================================================
def test_signed_out_visitors_are_sent_to_sign_in(web):
    c, _url = web
    for path in ("/items", "/items/1", "/items/1/pages/1", "/items/1/original"):
        r = c.get(path)
        assert r.status_code == 303 and r.headers["location"] == "/login", path


def test_an_idle_session_ends(web):
    c, url = web
    sign_in(c)
    q(url, "UPDATE ap.review_sessions SET last_seen_at = now() - interval '31 minutes'")
    assert c.get("/items").headers["location"] == "/login"


def test_a_session_ends_at_its_absolute_limit_even_if_used(web):
    c, url = web
    sign_in(c)
    q(url, "UPDATE ap.review_sessions SET expires_at = now() - interval '1 second'")
    assert c.get("/items").headers["location"] == "/login"


def test_sign_out_ends_the_session_on_the_server(web):
    c, url = web
    sign_in(c)
    token = c.cookies.get(SESSION_COOKIE)
    r = c.post("/logout", data={"csrf": csrf(c)})
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert q(url, "SELECT count(*) AS n FROM ap.review_sessions")[0]["n"] == 0
    plant(c, SESSION_COOKIE, token)  # a copy of the old cookie is useless
    assert c.get("/items").status_code == 303


@pytest.mark.parametrize("command", [["deactivate"], ["set-password", "--password-stdin"]])
def test_deactivating_or_a_new_password_ends_the_sessions(web, command, monkeypatch):
    c, url = web
    sign_in(c)
    monkeypatch.setenv("AP_DATABASE_URL", url)
    monkeypatch.setenv("REVIEW_ORIGINS", INTERNAL)
    monkeypatch.setattr("sys.stdin", io.StringIO("another long password\n"))
    assert users_cli([*command[:1], "--email", "rita@example.test", *command[1:]]) == 0
    assert c.get("/items").status_code == 303


# =====================================================================================
# Listeners: internal accounts never sign in on the public site
# =====================================================================================
def test_public_site_refuses_internal_accounts_without_counting_a_failure(web, public):
    _c, url = web
    p = browser(public, PUBLIC)
    r = sign_in(p)  # the reviewer's right password
    assert r.status_code == 401 and "Email or password incorrect" in r.text and SESSION_COOKIE not in p.cookies
    user = q(url, "SELECT failed_logins, locked_until FROM ap.review_users")[0]
    assert user == {"failed_logins": 0, "locked_until": None}  # an internet visitor can't lock a reviewer out
    assert q(url, "SELECT outcome, listener FROM ap.login_attempts")[0] == {
        "outcome": "role_not_allowed_here",
        "listener": "public",
    }


@pytest.mark.parametrize("role", ["reviewer", "admin"])
def test_internal_roles_never_sign_in_publicly(clean_db, public, role):
    add_user(clean_db, email=f"{role}@example.test", role=role)
    assert sign_in(browser(public, PUBLIC), email=f"{role}@example.test").status_code == 401


def test_demo_accounts_sign_in_on_the_public_site(clean_db, public):
    add_user(clean_db, email="visitor@example.test", role="demo", scope="demo", name="Demo visitor")
    p = browser(public, PUBLIC)
    assert sign_in(p, email="visitor@example.test").status_code == 303
    assert p.get("/items").status_code == 200


def test_an_internal_session_is_not_valid_on_the_public_site(web, public):
    c, _url = web
    sign_in(c)
    p = browser(public, PUBLIC)
    plant(p, SESSION_COOKIE, c.cookies.get(SESSION_COOKIE))
    assert p.get("/items").headers["location"] == "/login"


def test_a_session_ends_if_the_role_may_no_longer_use_the_listener(clean_db, public):
    add_user(clean_db, email="visitor@example.test", role="demo", scope="demo", name="Demo visitor")
    p = browser(public, PUBLIC)
    sign_in(p, email="visitor@example.test")
    q(clean_db, "UPDATE ap.review_users SET role = 'reviewer', scope = 'live'")
    assert p.get("/items").headers["location"] == "/login"


@pytest.mark.parametrize(
    "header", ["X-Forwarded-For", "Forwarded", "X-Forwarded-Host", "X-Forwarded-Proto", "X-Real-IP"]
)
def test_the_internal_listener_refuses_proxied_requests(web, header):
    c, _url = web
    r = c.get("/login", headers={header: "203.0.113.7"})
    assert r.status_code == 400


def test_unknown_host_names_are_refused(web, public):
    c, _url = web
    assert c.get("/login", headers={"Host": "review.davidmichel.dev"}).status_code == 400
    assert browser(public, PUBLIC).get("/login", headers={"Host": "evil.test"}).status_code == 400


def test_public_listener_uses_the_trusted_proxys_client_address_only(clean_db, public):
    # From the trusted proxy: the address it appended last. From anyone else: the peer, whatever it claims.
    for peer in ("10.9.9.9", "10.0.0.5"):
        p = browser(public, PUBLIC, peer=peer)
        p.headers["X-Forwarded-For"] = "1.1.1.1, 198.51.100.4"
        sign_in(p, email="nobody@example.test")
    rows = q(clean_db, "SELECT client_ip FROM ap.login_attempts ORDER BY id")
    assert [r["client_ip"] for r in rows] == ["198.51.100.4", "10.0.0.5"]


# =====================================================================================
# Cross-site requests
# =====================================================================================
def test_cross_origin_posts_are_refused(items):
    c, url, live, _demo = items
    token = csrf(c)
    # "null" is what a browser sends from a sandboxed frame or under Referrer-Policy: no-referrer
    for headers in ({"Origin": EVIL}, {"Origin": "null"}, {"Sec-Fetch-Site": "cross-site", "Origin": INTERNAL}):
        r = c.post(f"/items/{live}/reject", data={"csrf": token, "note": "forged"}, headers=headers)
        assert r.status_code == 403, headers
    assert review_row(url, live)["status"] == "open"


def test_a_post_without_origin_or_fetch_metadata_is_refused(items):
    c, _url, live, _demo = items
    token = csrf(c)
    del c.headers["Origin"]
    r = c.post(f"/items/{live}/reject", data={"csrf": token, "note": "forged"})
    assert r.status_code == 403
    r = c.post(
        f"/items/{live}/reject", data={"csrf": token, "note": "same origin"}, headers={"Sec-Fetch-Site": "same-origin"}
    )
    assert r.status_code == 303  # a same-origin request without Origin, as some browsers send


def test_oversized_or_undeclared_form_bodies_are_refused(items):
    c, url, live, _demo = items
    token = csrf(c)
    r = c.post(f"/items/{live}/reject", data={"csrf": token, "note": "x" * 70_000})
    assert r.status_code == 413
    r = c.post(
        f"/items/{live}/reject",
        content=iter([b"csrf=" + token.encode()]),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert r.status_code == 411  # streamed without a length
    assert review_row(url, live)["status"] == "open"


def test_decisions_need_the_sessions_csrf_token(items):
    c, url, live, _demo = items
    for token in ("", "a" * 43):
        r = c.post(f"/items/{live}/reject", data={"csrf": token, "note": "forged"})
        assert r.status_code == 403
    assert review_row(url, live)["status"] == "open"


def test_another_sessions_token_is_not_accepted(items):
    c, url, live, _demo = items
    other = browser(settings(url, "/tmp"), INTERNAL, peer="10.1.2.9")
    sign_in(other)
    r = c.post(f"/items/{live}/reject", data={"csrf": csrf(other), "note": "forged"})
    assert r.status_code == 403


# =====================================================================================
# Scope: what a user can't see doesn't exist for them
# =====================================================================================
def test_the_list_shows_only_the_users_scope(items):
    c, _url, live, demo = items
    html = c.get("/items").text
    assert f'href="/items/{live}"' in html and f'href="/items/{demo}"' not in html


def test_a_demo_user_gets_404_for_everything_outside_the_demo_scope(items):
    _c, url, live, demo = items
    add_user(url, email="visitor@example.test", role="demo", scope="demo", name="Demo visitor")
    d = browser(settings(url, "/tmp"), INTERNAL, peer="10.1.2.8")
    sign_in(d, email="visitor@example.test")
    token = csrf(d)
    for path in (f"/items/{live}", f"/items/{live}/pages/1", f"/items/{live}/original"):
        assert d.get(path).status_code == 404, path
    for action in ("approve", "verify", "correct", "reject"):
        r = d.post(f"/items/{live}/{action}", data={"csrf": token, "note": "not mine", "total": "1"})
        assert r.status_code == 404, action
    assert review_row(url, live)["status"] == "open" and review_row(url, live)["corrections"] == []
    assert d.get(f"/items/{demo}").status_code == 200  # its own scope works


def test_a_reviewer_gets_404_for_a_demo_item(items):
    c, _url, _live, demo = items
    assert c.get(f"/items/{demo}").status_code == 404
    assert "isn&#39;t one you can see" in c.get(f"/items/{demo}").text  # same answer as an unknown item


@pytest.mark.parametrize("role, scope", [("demo", "live"), ("reviewer", "demo"), ("admin", "demo")])
def test_roles_and_scopes_must_fit(clean_db, role, scope):
    with pytest.raises(accounts.UserError):
        add_user(clean_db, role=role, scope=scope)


# =====================================================================================
# Decisions through the page: the session is the reviewer
# =====================================================================================
def test_approving_records_the_signed_in_person(items):
    c, url, live, _demo = items
    r = c.post(f"/items/{live}/approve", data={"csrf": csrf(c), "account_id": "20", "default": "keep"})
    assert r.status_code == 303 and r.headers["location"] == f"/items/{live}?done=approve"
    row = review_row(url, live)
    user_id = q(url, "SELECT id FROM ap.review_users")[0]["id"]
    assert row["status"] == "approved" and row["reviewer"] == f"Rita Reviewer (review user {user_id})"
    assert row["decision"]["account_id"] == "20" and row["decision"]["supplier_default"] == "created"
    assert "Approved. The invoice is ready" in c.get(r.headers["location"]).text


def test_the_picker_shows_the_supplier_default_first_then_accounts_by_type(items):
    c, url, live, _demo = items
    known_supplier(url, account="20")  # the supplier's default account is Supplies (20)
    html = c.get(f"/items/{live}").text
    select = html[
        html.index('<select name="account_id"') : html.index("</select>", html.index('<select name="account_id"'))
    ]
    groups = re.findall(r'<optgroup label="([^"]+)">(.*?)</optgroup>', select, re.S)
    assert [label for label, _ in groups] == ["Supplier&#39;s default", "Expenses", "Fixed assets (capitalised)"]
    values = [re.findall(r'<option value="(\w+)"( selected)?', body) for _, body in groups]
    assert values == [[("20", " selected")], [("7", "")], [("40", "")]]  # the default once; no income account
    assert 'value=""' not in select  # nothing to choose when the default is there


@pytest.mark.parametrize(
    "sub_type", ["AccumulatedDepreciation", "Inventory", "UndepositedFunds", "UnappliedCashBillPaymentExpense"]
)
def test_accounts_a_bill_must_never_use_are_hidden_by_detail_type(items, sub_type):
    c, url, live, _demo = items
    kind = "Fixed Asset" if sub_type == "AccumulatedDepreciation" else "Other Current Asset"
    sql = """INSERT INTO ap.qbo_accounts
             (external_id, name, fully_qualified_name, account_type, account_sub_type, active)
             VALUES ('900', 'Any Name', 'Any Name', %s, %s, true)"""
    q(url, sql, kind, sub_type)
    assert 'value="900"' not in c.get(f"/items/{live}").text
    r = c.post(f"/items/{live}/approve", data={"csrf": csrf(c), "account_id": "900"})
    assert r.status_code == 409 and review_row(url, live)["status"] == "open"  # refused by the core too


@pytest.mark.parametrize(
    "name, kind, sub_type",
    [
        ("Uncategorized Expense", "Expense", "OtherMiscellaneousServiceCost"),
        ("Uncategorized Asset", "Other Current Asset", "OtherCurrentAssets"),
    ],
)
def test_catch_all_accounts_are_hidden_by_their_default_name(items, name, kind, sub_type):
    c, url, live, _demo = items
    sql = """INSERT INTO ap.qbo_accounts
             (external_id, name, fully_qualified_name, account_type, account_sub_type, active)
             VALUES (%s, %s, %s, %s, %s, true)"""
    q(url, sql, "901", name, name, kind, sub_type)
    q(url, sql, "902", "Commissions & fees", "Commissions & fees", kind, sub_type)  # same detail type: stays
    page = c.get(f"/items/{live}").text
    assert 'value="901"' not in page and 'value="902"' in page
    r = c.post(f"/items/{live}/approve", data={"csrf": csrf(c), "account_id": "901"})
    assert r.status_code == 409 and review_row(url, live)["status"] == "open"  # refused by the core too


def test_without_a_default_the_reviewer_must_choose(items):
    c, _url, live, _demo = items
    html = c.get(f"/items/{live}").text
    assert '<option value="" selected>Choose an account</option>' in html and "Supplier&#39;s default" not in html


def test_a_reviewer_name_typed_into_the_form_is_ignored(items):
    c, url, live, _demo = items
    c.post(f"/items/{live}/approve", data={"csrf": csrf(c), "account_id": "20", "reviewer": "Someone Else"})
    assert review_row(url, live)["reviewer"].startswith("Rita Reviewer")


def test_a_refused_decision_shows_the_cores_reason(items):
    c, url, live, _demo = items
    r = c.post(f"/items/{live}/approve", data={"csrf": csrf(c), "account_id": "1"})  # an income account
    assert r.status_code == 409 and "can&#39;t be used" in r.text
    r = c.post(f"/items/{live}/reject", data={"csrf": csrf(c), "note": ""})
    assert r.status_code == 422
    assert review_row(url, live)["status"] == "open"


def test_correcting_sends_only_the_changed_fields(items):
    c, url, live, _demo = items
    page = c.get(f"/items/{live}").text
    values = dict(re.findall(r'name="(\w+)" value="([^"]*)"', page))
    values.update({"csrf": csrf(c), "document_type": "invoice", "invoice_number": "QM-1042-A"})
    r = c.post(f"/items/{live}/correct", data=values)
    assert r.status_code == 303
    corrections = review_row(url, live)["corrections"]
    assert len(corrections) == 1 and corrections[0]["after"] == {"invoice_number": "QM-1042-A"}
    assert corrections[0]["by"].startswith("Rita Reviewer (review user ")
    r = c.post(f"/items/{live}/correct", data={**values, "invoice_number": "QM-1042-A"})
    assert r.status_code == 422 and "nothing was changed" in r.text


def test_rejecting_deletes_the_stored_file(items):
    c, url, live, _demo = items
    path = q(
        url, "SELECT i.file_path FROM ap.invoices i JOIN ap.review_queue r ON r.invoice_id = i.id WHERE r.id = %s", live
    )[0]["file_path"]
    r = c.post(f"/items/{live}/reject", data={"csrf": csrf(c), "note": "not our order"})
    assert r.status_code == 303 and review_row(url, live)["status"] == "rejected"
    assert c.get(f"/items/{live}/original").status_code == 404
    assert path and "no longer stored" in c.get(f"/items/{live}").text


def test_a_runaway_script_is_stopped(items):
    c, url, live, _demo = items
    token = csrf(c)
    q(url, "UPDATE ap.review_sessions SET window_count = 60, window_start = now()")
    r = c.post(f"/items/{live}/reject", data={"csrf": token, "note": "too fast"})
    assert r.status_code == 429 and r.headers["retry-after"] == "60"
    assert review_row(url, live)["status"] == "open"


def test_supplier_text_is_escaped(items):
    c, url, live, _demo = items
    q(url, "UPDATE ap.invoices SET supplier_name = '<script>alert(1)</script>'")
    for page in (c.get("/items").text, c.get(f"/items/{live}").text):
        assert "<script>alert(1)</script>" not in page and "&lt;script&gt;alert(1)&lt;/script&gt;" in page


# =====================================================================================
# Documents: page images, never the PDF itself
# =====================================================================================
def test_pages_are_png_images_rendered_on_the_server(items):
    c, _url, live, _demo = items
    assert f'src="/items/{live}/pages/1"' in c.get(f"/items/{live}").text
    r = c.get(f"/items/{live}/pages/1")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert r.content.startswith(b"\x89PNG") and r.headers["x-content-type-options"] == "nosniff"
    image = Image.open(BytesIO(r.content))
    assert abs(image.width - render.TARGET_WIDTH) <= 1  # PDFium rounds the size up
    assert c.get(f"/items/{live}/pages/2").status_code == 404  # a one-page document
    assert c.get(f"/items/{live}/pages/{render.MAX_PAGES + 1}").status_code == 422


def test_the_original_is_a_sandboxed_download(items):
    c, _url, live, _demo = items
    r = c.get(f"/items/{live}/original")
    assert r.status_code == 200 and r.content == PDF
    assert r.headers["content-disposition"].startswith("attachment;")
    assert "sandbox" in r.headers["content-security-policy"]


def test_a_damaged_file_is_not_shown(tmp_path):
    damaged = tmp_path / "damaged.pdf"
    damaged.write_bytes(b"%PDF-1.4 this is not really a PDF")
    with pytest.raises(render.RenderError):
        render.page_count(damaged, "application/pdf")


def test_a_decompression_bomb_is_refused(tmp_path):
    bomb = tmp_path / "bomb.png"
    Image.new("1", (8000, 8000)).save(bomb)  # 64 megapixels in a few kilobytes
    assert bomb.stat().st_size < 100_000
    with pytest.raises(render.RenderError):
        render.render_page(bomb, "image/png", 0)


def test_only_the_stored_file_types_are_rendered(tmp_path):
    other = tmp_path / "page.svg"
    other.write_text("<svg/>")
    with pytest.raises(render.RenderError):
        render.render_page(other, "image/svg+xml", 0)


def test_images_are_re_encoded(tmp_path):
    photo = tmp_path / "scan.jpg"
    Image.new("RGB", (2800, 3600), "white").save(photo, exif=b"Exif\x00\x00secret")
    png = render.render_page(photo, "image/jpeg", 0)
    out = Image.open(BytesIO(png))
    assert out.format == "PNG" and out.width <= render.TARGET_WIDTH and b"secret" not in png


# =====================================================================================
# Headers and surface
# =====================================================================================
def test_every_response_carries_the_security_headers(items):
    c, _url, live, _demo = items
    responses = [
        c.get("/login"),
        c.get("/items"),
        c.get(f"/items/{live}"),
        c.get(f"/items/{live}/pages/1"),
        c.get("/items/999999"),
        c.get("/static/app.js"),
        c.post(f"/items/{live}/reject", data={"csrf": "x"}, headers={"Origin": EVIL}),
    ]
    for r in responses:
        for name, value in SECURITY_HEADERS.items():
            assert r.headers.get(name) == value, (r.request.url.path, name)


def test_pages_have_no_inline_script_or_style(items):
    c, _url, live, _demo = items
    for html in (c.get("/login").text, c.get("/items").text, c.get(f"/items/{live}").text):
        assert re.search(r"<script(?![^>]*\bsrc=)", html) is None
        assert "<style" not in html and " style=" not in html and " on" + "click=" not in html


def test_every_page_has_the_copyright_footer(items):
    c, url, live, _demo = items
    signed_out = browser(settings(url, "/tmp"), INTERNAL, peer="10.1.2.7")
    pages = (signed_out.get("/login"), c.get("/items"), c.get(f"/items/{live}"), c.get("/items/999999"))
    for r in pages:
        assert '<a href="https://davidmichel.dev">© David Michel</a>' in r.text, r.request.url.path


def test_no_api_docs_and_no_machine_api(web):
    c, _url = web
    for path in ("/docs", "/redoc", "/openapi.json", "/invoices/process", "/postings/claim"):
        assert c.get(path).status_code == 404, path


# =====================================================================================
# Accounts
# =====================================================================================
def test_passwords_are_argon2id_hashes(web):
    _c, url = web
    stored = q(url, "SELECT password_hash FROM ap.review_users")[0]["password_hash"]
    assert stored.startswith("$argon2id$") and PASSWORD not in stored


@pytest.mark.parametrize("password", ["short", "x" * 129, "rita@example.test"])
def test_weak_passwords_are_refused(clean_db, password):
    with db.connect(clean_db) as conn, pytest.raises(PasswordPolicyError):
        accounts.add_user(conn, "rita@example.test", "Rita", "reviewer", "live", password)


def test_the_cli_creates_an_account_from_stdin(clean_db, monkeypatch, capsys):
    import io

    monkeypatch.setenv("AP_DATABASE_URL", clean_db)
    monkeypatch.setenv("REVIEW_ORIGINS", INTERNAL)
    monkeypatch.setattr("sys.stdin", io.StringIO(PASSWORD + "\n"))
    args = [
        "add",
        "--email",
        "Ada@Example.test",
        "--name",
        "Ada",
        "--role",
        "admin",
        "--scope",
        "live",
        "--password-stdin",
    ]
    assert users_cli(args) == 0
    assert q(clean_db, "SELECT email, role FROM ap.review_users")[0] == {"email": "ada@example.test", "role": "admin"}
    monkeypatch.setattr("sys.stdin", io.StringIO("short\n"))
    assert users_cli([*args[:2], "other@example.test", *args[3:]]) == 1
    assert "12 to 128 characters" in capsys.readouterr().err
