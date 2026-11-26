"""The public demo: sessions (cookie AND code), limits, the OCR budget, samples with stored results,
session-scoped checks (duplicates, bank details), the bill preview, no posting, and the clean-up."""

import hashlib
import io
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from app import accounting, db, demo, demo_samples, main
from app.extraction import build_common_invoice
from app.intake import Attachment, demo_code, screen_email
from app.main import app
from fastapi.testclient import TestClient
from PIL import Image

from .conftest import AUTH, FIXTURES, QUILLMOOR_KEY, known_supplier, query
from .test_accounting import OPEN_BOOKS
from .test_processing import FakeProvider, env  # noqa: F401  (fixture)

ALIAS = "invoices@davidmichel.dev"  # the settings' default alias
LIMITS = demo.Limits(enabled=True, hash_key=b"test-demo-hash-key-0123456789")
NOW = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)
WRENFOLD_KEY = "000000003"  # supplier_key of the hosting samples (EIN 00-0000003, normalized)
UPLOAD = (FIXTURES / "us_invoice_quillmoor.pdf").read_bytes()  # a visitor's own file: not a sample


def sample_bytes(key: str) -> bytes:
    found = demo_samples.sample(key)
    assert found is not None
    return found.file.read_bytes()


def pdf_with_pages(n: int) -> bytes:
    out = io.BytesIO()
    pages = [Image.new("RGB", (200, 280), "white") for _ in range(n)]
    pages[0].save(out, format="PDF", save_all=True, append_images=pages[1:])
    return out.getvalue()


def start(url: str, client: str = "203.0.113.7", now: datetime = NOW, limits: demo.Limits = LIMITS) -> demo.Started:
    with db.connect(url) as conn:
        return demo.start_session(conn, limits, client, now)


def refusal(fn, *args) -> demo.Refused:
    with pytest.raises(demo.Refused) as exc:
        fn(*args)
    return exc.value


# =====================================================================================
# The code in the email address: Delivered-To only
# =====================================================================================
def test_demo_code_is_read_from_delivered_to():
    assert demo_code("invoices+k7m2qxab@davidmichel.dev", ALIAS) == "k7m2qxab"
    assert demo_code("David <david@davidmichel.dev>, <INVOICES+K7M2QXAB@davidmichel.dev>", ALIAS) == "k7m2qxab"


@pytest.mark.parametrize(
    "delivered_to",
    [
        "invoices+e2e@davidmichel.dev",  # the end-to-end test's tag, not a session code
        "invoices+k7m2qxal@davidmichel.dev",  # "l" isn't in the code alphabet
        "invoices+k7m2qxab@example.test",  # another domain
        "xinvoices+k7m2qxab@davidmichel.dev",  # another mailbox
        "invoices+k7m2qxab@davidmichel.dev, invoices+p9r3tyzc@davidmichel.dev",  # two codes: neither
    ],
)
def test_no_demo_code(delivered_to):
    assert demo_code(delivered_to, ALIAS) is None


def test_a_code_in_to_but_not_in_delivered_to_is_not_demo_mail():
    """To is written by the sender: only Delivered-To, set by Google on delivery, names the session."""
    pdf = [Attachment("attachment_0", "application/pdf", "i.pdf", 50000)]
    to_code = ["invoices+k7m2qxab@davidmichel.dev"]
    delivered_to_alias = screen_email(to_code, "invoices@davidmichel.dev", pdf, ALIAS)
    assert (delivered_to_alias.scope, delivered_to_alias.demo_code) == ("live", None)
    nowhere = screen_email(to_code, "david@davidmichel.dev", pdf, ALIAS)
    assert (nowhere.route, nowhere.demo_code) == ("not_for_alias", None)
    demo_mail = screen_email(["invoices@davidmichel.dev"], "invoices+k7m2qxab@davidmichel.dev", pdf, ALIAS)
    assert (demo_mail.scope, demo_mail.demo_code, demo_mail.route) == ("demo", "k7m2qxab", "process")


# =====================================================================================
# Sessions
# =====================================================================================
def test_session_starts_with_a_code_and_a_token_stored_only_as_a_digest(clean_db):
    started = start(clean_db)
    assert len(started.session.code) == demo.CODE_LENGTH
    assert set(started.session.code) <= set(demo.CODE_ALPHABET)
    assert started.session.expires_at == NOW + timedelta(minutes=60)
    [row] = query(clean_db, "SELECT token_digest, client_digest FROM ap.demo_sessions")
    assert bytes(row["token_digest"]) != started.token.encode() and len(bytes(row["token_digest"])) == 32
    assert "203.0.113.7" not in row["client_digest"]


def test_the_code_alone_never_opens_the_session_page(clean_db):
    """/s/CODE needs the session cookie too: a shared or guessed link shows nothing."""
    mine, other = start(clean_db), start(clean_db, client="198.51.100.4")
    with db.connect(clean_db) as conn:
        page = demo.session_for_page(conn, mine.token, mine.session.code, NOW)
        assert page is not None and page.id == mine.session.id
        assert demo.session_for_page(conn, None, mine.session.code, NOW) is None  # no cookie
        assert demo.session_for_page(conn, "", mine.session.code, NOW) is None
        assert demo.session_for_page(conn, "forged-token", mine.session.code, NOW) is None
        assert demo.session_for_page(conn, other.token, mine.session.code, NOW) is None  # someone else's cookie
        assert demo.session_for_page(conn, mine.token, other.session.code, NOW) is None
        assert demo.session_for_page(conn, mine.token, None, NOW) is None
        assert demo.session_for_page(conn, mine.token, mine.session.code, NOW + timedelta(minutes=61)) is None


def test_nothing_starts_unless_switched_on_and_keyed(clean_db):
    assert refusal(start, clean_db, "203.0.113.7", NOW, demo.Limits()).code == "demo_paused"
    no_key = demo.Limits(enabled=True)
    assert refusal(start, clean_db, "203.0.113.7", NOW, no_key).code == "demo_paused"


def test_starts_per_client_per_hour_and_per_day(clean_db):
    for minute in range(3):
        start(clean_db, now=NOW + timedelta(minutes=minute))
    refused = refusal(start, clean_db, "203.0.113.7", NOW + timedelta(minutes=10))
    assert refused.code == "too_many_starts" and refused.retry_after == 50 * 60 + 1  # the first start leaves
    start(clean_db, client="198.51.100.4", now=NOW + timedelta(minutes=10))  # another client is fine
    for hour in range(1, 8):
        start(clean_db, now=NOW + timedelta(hours=hour))
    assert refusal(start, clean_db, "203.0.113.7", NOW + timedelta(hours=9)).code == "too_many_starts"  # 10 a day


def test_active_sessions_are_capped(clean_db):
    limits = demo.Limits(enabled=True, hash_key=LIMITS.hash_key, active_sessions=2)
    start(clean_db, "a", NOW, limits)
    start(clean_db, "b", NOW + timedelta(minutes=5), limits)
    refused = refusal(start, clean_db, "c", NOW + timedelta(minutes=10), limits)
    assert refused.code == "demo_full" and refused.retry_after == 50 * 60 + 1
    start(clean_db, "c", NOW + timedelta(minutes=61), limits)  # the first one has expired


# =====================================================================================
# Files and the OCR budget
# =====================================================================================
def test_file_check_size_type_and_pages():
    assert demo.check_file(UPLOAD, LIMITS) == 1
    assert demo.check_file(pdf_with_pages(3), LIMITS) == 3
    assert refusal(demo.check_file, pdf_with_pages(4), LIMITS).code == "too_many_pages"
    assert refusal(demo.check_file, b"%PDF-1.4 " + b"x" * (LIMITS.max_file_bytes), LIMITS).code == "file_too_large"
    assert refusal(demo.check_file, b"MZ\x90\x00 not a document", LIMITS).code == "unsupported_type"
    assert refusal(demo.check_file, b"%PDF-1.4 broken", LIMITS).code == "unreadable_file"


def test_queue_counts_files_already_used_and_waiting(clean_db):
    session = start(clean_db).session
    with db.connect(clean_db) as conn:
        for _ in range(5):
            demo.queue_file(
                conn, LIMITS, session, "x.pdf", sample_bytes("fr-eur-discount"), NOW, sample="fr-eur-discount"
            )
        assert refusal(demo.queue_file, conn, LIMITS, session, "y.pdf", UPLOAD, NOW).code == "file_limit"
        assert refusal(demo.queue_file, conn, LIMITS, session, "y.pdf", UPLOAD, NOW + timedelta(hours=2)).code in (
            "session_expired",
            "file_limit",
        )
        assert (
            refusal(demo.queue_file, conn, LIMITS, session, "big.pdf", pdf_with_pages(4), NOW).code == "too_many_pages"
        )


def test_budget_per_day_and_month_and_release(clean_db):
    limits = demo.Limits(pages_per_day=3, pages_per_month=5)
    day = NOW.date()
    with db.connect(clean_db) as conn:
        demo.reserve_pages(conn, limits, 3, day)
        assert demo.budget_left(conn, limits, day) == 0
        assert refusal(demo.reserve_pages, conn, limits, 1, day).code == "budget_reached"  # the day's cap
        demo.release_pages(conn, 2, day)  # day 1, month 1
        assert demo.budget_left(conn, limits, day) == 2
        demo.reserve_pages(conn, limits, 3, day + timedelta(days=1))  # a new day, same month: 4 used
        assert demo.budget_left(conn, limits, day + timedelta(days=2)) == 1
        assert refusal(demo.reserve_pages, conn, limits, 2, day + timedelta(days=2)).code == "budget_reached"


# =====================================================================================
# Processing: samples, uploads, session-scoped checks
# =====================================================================================
def seed_demo_accounts(url: str) -> None:
    """The accounts the samples' suppliers are coded to, as the QuickBooks sync stores them (those
    the shared seed already has are kept). Nothing else about the suppliers is in the database."""
    names = sorted({s.account for s in demo_samples.CATALOG})
    for i, name in enumerate(names):
        query(
            url,
            """INSERT INTO ap.qbo_accounts (external_id, name, fully_qualified_name, account_type, active)
               SELECT %s, %s, %s, 'Expense', true WHERE NOT EXISTS (SELECT 1 FROM ap.qbo_accounts WHERE name = %s)""",
            f"90{i}",
            name,
            name,
            name,
        )


@pytest.fixture
def demo_env(env, cfg):  # noqa: F811
    """Processing and accounting on the test database, with the accounts the samples need."""
    state, pcfg = env
    seed_demo_accounts(cfg.database_url)
    pcfg.demo = LIMITS
    return state, pcfg, cfg


def queue(url: str, session: demo.Session, content: bytes, sample: str | None = None) -> int:
    with db.connect(url) as conn:
        return demo.queue_file(conn, LIMITS, session, f"{sample or 'upload'}.pdf", content, datetime.now(UTC), sample)


def process(upload_id: int):
    r = TestClient(app).post(f"/demo/uploads/{upload_id}/process", headers=AUTH, json={"execution_id": "7"})
    assert r.status_code == 200, r.text
    return r.json()


def send_sample(url: str, session: demo.Session, key: str):
    return process(queue(url, session, sample_bytes(key), sample=key))


def codes(body) -> list[str]:
    return [r["code"] for r in body["reasons"]]


@pytest.fixture
def live_now():
    """Sessions started 'now', so processing (which uses the real clock) finds them live."""
    return datetime.now(UTC)


def test_sample_uses_its_stored_result_and_gets_a_bill_preview(demo_env, live_now):
    state, _pcfg, cfg = demo_env
    session = start(cfg.database_url, now=live_now).session
    body = send_sample(cfg.database_url, session, "us-hosting-october")
    assert state["provider"].calls == 0  # no OCR call
    assert body["status"] == "ready", body
    assert body["summary"]["invoice_number"] == "WH-2026-1001" and body["summary"]["total"] == "160.58"
    [inv] = query(cfg.database_url, "SELECT scope, demo_session_id, extraction_provider FROM ap.invoices")
    assert (inv["scope"], str(inv["demo_session_id"])) == ("demo", session.id)
    assert inv["extraction_provider"] == demo_samples.PROVIDER_NAME
    [preview] = query(cfg.database_url, "SELECT preview FROM ap.demo_bill_previews")
    p = preview["preview"]
    assert p["object_type"] == "bill" and p["vendor"] == {"name": "Wrenfold Hosting LLC", "action": "existing"}
    assert p["document"]["DocNumber"] == "WH-2026-1001"
    assert list(p["account_names"].values()) == ["Hosting & Cloud Services"]
    assert sum(line["Amount"] for line in p["document"]["Line"]) == pytest.approx(160.58)
    [upload] = query(cfg.database_url, "SELECT status, content, invoice_id FROM ap.demo_uploads")
    assert upload["status"] == "done" and upload["content"] is None and str(upload["invoice_id"]) == body["invoice_id"]


# What each sample shows a visitor who sends it first in a new session (demo_samples.CATALOG "shows")
SAMPLE_OUTCOMES = {
    "fr-eur-discount": ("ready", []),
    "de-eur-wrong-total": ("needs_review", ["total_mismatch"]),
    "es-eur-ambiguous-date": ("needs_review", ["ambiguous_date"]),
    "ar-aed-tax-invoice": ("ready", []),
    "hi-inr-gst-invoice": ("ready", []),
    "us-hosting-october": ("ready", []),
    "us-hosting-november-other-account": ("ready", []),  # the fraud alert needs October first (test below)
}


def test_every_sample_has_an_expected_outcome():
    assert sorted(SAMPLE_OUTCOMES) == sorted(s.key for s in demo_samples.CATALOG)


@pytest.mark.parametrize("key", sorted(SAMPLE_OUTCOMES))
def test_each_sample_produces_its_outcome(demo_env, live_now, key):
    """Every time, whatever the live data: the demo knows the samples' suppliers itself."""
    _state, _pcfg, cfg = demo_env
    session = start(cfg.database_url, now=live_now).session
    body = send_sample(cfg.database_url, session, key)
    assert (body["status"], codes(body)) == SAMPLE_OUTCOMES[key], body["reasons"]
    if body["status"] != "ready":
        assert query(cfg.database_url, "SELECT count(*) AS n FROM ap.demo_bill_previews")[0]["n"] == 0
        return
    [preview] = query(cfg.database_url, "SELECT preview FROM ap.demo_bill_previews")
    p, sample = preview["preview"], demo_samples.sample(key)
    assert "error" not in p and p["vendor"]["action"] == "existing", p
    assert sample is not None and list(p["account_names"].values()) == [sample.account]


def test_live_supplier_data_does_not_reach_the_demo(demo_env, live_now):
    """A live default account (or a live supplier) never decides a demo outcome."""
    _state, _pcfg, cfg = demo_env
    url = cfg.database_url
    known_supplier(url, WRENFOLD_KEY, account="20")  # live: hosting coded to Supplies
    # live: the visitor's upload (Quillmoor) is also from a known supplier (the env fixture)
    assert query(url, "SELECT 1 FROM ap.supplier_defaults WHERE supplier_key = %s", QUILLMOOR_KEY)
    session = start(url, now=live_now).session
    hosting = send_sample(url, session, "us-hosting-october")
    [inv] = query(url, "SELECT expense_account_id FROM ap.invoices WHERE id = %s", hosting["invoice_id"])
    [hosting_account] = query(url, "SELECT external_id FROM ap.qbo_accounts WHERE name = 'Hosting & Cloud Services'")
    assert inv["expense_account_id"] == hosting_account["external_id"]
    upload = process(queue(url, session, UPLOAD))
    assert {"new_vendor", "expense_account_missing"} <= set(codes(upload))


def test_the_demo_suppliers_are_unknown_outside_the_demo(demo_env):
    _state, _pcfg, cfg = demo_env
    content = sample_bytes("us-hosting-october")
    result = demo_samples.stored_result(hashlib.sha256(content).hexdigest())
    assert result is not None
    inv = build_common_invoice(result, demo_samples.PROVIDER_NAME)
    with db.connect(cfg.database_url) as conn:
        assert db.supplier_status(conn, inv).known is False
        assert demo_samples.supplier_status(conn, inv).known is True


def test_a_sample_supplier_without_its_account_is_known_but_has_no_default(demo_env, live_now):
    """The account comes from the QuickBooks copy by name: missing there, the invoice goes to review."""
    _state, _pcfg, cfg = demo_env
    query(cfg.database_url, "UPDATE ap.qbo_accounts SET active = false WHERE name = 'Hosting & Cloud Services'")
    session = start(cfg.database_url, now=live_now).session
    body = send_sample(cfg.database_url, session, "us-hosting-october")
    assert (body["status"], codes(body)) == ("needs_review", ["expense_account_missing"])


def test_demo_invoices_are_never_posted(demo_env, live_now):
    _state, _pcfg, cfg = demo_env
    session = start(cfg.database_url, now=live_now).session
    assert send_sample(cfg.database_url, session, "us-hosting-october")["status"] == "ready"
    with db.connect(cfg.database_url) as conn:
        assert accounting.claim_postings(conn, cfg, OPEN_BOOKS, 10) == []
    assert query(cfg.database_url, "SELECT count(*) AS n FROM ap.accounting_postings")[0]["n"] == 0


def test_new_bank_details_raise_the_fraud_alert_within_one_session_only(demo_env, live_now):
    _state, _pcfg, cfg = demo_env
    url = cfg.database_url
    first, second = start(url, now=live_now).session, start(url, client="198.51.100.4", now=live_now).session
    assert send_sample(url, first, "us-hosting-october")["status"] == "ready"
    changed = send_sample(url, first, "us-hosting-november-other-account")
    assert changed["status"] == "needs_review" and codes(changed) == ["possible_bank_detail_change"]
    # Another visitor's session has no earlier hosting invoice: nothing to compare with
    alone = send_sample(url, second, "us-hosting-november-other-account")
    assert alone["status"] == "ready" and codes(alone) == []


def test_duplicate_file_is_detected_within_the_session_only(demo_env, live_now):
    _state, _pcfg, cfg = demo_env
    url = cfg.database_url
    first, second = start(url, now=live_now).session, start(url, client="198.51.100.4", now=live_now).session
    assert send_sample(url, first, "us-hosting-october")["status"] == "ready"
    again = send_sample(url, first, "us-hosting-october")
    assert again["status"] == "rejected" and codes(again) == ["duplicate_file"]
    assert send_sample(url, second, "us-hosting-october")["status"] == "ready"  # every visitor can use it


def test_upload_takes_pages_from_the_budget(demo_env, live_now):
    state, _pcfg, cfg = demo_env
    session = start(cfg.database_url, now=live_now).session
    body = process(queue(cfg.database_url, session, UPLOAD))
    assert state["provider"].calls == 1 and body["status"] in ("ready", "needs_review")
    rows = query(cfg.database_url, "SELECT period, pages FROM ap.demo_budget ORDER BY period")
    assert [(r["period"], r["pages"]) for r in rows] == [("day", 1), ("month", 1)]


def test_budget_used_up_rejects_without_calling_ocr(demo_env, live_now):
    state, pcfg, cfg = demo_env
    pcfg.demo = demo.Limits(enabled=True, hash_key=LIMITS.hash_key, pages_per_day=0)
    session = start(cfg.database_url, now=live_now).session
    body = process(queue(cfg.database_url, session, UPLOAD))
    assert body["status"] == "rejected" and codes(body) == ["demo_budget_reached"]
    assert state["provider"].calls == 0
    assert send_sample(cfg.database_url, session, "us-hosting-october")["status"] == "ready"  # samples still work


def test_failed_ocr_gives_its_pages_back(demo_env, live_now):
    from app.providers import ProviderError

    state, _pcfg, cfg = demo_env
    state["provider"] = FakeProvider(error=ProviderError("server", "down", retryable=True))
    session = start(cfg.database_url, now=live_now).session
    upload_id = queue(cfg.database_url, session, UPLOAD)
    r = TestClient(app).post(f"/demo/uploads/{upload_id}/process", headers=AUTH)
    assert r.status_code == 503
    assert {r["pages"] for r in query(cfg.database_url, "SELECT pages FROM ap.demo_budget")} == {0}
    [upload] = query(cfg.database_url, "SELECT status, error, content FROM ap.demo_uploads")
    assert (upload["status"], upload["error"], upload["content"]) == ("failed", "provider_server", None)


def test_files_per_session_are_enforced_by_processing_too(demo_env, live_now):
    """Every channel counts: an email's attachments reach processing without the page's queue check."""
    _state, pcfg, cfg = demo_env
    pcfg.demo = demo.Limits(enabled=True, hash_key=LIMITS.hash_key, files_per_session=1)
    session = start(cfg.database_url, now=live_now).session
    assert send_sample(cfg.database_url, session, "us-hosting-october")["status"] == "ready"
    form = {"channel": "email", "source_ref": "gmail:m9:a1", "scope": "demo", "demo_session_id": session.id}
    r = TestClient(app).post("/invoices/process", headers=AUTH, files={"file": ("i.pdf", UPLOAD)}, data=form)
    assert r.status_code == 200 and codes(r.json()) == ["demo_file_limit"]


def test_a_demo_file_needs_its_session_and_only_demo_files_have_one(demo_env):
    c = TestClient(app)
    data = {"channel": "email", "source_ref": "gmail:m1:a1"}
    no_session = c.post(
        "/invoices/process", headers=AUTH, files={"file": ("i.pdf", UPLOAD)}, data=data | {"scope": "demo"}
    )
    assert no_session.status_code == 422
    stray = c.post(
        "/invoices/process",
        headers=AUTH,
        files={"file": ("i.pdf", UPLOAD)},
        data=data | {"demo_session_id": "00000000-0000-0000-0000-000000000000"},
    )
    assert stray.status_code == 422


def test_an_upload_is_processed_once(demo_env, live_now):
    _state, _pcfg, cfg = demo_env
    session = start(cfg.database_url, now=live_now).session
    upload_id = queue(cfg.database_url, session, sample_bytes("us-hosting-october"), "us-hosting-october")
    process(upload_id)
    assert TestClient(app).post(f"/demo/uploads/{upload_id}/process", headers=AUTH).status_code == 409


# =====================================================================================
# Email to invoices+CODE@
# =====================================================================================
def screen(code: str, sender: str = "visitor@example.test", message_id: str = "m1"):
    body = {
        "recipients": [f"invoices+{code}@davidmichel.dev"],
        "delivered_to": f"invoices+{code}@davidmichel.dev",
        "attachments": [{"key": "attachment_0", "mime_type": "application/pdf", "file_name": "i.pdf", "size": 50000}],
        "sender": sender,
        "message_id": message_id,
    }
    r = TestClient(app).post("/intake/email/screen", headers=AUTH, json=body)
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture
def demo_on(cfg, monkeypatch):
    monkeypatch.setattr(main, "get_demo_limits", lambda: LIMITS)
    return cfg


def test_email_to_a_live_session(demo_on, live_now):
    session = start(demo_on.database_url, now=live_now).session
    s = screen(session.code)
    assert (s["route"], s["scope"], s["demo_session_id"]) == ("process", "demo", session.id)
    [msg] = query(demo_on.database_url, "SELECT session_id, channel, item_id FROM ap.demo_messages")
    assert (str(msg["session_id"]), msg["channel"], msg["item_id"]) == (session.id, "email", "m1")
    [sender] = query(demo_on.database_url, "SELECT sender_digest, emails FROM ap.demo_senders")
    assert sender["emails"] == 1 and "visitor" not in sender["sender_digest"]
    assert screen(session.code)["route"] == "process"  # the re-check of the same message isn't counted again
    assert query(demo_on.database_url, "SELECT emails FROM ap.demo_senders")[0]["emails"] == 1


def test_email_to_an_unknown_or_ended_session_is_refused(demo_on, live_now):
    s = screen("k7m2qxab")
    assert (s["route"], s["reason"], s["accepted"]) == ("demo_refused", "demo_session_expired", [])
    [log] = query(demo_on.database_url, "SELECT message, scope, details FROM ap.processing_log")
    assert (log["message"], log["scope"], log["details"]) == (
        "demo email refused",
        "demo",
        {"reason": "session_expired"},
    )


def test_emails_per_sender_and_day(demo_on, live_now, monkeypatch):
    monkeypatch.setattr(main, "get_demo_limits", lambda: demo.Limits(True, LIMITS.hash_key, emails_per_sender_day=2))
    session = start(demo_on.database_url, now=live_now).session
    assert screen(session.code, message_id="m1")["route"] == "process"
    assert screen(session.code, message_id="m2")["route"] == "process"
    refused = screen(session.code, message_id="m3")
    assert (refused["route"], refused["reason"]) == ("demo_refused", "demo_sender_limit")
    assert screen(session.code, sender="other@example.test", message_id="m4")["route"] == "process"


# =====================================================================================
# Clean-up
# =====================================================================================
def test_expired_sessions_go_with_their_invoices_and_files(demo_env, live_now, monkeypatch):
    _state, _pcfg, cfg = demo_env
    url = cfg.database_url
    old = start(url, now=live_now - timedelta(minutes=59)).session
    current = start(url, client="198.51.100.4", now=live_now).session
    send_sample(url, old, "us-hosting-october")
    send_sample(url, current, "us-hosting-october")
    with db.connect(url) as conn:
        demo.admit_email(conn, LIMITS, old, "visitor@example.test", "gmail-msg-1")
    [stored] = query(url, "SELECT file_path FROM ap.invoices WHERE demo_session_id = %s", old.id)
    assert (Path(cfg.storage_dir) / stored["file_path"]).exists()

    later = live_now + timedelta(minutes=2)
    monkeypatch.setattr(main, "datetime", type("Clock", (), {"now": staticmethod(lambda tz=None: later)}))
    r = TestClient(app).post("/demo/cleanup", headers=AUTH)
    assert r.status_code == 200, r.text
    assert r.json() == {
        "sessions": 1,
        "invoices": 1,
        "files": 1,
        "trash": [{"channel": "email", "item_id": "gmail-msg-1"}],
    }
    assert not (Path(cfg.storage_dir) / stored["file_path"]).exists()
    assert [str(r["id"]) for r in query(url, "SELECT id FROM ap.demo_sessions")] == [current.id]
    assert query(url, "SELECT count(*) AS n FROM ap.invoices")[0]["n"] == 1
    logs = query(url, "SELECT count(*) AS n FROM ap.processing_log WHERE scope = 'demo'")[0]["n"]
    assert logs > 0  # the audit trail stays (it never holds document content)

    done = TestClient(app).post(
        "/demo/trash/done", headers=AUTH, json={"channel": "email", "item_ids": ["gmail-msg-1"]}
    )
    assert done.json() == {"removed": 1}
    assert query(url, "SELECT count(*) AS n FROM ap.demo_trash")[0]["n"] == 0


def test_every_sample_has_its_recorded_result_and_fits_the_limits():
    """A sample without its recorded response would cost an OCR call on every click."""
    for s in demo_samples.CATALOG:
        content = s.file.read_bytes()
        stored = demo_samples.stored_result(hashlib.sha256(content).hexdigest())
        assert stored is not None and stored.annotation, s.key
        assert demo.check_file(content, LIMITS) == stored.pages
