"""What the review page shows and how its decision panel posts: plain-language reasons, the confidence
badge, the list rows, details hidden by default, and the single decision panel. Display only: the
decisions themselves are tested in test_web.py and test_review.py."""
# ruff: noqa: F811  (pytest fixtures imported from test_web are redefined as test arguments)

import ast
import json
import re
from datetime import UTC, datetime, timedelta
from html import escape as _escape
from pathlib import Path

import pytest
from app import accounting, review
from app.web import display
from app.web.reasons import REASONS
from app.web.settings import WebSettings
from pydantic import ValidationError

from .test_processing import env  # noqa: F401  (fixture)
from .test_web import INTERNAL, browser, csrf, items, q, review_row, settings, sign_in  # noqa: F401

APP = Path(review.__file__).parent
CODE = re.compile(r"[a-z]+(_[a-z]+)*")
# Calls whose first argument is a reason code (an expression may hold several, e.g. a conditional)
CODE_CALLS = {"_issue", "fail", "add", "Issue", "NormalizationError"}


def _codes_in(tree: ast.AST) -> set[str]:
    found = set()
    for node in ast.walk(tree):
        exprs: list[ast.expr] = []
        if isinstance(node, ast.Call):
            f = node.func
            name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
            exprs = ([node.args[0]] if node.args and name in CODE_CALLS else []) + [
                k.value for k in node.keywords if k.arg == "code"
            ]
        elif isinstance(node, ast.Dict):
            exprs = [
                v
                for k, v in zip(node.keys, node.values, strict=True)
                if isinstance(k, ast.Constant) and k.value == "code"
            ]
        for e in exprs:
            found |= {
                c.value
                for c in ast.walk(e)
                if isinstance(c, ast.Constant) and isinstance(c.value, str) and CODE.fullmatch(c.value)
            }
    return found


def core_reason_codes() -> set[str]:
    """Every reason code the core can put on a review item, read from its source."""
    codes: set[str] = set()
    for path in APP.rglob("*.py"):
        if "web" not in path.relative_to(APP).parts:
            codes |= _codes_in(ast.parse(path.read_text()))
    return codes | set(accounting.REVIEW_ERRORS) | set(accounting.MATCHING_REVIEWS) | set(review.FRAUD_CODES)


def escape(text):
    """As the templates escape it (Jinja writes an apostrophe as &#39;)."""
    return _escape(text).replace("&#x27;", "&#39;")


def set_reasons(url, review_id, *reasons, days_ago=0):
    q(
        url,
        "UPDATE ap.review_queue SET reasons = %s::jsonb, created_at = now() - make_interval(days => %s) WHERE id = %s",
        json.dumps([{"code": c, "field": None, "message": m} for c, m in reasons]),
        days_ago,
        review_id,
    )


def page_of(c, review_id):
    r = c.get(f"/items/{review_id}")
    assert r.status_code == 200
    return r.text


# =====================================================================================
# Plain-language reasons: one place, every code
# =====================================================================================
def test_every_reason_code_the_core_produces_has_a_plain_sentence():
    codes = core_reason_codes()
    assert len(codes) >= 30  # the scan really finds the core's codes
    assert {"total_mismatch", "unparseable_amount", "ambiguous_currency", "ambiguous_match"} <= codes
    assert sorted(codes - set(REASONS)) == []


@pytest.mark.parametrize("code", sorted(REASONS))
def test_reasons_read_as_plain_language(code):
    label, sentence = REASONS[code]
    for text in (label, sentence):
        assert "_" not in text and code not in text
    assert len(label) <= 28 and label[0].isupper()
    assert sentence[0].isupper() and sentence.endswith(".")


def test_the_item_page_explains_why_in_sentences_without_codes(items):
    c, url, live, _demo = items
    set_reasons(url, live, ("possible_capex", "Line 1 costs 4,800.00 USD."), ("ambiguous_date", "Printed 04/11/2026."))
    page = page_of(c, live)
    why = page[page.index('class="why') : page.index("</div>", page.index('class="why'))]
    assert "Why it needs review" in why
    assert escape(REASONS["possible_capex"].sentence) in why and escape(REASONS["ambiguous_date"].sentence) in why
    assert "Line 1 costs 4,800.00 USD." in why  # the core's specific detail, under the sentence
    assert "possible_capex" not in page and "possible capex" not in page


# =====================================================================================
# Confidence badge (display only)
# =====================================================================================
@pytest.mark.parametrize(
    "score, shown, level",
    [
        (0.9775, "97.75%", "high"),
        (0.95, "95.00%", "high"),  # green starts exactly at the high band
        (0.9499, "94.99%", "medium"),
        (0.85, "85.00%", "medium"),  # the threshold itself is not below the threshold
        (0.8499, "84.99%", "low"),
        ("0.12345", "12.35%", "low"),  # two decimals, halves rounded up
        (None, "–", "unknown"),
    ],
)
def test_confidence_bands_follow_the_review_threshold(score, shown, level):
    badge = display.confidence_badge(score, 0.85, 0.95)
    assert (badge.percent, badge.level, badge.threshold) == (shown, level, "85.00%")
    assert badge.label == display.LEVEL_LABELS[level]  # the colour is always said in words too


def test_the_badge_sits_next_to_the_extracted_data_title(items):
    c, _url, live, _demo = items  # the test invoice: OCR 0.97 × all required fields = 0.97
    page = page_of(c, live)
    head = page[page.index('class="extracted-head"') : page.index('class="why') if 'class="why' in page else None]
    assert "Extracted data (OCR)" in head and "OCR confidence" in head
    assert 'class="conf level-high"' in head and "97.00%" in head and ">high<" in head
    assert "threshold 85.00%" in head


@pytest.mark.parametrize("threshold, high, level", [(0.9, 0.98, "medium"), (0.98, 0.99, "low")])
def test_the_bands_are_configurable_and_red_is_the_review_threshold(items, threshold, high, level):
    signed_in, url, live, _demo = items
    s = settings(url, signed_in.app.state.settings.file_storage_dir).model_copy(
        update={"confidence_threshold": threshold, "review_confidence_high": high}
    )
    c = browser(s, INTERNAL)
    assert sign_in(c).status_code == 303
    page = page_of(c, live)
    assert f'class="conf level-{level}"' in page and f"threshold {display.percent(threshold)}" in page


@pytest.mark.parametrize("threshold, high", [(0.96, 0.95), (0, 0.95), (0.85, 1.2)])
def test_bands_out_of_order_stop_the_page_at_startup(clean_db, threshold, high):
    with pytest.raises(ValidationError, match="confidence bands"):
        WebSettings(
            ap_database_url=clean_db,
            review_listener="internal",
            review_origins=INTERNAL,
            confidence_threshold=threshold,
            review_confidence_high=high,
        )


def test_the_badge_changes_no_stored_value(items):
    c, url, live, _demo = items
    before = q(
        url, "SELECT confidence, ocr_confidence_avg FROM ap.invoices i JOIN ap.review_queue r ON r.invoice_id = i.id"
    )
    page_of(c, live)
    assert (
        q(url, "SELECT confidence, ocr_confidence_avg FROM ap.invoices i JOIN ap.review_queue r ON r.invoice_id = i.id")
        == before
    )


# =====================================================================================
# Layout: titled panels, details hidden by default, no inline code
# =====================================================================================
def test_panels_are_titled_and_details_start_closed(items):
    c, _url, live, _demo = items
    page = page_of(c, live)
    assert "Original invoice (as received)" in page and "Extracted data (OCR)" in page
    summaries = re.findall(r"<summary>([^<]+)</summary>", page)
    assert summaries == [
        "Show details: line items (2)",
        "Show details: tax lines (1)",
        "Show details: raw values",
        "Show details: history",
    ]
    assert "<details open" not in page and "<details>" in page


def test_pages_carry_no_inline_code(items):
    c, _url, live, _demo = items
    for page in (c.get("/items").text, page_of(c, live)):
        assert not re.search(r"<script(?![^>]*\bsrc=)", page)  # every script comes from a file
        assert not re.search(r"\sstyle=|<style|\son[a-z]+=", page)


# =====================================================================================
# The decision panel: one form, only the allowed actions, approve first
# =====================================================================================
def radios(page):
    return re.findall(r'<input type="radio" class="act" name="action" value="(\w+)" id="act-\w+" ?(checked)?>', page)


def test_the_panel_offers_only_allowed_actions_with_approve_chosen(items):
    c, url, live, _demo = items
    assert radios(page_of(c, live)) == [("approve", "checked"), ("correct", ""), ("reject", "")]
    set_reasons(url, live, ("possible_bank_detail_change", "New bank account requested."))
    page = page_of(c, live)
    assert radios(page) == [("verify", "checked"), ("reject", "")]  # fraud: verify (never approve) or reject
    assert ">Possible fraud<" in page


def test_a_decided_item_has_no_panel(items):
    c, url, live, _demo = items
    q(
        url,
        "UPDATE ap.review_queue SET status = 'rejected', resolved_at = now(), reviewer = 'Rita Reviewer' WHERE id = %s",
        live,
    )
    page = page_of(c, live)
    assert radios(page) == [] and 'action="/items/' not in page.replace('action="/logout"', "")


def test_the_panel_posts_only_the_chosen_actions_fields(items):
    c, url, live, _demo = items
    r = c.post(
        f"/items/{live}",
        data={
            "csrf": csrf(c),
            "action": "approve",
            "approve.account_id": "20",
            "approve.default": "keep",
            "approve.note": "checked against the PO",
            "correct.invoice_number": "SHOULD-NOT-APPLY",
            "reject.note": "should not apply",
        },
    )
    assert r.status_code == 303 and r.headers["location"] == f"/items/{live}?done=approve"
    row = review_row(url, live)
    assert row["status"] == "approved" and row["decision_note"] == "checked against the PO"
    assert row["decision"]["account_id"] == "20"
    number = q(url, "SELECT invoice_number FROM ap.invoices WHERE id = %s", row["invoice_id"])[0]["invoice_number"]
    assert number != "SHOULD-NOT-APPLY"


def test_rejecting_in_the_panel_uses_the_reject_reason(items):
    c, url, live, _demo = items
    data = {"csrf": csrf(c), "action": "reject", "reject.note": "not our order", "approve.note": "ignored"}
    assert c.post(f"/items/{live}", data=data).status_code == 303
    row = review_row(url, live)
    assert row["status"] == "rejected" and row["decision_note"] == "not our order"


def test_correcting_in_the_panel_changes_only_what_changed(items):
    c, url, live, _demo = items
    page = page_of(c, live)
    fields = dict(re.findall(r'name="(correct\.\w+)" value="([^"]*)"', page))
    r = c.post(
        f"/items/{live}", data={"csrf": csrf(c), "action": "correct", **fields, "correct.invoice_number": "QM-1042-A"}
    )
    assert r.status_code == 303
    row = review_row(url, live)
    assert row["status"] == "open" and [list(x["after"]) for x in row["corrections"]] == [["invoice_number"]]


@pytest.mark.parametrize("action", [None, "", "approve_all", "write_off"])
def test_the_panel_needs_a_known_action(items, action):
    c, url, live, _demo = items
    data = {"csrf": csrf(c), "approve.account_id": "20"} | ({"action": action} if action is not None else {})
    r = c.post(f"/items/{live}", data=data)
    assert r.status_code == 422 and "choose what to do with this invoice" in r.text
    assert review_row(url, live)["status"] == "open"


def test_the_core_still_decides_which_actions_are_allowed(items):
    c, url, live, _demo = items  # not a fraud item: "verify" isn't offered, and the core refuses it
    r = c.post(
        f"/items/{live}",
        data={"csrf": csrf(c), "action": "verify", "verify.account_id": "20", "verify.note": "phoned them"},
    )
    assert r.status_code == 409 and review_row(url, live)["status"] == "open"


def test_after_an_error_the_panel_stays_on_the_action_tried(items):
    c, _url, live, _demo = items
    r = c.post(f"/items/{live}", data={"csrf": csrf(c), "action": "reject", "reject.note": ""})
    assert r.status_code == 422
    assert ("reject", "checked") in radios(r.text) and ("approve", "") in radios(r.text)


def test_the_panel_is_protected_like_every_form(items):
    c, url, live, _demo = items
    assert c.post(f"/items/{live}", data={"action": "reject", "reject.note": "no token"}).status_code == 403
    assert review_row(url, live)["status"] == "open"


# =====================================================================================
# List page: supplier, amount, age, one plain reason
# =====================================================================================
def test_list_rows_show_supplier_amount_age_and_one_plain_reason(items):
    c, url, live, _demo = items
    set_reasons(url, live, ("ambiguous_date", "Printed 04/11/2026."), ("total_mismatch", "Off by 36.00."), days_ago=3)
    page = c.get("/items").text
    row = page[page.index(f'href="/items/{live}"') : page.index("</a>", page.index(f'href="/items/{live}"'))]
    assert "Quillmoor Paper Supply LLC" in row and "641.24 USD" in row and "3 days" in row
    assert escape(REASONS["ambiguous_date"].label) in row and "and 1 more" in row
    assert escape(REASONS["total_mismatch"].label) not in row  # one badge per row
    assert "ambiguous_date" not in page and "ambiguous date" not in page


def test_list_rows_are_coloured_by_status_and_say_it_in_words(items):
    c, url, live, _demo = items
    assert f'<a class="row tone-review" href="/items/{live}">' in c.get("/items").text
    set_reasons(url, live, ("possible_bank_detail_change", "New bank account requested."))
    assert f'<a class="row tone-fraud" href="/items/{live}">' in c.get("/items").text
    q(
        url,
        "UPDATE ap.review_queue SET status = 'approved', resolved_at = now(), reviewer = 'Rita Reviewer' WHERE id = %s",
        live,
    )
    page = c.get("/items?status=approved").text
    assert f'<a class="row tone-ready" href="/items/{live}">' in page and ">Approved<" in page


@pytest.mark.parametrize("days, shown", [(0, "today"), (1, "1 day"), (12, "12 days")])
def test_age_is_counted_in_whole_days(days, shown):
    now = datetime(2026, 11, 23, 9, 0, tzinfo=UTC)
    assert display.age((now - timedelta(days=days, hours=1)).isoformat(), now) == shown
