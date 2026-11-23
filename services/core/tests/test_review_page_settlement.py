"""The review page on posting and matching items: the bank panel, the choices the core allows, the
waiting state, and a QuickBooks failure shown with a retry. The decisions themselves are tested in
test_settlement.py."""

import re

import pytest

from .test_accounting import bank_page, claim, make_invoice, plaid_tx, posted_bill, register_bank, result, run, sql
from .test_review_page import radios
from .test_settlement import pay, review
from .test_web import INTERNAL, add_user, browser, csrf, settings, sign_in


@pytest.fixture
def page(cfg):
    add_user(cfg.database_url)
    c = browser(settings(cfg.database_url, cfg.storage_dir), INTERNAL)
    assert sign_in(c).status_code == 303
    return c, cfg


def text_of(html):
    return " ".join(re.sub(r"<[^>]+>", " ", html).split())


def test_an_ambiguous_match_shows_its_candidates_to_choose_from(page):
    c, cfg = page
    register_bank(cfg)
    inv = posted_bill(cfg)
    bank_page(cfg, plaid_tx("t1", 641.24), plaid_tx("t2", 641.24, day="2026-11-28", name="QUILLMOOR PAPER SUP 2"))
    run(cfg)
    rid = review(cfg, inv)["id"]
    html = c.get(f"/items/{rid}").text
    assert radios(html) == [("confirm_match", "checked"), ("reject_match", "")]
    assert "Bill and bank payment" in html and "Several bank transactions could be this payment" in html
    choices = re.findall(
        r'name="confirm_match\.bank_transaction_id" value="(\w+)" data-required-for="confirm_match"', html
    )
    assert choices == ["t1", "t2"]
    assert "2026-11-28 · “QUILLMOOR PAPER SUP 2” ·" in text_of(html)


def test_confirming_on_the_page_needs_a_transaction_then_waits(page):
    c, cfg = page
    register_bank(cfg)
    inv = posted_bill(cfg)
    bank_page(cfg, plaid_tx("t1", 641.24), plaid_tx("t2", 641.24, day="2026-11-28"))
    run(cfg)
    rid = review(cfg, inv)["id"]
    r = c.post(f"/items/{rid}", data={"csrf": csrf(c), "action": "confirm_match", "confirm_match.note": "x"})
    assert r.status_code == 422 and "bank_transaction_id" in r.text  # none chosen (without scripts)
    assert review(cfg, inv)["waiting_for"] is None
    data = {"csrf": csrf(c), "action": "confirm_match", "confirm_match.bank_transaction_id": "t2"}
    r = c.post(f"/items/{rid}", data=data)
    assert r.status_code == 303 and r.headers["location"] == f"/items/{rid}?done=confirm_match"
    html = c.get(r.headers["location"]).text
    assert "Match confirmed." in html and "Waiting for QuickBooks: bill payment" in html
    assert radios(html) == [] and 'action="/items/' not in html.replace('action="/logout"', "")
    rows = c.get("/items").text
    assert f'<a class="row tone-closed" href="/items/{rid}">' in rows and "Waiting for QuickBooks: bill payment" in rows


def test_a_payment_difference_offers_write_off_or_keep_open(page):
    c, cfg = page
    register_bank(cfg)
    inv = posted_bill(cfg)
    bank_page(cfg, plaid_tx("t1", 640.84))
    pay(run(cfg)["payments_due"][0]["match_id"])
    rid = review(cfg, inv)["id"]
    html = c.get(f"/items/{rid}").text
    assert radios(html) == [("write_off", "checked"), ("keep_open", "")]
    assert "Writes off 0.40 USD to the “Payment differences” account" in text_of(html)
    assert "Open difference 0.40 USD" in text_of(html)
    r = c.post(f"/items/{rid}", data={"csrf": csrf(c), "action": "write_off", "write_off.note": "bank fee"})
    assert r.status_code == 303
    html = c.get(f"/items/{rid}").text
    assert "Waiting for QuickBooks: write-off" in html and "Decided by Rita Reviewer" in html
    assert sql(cfg, "SELECT decided_by FROM ap.write_offs")[0]["decided_by"].startswith("Rita Reviewer")


def test_a_quickbooks_failure_shows_the_error_and_only_a_retry(page):
    c, cfg = page
    inv = make_invoice(cfg)
    claim(cfg)
    result(cfg, inv, outcome="failed", http_status=400, fault_code="6240", error_message="Duplicate Name Exists Error")
    rid = review(cfg, inv)["id"]
    html = c.get(f"/items/{rid}").text
    assert radios(html) == [("retry", "checked")]
    assert "QuickBooks refused this and no automatic retry is left" in html
    assert "qbo_6240: Duplicate Name Exists Error" in html
    r = c.post(f"/items/{rid}", data={"csrf": csrf(c), "action": "retry", "retry.note": "vendor renamed"})
    assert r.status_code == 303
    assert "Waiting for QuickBooks: bill posting" in c.get(f"/items/{rid}").text
