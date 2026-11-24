"""Bank details: masking, fingerprints, change wording (several languages) and the comparison with
the supplier's earlier invoices. All numbers are fictional and invalid."""

import json

import pytest
from app.bank_details import KnownAccount, assess, change_wording, fingerprint, mask_annotation
from app.extraction import build_common_invoice
from app.models import BankAccount, mask_number
from app.providers.base import ProviderResult

from .conftest import BANK_KEY as KEY
from .conftest import OCR_TEXT, base_annotation

OLD_IBAN = "GB00 FAKE 0000 0000 0000 21"
NEW_IBAN = "GB00 FAKE 9999 9999 9999 17"


def account(**fields):
    return {"iban": None, "bic": None, "account_number": None, "routing_code": None, "account_holder": None} | fields


def invoice(accounts=(), notice=False, ocr_text=OCR_TEXT, **overrides):
    annotation = base_annotation(bank_accounts=list(accounts), bank_details_change_notice=notice, **overrides)
    result = ProviderResult(annotation=annotation, ocr_text=ocr_text, pages=1, ocr_confidence_avg=0.98)
    return build_common_invoice(result, "test")


def baseline(*ibans):
    accounts = [BankAccount(iban=i.replace(" ", "")) for i in ibans]
    return [KnownAccount(fingerprint(a, KEY) or "", a.label()) for a in accounts]


# --- masking: last 4 characters only, everywhere ---------------------------------------
@pytest.mark.parametrize(
    ("value", "masked"),
    [("GB00FAKE00000000000021", "•••• 0021"), ("00001234", "•••• 1234"), ("123456", "••••"), ("FAKEGB00", "•••• GB00")],
)
def test_mask_number_shows_the_last_4_only_when_enough_is_hidden(value, masked):
    assert mask_number(value) == masked


def test_full_numbers_never_appear_in_str_repr_or_json():
    inv = invoice([account(iban=NEW_IBAN, bic="FAKEGB00", account_number="99999917", routing_code="123456")])
    a = inv.bank_accounts[0]
    for text in (str(a), repr(a), a.model_dump_json(), inv.model_dump_json(), str(inv.model_dump())):
        assert "GB00FAKE9999" not in text and "99999917" not in text and "123456" not in text
    assert a.model_dump()["iban"] == "•••• 9917"
    assert a.model_dump()["account_holder"] is None
    assert a.label() == "IBAN •••• 9917"


def test_numbers_are_normalized_spaces_and_case():
    inv = invoice([account(iban="gb00 fake-9999 9999 9999 17", account_holder="  Thistlecombe  Ltd ")])
    a = inv.bank_accounts[0]
    assert a.iban is not None and a.iban.get_secret_value() == "GB00FAKE99999999999917"
    assert a.account_holder == "Thistlecombe Ltd"


def test_an_empty_account_entry_is_dropped():
    assert invoice([account(), account(account_holder="Somebody")]).bank_accounts == []


def test_stored_annotation_without_bank_fields_still_validates():
    annotation = base_annotation()
    assert "bank_accounts" not in annotation
    inv = build_common_invoice(ProviderResult(annotation=annotation, ocr_text=OCR_TEXT, pages=1), "test")
    assert inv.bank_accounts == [] and inv.bank_change_notice is False and inv.issues == []


# --- fingerprints -------------------------------------------------------------------------
def test_fingerprint_ignores_spacing_and_depends_on_the_key():
    a, b = BankAccount(iban="GB00FAKE00000000000021"), BankAccount(iban="GB00FAKE00000000000021")
    assert fingerprint(a, KEY) == fingerprint(b, KEY)
    assert fingerprint(a, KEY) != fingerprint(a, b"another-key")
    assert "0021" not in (fingerprint(a, KEY) or "")


def test_fingerprint_without_iban_uses_routing_and_account_number():
    a = BankAccount(routing_code="000000000", account_number="0000FAKE0017")
    b = BankAccount(routing_code="111111111", account_number="0000FAKE0017")
    assert fingerprint(a, KEY) and fingerprint(a, KEY) != fingerprint(b, KEY)
    assert fingerprint(BankAccount(bic="FAKEGB00"), KEY) is None  # a BIC alone is a bank, not an account


def test_an_empty_key_is_refused():
    with pytest.raises(ValueError):
        fingerprint(BankAccount(iban="GB00FAKE00000000000021"), b"")


# --- change wording, several languages ----------------------------------------------------
@pytest.mark.parametrize(
    ("text", "language"),
    [
        ("Please update our bank details for all future payments.", "en"),
        ("PAYMENT DETAILS HAVE CHANGED. We have moved banks.", "en"),
        ("Please pay this invoice to our new account.", "en"),
        ("Subject: Change of bank details – urgent", "en"),
        ("Veuillez noter nos nouvelles coordonnées bancaires.", "fr"),
        ("Changement de RIB : merci de mettre à jour votre fichier fournisseurs.", "fr"),
        ("Bitte beachten Sie unsere neue Bankverbindung.", "de"),
        ("Änderung unserer Bankverbindung ab sofort", "de"),
        ("Le informamos de nuestros nuevos datos bancarios.", "es"),
        ("Cambio de cuenta bancaria: por favor actualicen sus registros.", "es"),
        ("Vi comunichiamo le nuove coordinate bancarie.", "it"),
        ("Let op: ons nieuwe rekeningnummer.", "nl"),
        ("Informamos a alteração dos nossos dados bancários.", "pt"),
    ],
)
def test_change_wording_is_found(text, language):
    assert change_wording(text) == language


@pytest.mark.parametrize(
    "text",
    [
        "Bank details: IBAN GB00 FAKE 0000 0000 0000 21, BIC FAKEGB00",
        "Coordonnées bancaires : IBAN FR00 0000 0000 0000",
        "Bankverbindung: Fiktivbank, IBAN DE00 0000 0000",
        "Datos bancarios: IBAN ES00 0000 0000",
        "New account setup fee",
        "Our bank details have not changed.",
        "",
        None,
    ],
)
def test_ordinary_payment_details_are_not_a_change(text):
    assert change_wording(text) is None


# --- assessment: what the invoice is compared with -----------------------------------------
def test_no_bank_details_no_finding():
    check = assess(invoice(), None, baseline(OLD_IBAN), KEY)
    assert check.accounts == [] and not check.notice_sources and not check.new_accounts and not check.first_seen


def test_same_details_as_earlier_invoices():
    check = assess(invoice([account(iban=OLD_IBAN)]), None, baseline(OLD_IBAN), KEY)
    assert not check.new_accounts and not check.first_seen and not check.notice_sources


def test_changed_details_are_new_accounts_masked():
    check = assess(invoice([account(iban=NEW_IBAN)]), None, baseline(OLD_IBAN), KEY)
    assert check.new_accounts == ["IBAN •••• 9917"] and check.known_accounts == ["IBAN •••• 0021"]


def test_first_bank_details_seen_for_the_supplier():
    check = assess(invoice([account(iban=NEW_IBAN)]), None, [], KEY)
    assert check.first_seen and not check.new_accounts


def test_notice_sources_model_document_text_and_email():
    assert assess(invoice(notice=True), None, [], KEY).notice_sources == ["document"]
    text = OCR_TEXT + "\nPlease update our bank details."
    assert assess(invoice(ocr_text=text), None, [], KEY).notice_sources == ["document_text:en"]
    email = "Bonjour, veuillez noter nos nouvelles coordonnées bancaires."
    assert assess(invoice(), email, [], KEY).notice_sources == ["email:fr"]


def test_stored_accounts_hold_masked_values_and_the_fingerprint_only():
    check = assess(invoice([account(iban=NEW_IBAN, bic="FAKEGB00", account_holder="Thistlecombe Ltd")]), None, [], KEY)
    [stored] = check.accounts
    assert stored == {
        "iban": "•••• 9917",
        "bic": "•••• GB00",
        "account_number": None,
        "routing_code": None,
        "account_holder": "Thistlecombe Ltd",
        "label": "IBAN •••• 9917",
        "fingerprint": fingerprint(BankAccount(iban="GB00FAKE99999999999917"), KEY),
    }


# --- the provider's raw answer is stored masked ---------------------------------------------
@pytest.mark.parametrize("as_text", [True, False])
def test_mask_annotation_masks_numbers_in_the_raw_answer(as_text):
    annotation = base_annotation(bank_accounts=[account(iban=NEW_IBAN, account_holder="Thistlecombe Ltd")])
    raw = {"model": "x", "document_annotation": json.dumps(annotation) if as_text else annotation}
    masked = mask_annotation(raw)
    text = json.dumps(masked)
    assert "9999" not in text.replace("•••• 9917", "") and "Thistlecombe Ltd" in text
    assert raw["document_annotation"] != masked["document_annotation"]  # the input is not modified in place
