"""Supplier onboarding and expense coding, the second duplicate rule, the QuickBooks
reference copy, and review decisions (approve / verify / correct / reject). Real Postgres."""

import json
from datetime import date

import pytest
from app import accounting, db, postings, qbo_reference
from app.main import app
from fastapi.testclient import TestClient

from .conftest import AUTH, QUILLMOOR_KEY, known_supplier, query, seed_accounts
from .test_processing import FakeProvider, env, post  # noqa: F401  (env is a fixture)

OPEN_BOOKS = postings.BookClose(known=True)
c = TestClient(app)


@pytest.fixture
def new(env, cfg):  # noqa: F811
    """Processing and review on a database where the test supplier has never been seen."""
    query(cfg.database_url, "DELETE FROM ap.supplier_defaults")
    seed_accounts(cfg.database_url)
    return env[0], cfg


def q(cfg, sql, *args):
    return query(cfg.database_url, sql, *args)


def codes(body):
    return [r["code"] for r in body["reasons"]]


def item(review_id):
    return c.get(f"/review/items/{review_id}", headers=AUTH).json()


def act(review_id, action, **body):
    return c.post(f"/review/items/{review_id}/{action}", headers=AUTH, json=body)


# =====================================================================================
# Supplier onboarding and expense coding (processing)
# =====================================================================================
def test_first_invoice_of_a_new_supplier_goes_to_review(new):
    _state, cfg = new
    body = post().json()
    assert body["status"] == "needs_review" and codes(body) == ["new_vendor", "expense_account_missing"]
    checks = q(cfg, "SELECT checks FROM ap.invoices")[0]["checks"]
    assert checks["supplier"] == {"known": False, "basis": None, "default_account_id": None}


def test_supplier_linked_in_accounting_only_needs_an_account(new):
    _state, cfg = new
    q(
        cfg,
        "INSERT INTO ap.vendor_links (supplier_key, currency, external_id, display_name) VALUES (%s, 'USD', '58', 'Q')",
        QUILLMOOR_KEY,
    )
    body = post().json()
    assert codes(body) == ["expense_account_missing"]


def test_existing_accounting_vendor_with_the_same_name_is_known(new):
    _state, cfg = new
    vendors = {"Vendor": [{"Id": "61", "DisplayName": "Quillmoor Paper Supply LLC", "Active": True}]}
    with db.connect(cfg.database_url) as conn:
        qbo_reference.store(conn, "Vendor", 200, {"QueryResponse": vendors})
    body = post().json()
    assert codes(body) == ["expense_account_missing"]
    assert q(cfg, "SELECT checks FROM ap.invoices")[0]["checks"]["supplier"]["basis"] == "accounting_vendor_name"


def test_two_accounting_vendors_with_the_same_name_are_not_a_match(new):
    _state, cfg = new
    vendors = {
        "Vendor": [
            {"Id": "61", "DisplayName": "Quillmoor Paper Supply LLC", "Active": True},
            {"Id": "62", "DisplayName": "Quillmoor Paper Supply (USD)", "Active": True},
        ]
    }
    with db.connect(cfg.database_url) as conn:
        qbo_reference.store(conn, "Vendor", 200, {"QueryResponse": vendors})
    assert codes(post().json()) == ["new_vendor", "expense_account_missing"]


def test_ready_invoice_records_the_supplier_default_account(env, cfg):  # noqa: F811
    assert post().json()["status"] == "ready"
    row = q(cfg, "SELECT expense_account_id, expense_account_source FROM ap.invoices")[0]
    assert row == {"expense_account_id": "20", "expense_account_source": "supplier_default"}


def test_posting_uses_the_invoice_account(env, cfg):  # noqa: F811
    known_supplier(cfg.database_url, key="other", account="7")  # unrelated default: not used
    post()
    q(cfg, "UPDATE ap.invoices SET expense_account_id = '7', expense_account_source = 'reviewer'")
    with db.connect(cfg.database_url) as conn:
        items = accounting.claim_postings(conn, cfg, OPEN_BOOKS)
    accounts = {ln["AccountBasedExpenseLineDetail"]["AccountRef"]["value"] for ln in items[0]["document"]["Line"]}
    assert accounts == {"7"}


def test_invoice_without_an_account_is_not_posted(env, cfg):  # noqa: F811
    post()
    q(cfg, "UPDATE ap.invoices SET expense_account_id = NULL, expense_account_source = NULL")
    with db.connect(cfg.database_url) as conn:
        assert accounting.claim_postings(conn, cfg, OPEN_BOOKS) == []
    assert q(cfg, "SELECT last_error FROM ap.accounting_postings")[0]["last_error"].startswith(
        "expense_account_missing"
    )


# =====================================================================================
# Possible duplicate: same supplier, total and currency within 7 days, another number
# =====================================================================================
def resend(state, number, day, content):
    state["provider"] = FakeProvider(invoice_number=number, invoice_date=day, due_date="12/31/2026")
    r = post(content=content, source_ref=f"gmail:{number}:{len(content)}")
    assert r.status_code == 200, r.text
    return r.json()


def test_same_amount_within_7_days_with_another_number_is_a_possible_duplicate(env, cfg):  # noqa: F811
    state, _ = env
    first = resend(state, "QM-1042", "11/18/2026", b"%PDF-1.4 a")
    second = resend(state, "QM-1043", "11/25/2026", b"%PDF-1.4 b")
    assert second["status"] == "needs_review" and codes(second) == ["possible_duplicate"]
    assert (
        "dated within 7 days" in second["reasons"][0]["message"]
        and first["invoice_id"] in second["reasons"][0]["message"]
    )
    checks = q(cfg, "SELECT checks FROM ap.invoices WHERE id = %s", second["invoice_id"])[0]["checks"]
    assert checks["duplicate"]["rules"] == ["same_amount_near_date"]


def test_same_amount_8_days_apart_is_not_a_duplicate(env, cfg):  # noqa: F811
    state, _ = env
    resend(state, "QM-1042", "11/18/2026", b"%PDF-1.4 a")
    assert resend(state, "QM-1043", "11/26/2026", b"%PDF-1.4 b")["status"] == "ready"


def test_rejected_documents_do_not_count_as_duplicates(env, cfg):  # noqa: F811
    state, _ = env
    first = resend(state, "QM-1042", "11/18/2026", b"%PDF-1.4 a")
    q(cfg, "UPDATE ap.invoices SET status = 'rejected' WHERE id = %s", first["invoice_id"])
    assert resend(state, "QM-1043", "11/19/2026", b"%PDF-1.4 b")["status"] == "ready"


# =====================================================================================
# QuickBooks reference copy
# =====================================================================================
ACCOUNT_LIST = {
    "QueryResponse": {
        "Account": [
            {
                "Id": "20",
                "Name": "Supplies",
                "FullyQualifiedName": "Supplies",
                "AccountType": "Expense",
                "Active": True,
            },
            {"Id": "1", "Name": "Services", "FullyQualifiedName": "Services", "AccountType": "Income", "Active": True},
            {
                "Id": "88",
                "Name": "Unapplied Cash Bill Payment Expense",
                "AccountType": "Expense",
                "AccountSubType": "UnappliedCashBillPaymentExpense",
                "Active": True,
            },
        ]
    }
}


def test_accounts_are_stored_and_only_coding_accounts_are_offered(cfg):
    r = c.post("/reference/accounts", headers=AUTH, json={"status_code": 200, "body": ACCOUNT_LIST})
    assert r.json() == {"rows": 3, "deactivated": 0}
    assert c.get("/accounting/accounts", headers=AUTH).json() == [{"id": "20", "name": "Supplies", "type": "Expense"}]


def test_account_missing_from_the_next_sync_is_kept_inactive(cfg):
    c.post("/reference/accounts", headers=AUTH, json={"status_code": 200, "body": ACCOUNT_LIST})
    smaller = {"QueryResponse": {"Account": ACCOUNT_LIST["QueryResponse"]["Account"][1:]}}
    assert (
        c.post("/reference/accounts", headers=AUTH, json={"status_code": 200, "body": smaller}).json()["deactivated"]
        == 1
    )
    assert q(cfg, "SELECT active FROM ap.qbo_accounts WHERE external_id = '20'") == [{"active": False}]


FULL_PAGE = {"QueryResponse": {"Account": [{"Id": str(i)} for i in range(qbo_reference.PAGE_SIZE)]}}


@pytest.mark.parametrize("status, body", [(401, {"Fault": {}}), (200, {"QueryResponse": {}}), (200, FULL_PAGE)])
def test_unusable_account_responses_change_nothing(cfg, status, body):
    c.post("/reference/accounts", headers=AUTH, json={"status_code": 200, "body": ACCOUNT_LIST})
    r = c.post("/reference/accounts", headers=AUTH, json={"status_code": status, "body": body})
    assert r.status_code == 422 and r.json()["error"] == "sync_refused"
    assert len(q(cfg, "SELECT 1 FROM ap.qbo_accounts WHERE active")) == 3


def test_vendor_names_are_normalized_without_our_currency_suffix():
    assert qbo_reference.vendor_name_norm("Ateliers Verlibreux SARL (EUR)") == "ateliers verlibreux"


# =====================================================================================
# Review decisions
# =====================================================================================
def test_approve_requires_an_account_for_a_new_supplier(new):
    review_id = post().json()["review_id"]
    assert item(review_id)["actions"] == ["approve", "correct", "reject"]
    r = act(review_id, "approve", reviewer="david")
    assert r.status_code == 409 and "choose an account" in r.json()["message"]
    r = act(review_id, "approve", reviewer="david", account_id="1")  # an income account
    assert r.status_code == 409 and "can't be used" in r.json()["message"]


def test_first_approval_sets_the_default_and_the_next_invoice_is_ready(new):
    state, cfg = new
    first = post().json()
    r = act(first["review_id"], "approve", reviewer="david", account_id="7")
    assert r.status_code == 200 and r.json()["status"] == "approved"
    assert r.json()["decision"]["supplier_default"] == "created" and r.json()["actions"] == []
    inv = q(cfg, "SELECT status, expense_account_id, expense_account_source FROM ap.invoices")[0]
    assert inv == {"status": "ready", "expense_account_id": "7", "expense_account_source": "reviewer"}
    default = q(cfg, "SELECT expense_account_id, source, set_by FROM ap.supplier_defaults")[0]
    assert default == {"expense_account_id": "7", "source": "reviewer", "set_by": "david"}
    # the supplier is now known, with a default: its next invoice goes straight through
    second = resend(state, "QM-2001", "12/20/2026", b"%PDF-1.4 next")
    assert second["status"] == "ready"
    assert q(cfg, "SELECT expense_account_id FROM ap.invoices WHERE id = %s", second["invoice_id"])[0] == {
        "expense_account_id": "7"
    }
    log = q(cfg, "SELECT message, details FROM ap.processing_log WHERE step = 'review'")
    assert log[0]["message"] == "review approved" and log[0]["details"]["account_id"] == "7"


def test_one_off_account_does_not_become_the_default(new):
    _state, cfg = new
    review_id = post().json()["review_id"]
    act(review_id, "approve", reviewer="david", account_id="40", set_default=False)  # capitalised
    assert q(cfg, "SELECT * FROM ap.supplier_defaults") == []


def test_default_is_replaced_only_when_asked(env, cfg):  # noqa: F811
    state, _ = env
    state["provider"] = FakeProvider(total="USD 999.00")  # totals mismatch: a review, supplier known
    review_id = post().json()["review_id"]
    act(review_id, "correct", reviewer="david", changes={"total": "641.24"})
    r = act(review_id, "approve", reviewer="david", account_id="7")
    assert r.json()["status"] == "corrected" and r.json()["decision"]["supplier_default"] is None
    assert q(cfg, "SELECT expense_account_id FROM ap.supplier_defaults")[0]["expense_account_id"] == "20"


def test_approve_with_the_supplier_default(env, cfg):  # noqa: F811
    state, _ = env
    state["provider"] = FakeProvider(total="USD 999.00")
    review_id = post().json()["review_id"]
    act(review_id, "correct", reviewer="david", changes={"total": "641.24"})
    r = act(review_id, "approve", reviewer="david", set_default=True)
    assert r.json()["decision"]["account_source"] == "supplier_default" and r.json()["decision"]["account_id"] == "20"


def test_totals_that_do_not_add_up_block_the_approval(env, cfg):  # noqa: F811
    state, _ = env
    state["provider"] = FakeProvider(total="USD 999.00")
    review_id = post().json()["review_id"]
    assert [b["code"] for b in item(review_id)["blockers"]] == ["total_mismatch"]
    r = act(review_id, "approve", reviewer="david")
    assert r.status_code == 409 and r.json()["error"] == "blocked"
    assert [p["code"] for p in r.json()["problems"]] == ["total_mismatch"]
    assert q(cfg, "SELECT status FROM ap.invoices")[0]["status"] == "needs_review"


def test_correction_keeps_before_and_after(env, cfg):  # noqa: F811
    state, _ = env
    state["provider"] = FakeProvider(total="USD 999.00")
    review_id = post().json()["review_id"]
    body = act(review_id, "correct", reviewer="david", changes={"total": "641.24"}).json()
    assert body["blockers"] == [] and body["status"] == "open"
    [entry] = body["corrections"]
    assert entry["by"] == "david" and entry["before"] == {"total": "999.0000"} and entry["after"] == {"total": "641.24"}


def test_corrected_number_is_checked_for_duplicates_again(env, cfg):  # noqa: F811
    state, _ = env
    post(content=b"%PDF-1.4 one", source_ref="gmail:1:a")  # QM-1042, ready
    state["provider"] = FakeProvider(invoice_number="QM-9999", total="USD 999.00", invoice_date="10/01/2026")
    review_id = post(content=b"%PDF-1.4 two", source_ref="gmail:2:a").json()["review_id"]
    body = act(review_id, "correct", reviewer="david", changes={"invoice_number": "QM-1042"}).json()
    assert codes(body)[-1] == "possible_duplicate"
    assert q(cfg, "SELECT invoice_number_norm FROM ap.invoices WHERE id = %s", body["invoice"]["id"])[0] == {
        "invoice_number_norm": "QM1042"
    }


def test_required_fields_cannot_be_emptied(env, cfg):  # noqa: F811
    state, _ = env
    state["provider"] = FakeProvider(total="USD 999.00")
    review_id = post().json()["review_id"]
    r = act(review_id, "correct", reviewer="david", changes={"invoice_number": None})
    assert r.status_code == 409 and "not emptied" in r.json()["message"]


def test_suggested_dates_can_be_accepted(env, cfg):  # noqa: F811
    state, _ = env
    state["provider"] = FakeProvider(invoice_date="03/04/2026", due_date="05/04/2026")
    review_id = post().json()["review_id"]
    assert [b["code"] for b in item(review_id)["blockers"]] == ["missing_field"]  # no invoice date yet
    r = act(review_id, "approve", reviewer="david", accept_suggested_dates=True)
    assert r.json()["status"] == "corrected" and r.json()["decision"]["suggested_dates_accepted"]
    inv = q(cfg, "SELECT invoice_date, due_date FROM ap.invoices")[0]
    assert inv == {"invoice_date": date(2026, 3, 4), "due_date": date(2026, 5, 4)}


def test_fraud_item_has_no_plain_approve(env, cfg):  # noqa: F811
    state, _ = env
    state["provider"] = FakeProvider(total="USD 999.00")
    review_id = post().json()["review_id"]
    act(review_id, "correct", reviewer="david", changes={"total": "641.24"})
    fraud = [{"code": "possible_bank_detail_change", "field": None, "message": "new bank details"}]
    q(cfg, "UPDATE ap.review_queue SET reasons = reasons || %s::jsonb", json.dumps(fraud))
    assert item(review_id)["actions"] == ["verify", "reject"]
    assert act(review_id, "approve", reviewer="david").status_code == 409
    r = act(review_id, "verify", reviewer="david", note="ok")
    assert r.status_code == 409 and "how the change was verified" in r.json()["message"]
    r = act(review_id, "verify", reviewer="david", note="Called the number on file (not the one in the letter)")
    assert r.status_code == 200 and r.json()["status"] == "verified"
    assert q(cfg, "SELECT decision_note FROM ap.review_queue")[0]["decision_note"].startswith("Called the number")


def test_reject_needs_a_reason_and_deletes_the_file(new):
    _state, cfg = new
    review_id = post().json()["review_id"]
    path = q(cfg, "SELECT file_path FROM ap.invoices")[0]["file_path"]
    assert (cfg.storage_dir / path).exists()
    assert act(review_id, "reject", reviewer="david", note="").status_code == 422
    r = act(review_id, "reject", reviewer="david", note="Not our supplier")
    assert r.status_code == 200 and r.json()["status"] == "rejected"
    inv = q(cfg, "SELECT status, file_path, file_deletion_reason FROM ap.invoices")[0]
    assert inv == {"status": "rejected", "file_path": None, "file_deletion_reason": "rejected"}
    assert not (cfg.storage_dir / path).exists()


def test_a_decided_item_cannot_be_decided_again(new):
    review_id = post().json()["review_id"]
    act(review_id, "approve", reviewer="david", account_id="7")
    r = act(review_id, "reject", reviewer="eve", note="changed my mind")
    assert r.status_code == 409 and "approved" in r.json()["message"]


def test_posting_reviews_have_no_actions_yet(new):
    _state, cfg = new
    review_id = post().json()["review_id"]
    q(cfg, "UPDATE ap.review_queue SET stage = 'posting'")
    assert item(review_id)["actions"] == []


def test_review_list_filters_by_scope(new):
    review_id = post().json()["review_id"]
    assert [i["id"] for i in c.get("/review/items", headers=AUTH).json()] == [review_id]
    assert c.get("/review/items?scope=e2e", headers=AUTH).json() == []


def test_unknown_review_item_is_404(cfg):
    assert c.get("/review/items/999999", headers=AUTH).status_code == 404
