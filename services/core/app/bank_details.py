"""Possible change of a supplier's bank details: the classic payment-diversion fraud.

Deterministic code only. Two signals, either one sends the invoice to review:
  * announcement: the provider read that the document announces new bank details (it can only raise
    this flag, never lower it), or the document's text or the email's text uses change wording
    (phrase lists below: EN, FR, DE, ES, IT, NL, PT; accents and case ignored)
  * comparison: an account on the invoice is not among the accounts on this supplier's earlier
    invoices (the baseline, read by db.bank_baseline: not rejected, never flagged themselves)

Bank details read from a document are never trusted. Full numbers are never stored or logged:
accounts are kept masked (last 4 characters) with a keyed fingerprint (HMAC-SHA256) to compare them.
The only full copies are the document itself and its OCR text.
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from pydantic import SecretStr

from .models import BankAccount, CommonInvoice, mask_number
from .normalize import normalize_bank_number

FINGERPRINT_LENGTH = 32  # hex characters (128 bits): plenty to tell accounts apart

_BANK_EN = r"(bank(ing)?|payment|remittance|account)"
_DETAILS_EN = r"(details|information|info|account|coordinates)"
_BANK_FR = r"(coordonnees\s+bancaires|rib|iban|compte(\s+bancaire)?|banque|domiciliation(\s+bancaire)?)"
_BANK_DE = r"(bankverbindung|bankdaten|kontoverbindung|kontodaten|iban|konto|bank)"
_BANK_ES = r"(datos\s+bancarios|cuenta(\s+bancaria)?|iban|banco|coordenadas\s+bancarias)"
_BANK_IT = r"(coordinate\s+bancarie|iban|conto(\s+corrente)?|dati\s+bancari)"
_BANK_NL = r"(bankgegevens|rekeningnummer|bankrekening(nummer)?|iban)"
_BANK_PT = r"(dados\s+bancarios|conta(\s+bancaria)?|iban|banco|nib)"

# Wording that announces new or changed bank details, on text folded by _fold (no accents, lower case)
CHANGE_WORDING: dict[str, tuple[str, ...]] = {
    "en": (
        rf"\b(new|changed|updated|different)\s+{_BANK_EN}\s+{_DETAILS_EN}\b",
        rf"\bchange\s+(of|in|to)\s+(our\s+)?{_BANK_EN}\s+{_DETAILS_EN}\b",
        rf"\b(update|amend|change)\s+(our|the|your\s+records\s+(of|with)\s+our)\s+{_BANK_EN}\s+{_DETAILS_EN}\b",
        rf"\b{_BANK_EN}\s+{_DETAILS_EN}\s+(have|has)\s+changed\b",
        r"\b(moved|changed|switched)\s+banks?\b",
        r"\bto\s+our\s+new\s+(bank\s+)?account\b",
    ),
    "fr": (
        rf"\bnouv(eaux?|elles?|el)\s+{_BANK_FR}\b",
        rf"\b(changement|modification)\s+(de|des|du)\s+(nos\s+|notre\s+)?{_BANK_FR}\b",
        r"\b(coordonnees\s+bancaires|rib)\s+(ont|a)\s+change\b",
        r"\bmettre\s+a\s+jour\s+(nos|notre|vos)\s+(coordonnees\s+bancaires|rib)\b",
    ),
    "de": (
        rf"\bneue[nrs]?\s+{_BANK_DE}\b",
        r"\b(anderung|wechsel)\s+(der|unserer|unsere)\s+(bankverbindung|bankdaten|kontoverbindung|kontodaten)\b",
        r"\b(bankverbindung|bankdaten|kontoverbindung)\s+(hat|haben)\s+sich\s+geandert\b",
        r"\b(bankverbindungs|bankdaten|konto)anderung\b",
    ),
    "es": (
        rf"\bnuev[oa]s?\s+{_BANK_ES}\b",
        rf"\bcambio\s+de\s+(nuestr[oa]s?\s+)?{_BANK_ES}\b",
        r"\b(actualizar|modificar)\s+(nuestros|sus|los)\s+datos\s+bancarios\b",
        r"\bdatos\s+bancarios\s+han\s+cambiado\b",
    ),
    "it": (
        rf"\bnuov[aoei]\s+{_BANK_IT}\b",
        rf"\b(cambio|variazione|modifica)\s+(delle\s+|del\s+|dei\s+)?(nostr[aoei]\s+)?{_BANK_IT}\b",
    ),
    "nl": (
        rf"\bnieuwe?\s+{_BANK_NL}\b",
        rf"\b(wijziging|gewijzigde?)\s+(van\s+)?(onze\s+)?{_BANK_NL}\b",
    ),
    "pt": (
        rf"\bnov[oa]s?\s+{_BANK_PT}\b",
        rf"\b(alteracao|mudanca)\s+(de|dos|da|do)\s+(nossos\s+|nossa\s+|nosso\s+)?{_BANK_PT}\b",
    ),
}
_COMPILED = {lang: [re.compile(p) for p in patterns] for lang, patterns in CHANGE_WORDING.items()}
_NUMBERS = ("iban", "bic", "account_number", "routing_code")


@dataclass(frozen=True)
class KnownAccount:
    """An account on one of the supplier's earlier invoices: its fingerprint and masked label."""

    fingerprint: str
    label: str


@dataclass(frozen=True)
class BankCheck:
    accounts: list[dict[str, Any]]  # stored form: masked numbers, holder, label, fingerprint
    notice_sources: list[str]  # document | document_text:<lang> | email:<lang>
    new_accounts: list[str] = field(default_factory=list)  # labels not on the earlier invoices
    known_accounts: list[str] = field(default_factory=list)  # labels on the earlier invoices

    @property
    def first_seen(self) -> bool:
        """Bank details on the invoice, but none on this supplier's earlier invoices to compare with."""
        return bool(self.accounts) and not self.known_accounts


def _fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.replace("ß", "ss"))
    plain = "".join(c for c in decomposed if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", plain).lower()


def change_wording(text: str | None) -> str | None:
    """The language whose change wording the text uses, or None."""
    if not text:
        return None
    folded = _fold(text)
    return next((lang for lang, patterns in _COMPILED.items() if any(p.search(folded) for p in patterns)), None)


def _value(secret: SecretStr | None) -> str:
    return secret.get_secret_value() if secret else ""


def fingerprint(account: BankAccount, key: bytes) -> str | None:
    """Keyed fingerprint of the account itself: the IBAN, otherwise routing code + account number.
    None without an account number (a BIC alone names a bank, not an account)."""
    if not key:
        raise ValueError("BANK_FINGERPRINT_KEY is not set")
    if account.iban:
        identity = f"iban:{_value(account.iban)}"
    elif account.account_number:
        identity = f"account:{_value(account.routing_code)}:{_value(account.account_number)}"
    else:
        return None
    return hmac.new(key, identity.encode(), hashlib.sha256).hexdigest()[:FINGERPRINT_LENGTH]


def stored_form(account: BankAccount, key: bytes) -> dict[str, Any]:
    """What is kept of an account: masked numbers, holder, label and fingerprint."""
    return account.model_dump() | {"label": account.label(), "fingerprint": fingerprint(account, key)}


def notice_sources(inv: CommonInvoice, email_text: str | None) -> list[str]:
    sources = ["document"] if inv.bank_change_notice else []
    if lang := change_wording(inv.ocr_text):
        sources.append(f"document_text:{lang}")
    if lang := change_wording(email_text):
        sources.append(f"email:{lang}")
    return sources


def assess(inv: CommonInvoice, email_text: str | None, baseline: list[KnownAccount], key: bytes) -> BankCheck:
    """The invoice's bank details against the announcement wording and the supplier's earlier accounts."""
    accounts = [stored_form(a, key) for a in inv.bank_accounts]
    known = {k.fingerprint for k in baseline}
    new = [a["label"] for a in accounts if known and a["fingerprint"] and a["fingerprint"] not in known]
    return BankCheck(
        accounts=accounts,
        notice_sources=notice_sources(inv, email_text),
        new_accounts=list(dict.fromkeys(new)),
        known_accounts=list(dict.fromkeys(k.label for k in baseline)),
    )


def mask_annotation(raw: dict[str, Any]) -> dict[str, Any]:
    """The provider's raw answer with the bank numbers of its annotation masked (a copy).
    An annotation that isn't valid JSON is kept as received: like the OCR text, it is a copy of the
    document, and nothing was read from it."""
    annotation = raw.get("document_annotation")
    parsed = annotation
    if isinstance(annotation, str):
        try:
            parsed = json.loads(annotation)
        except ValueError:
            return raw
    if not isinstance(parsed, dict) or not isinstance(parsed.get("bank_accounts"), list):
        return raw
    masked = copy.deepcopy(parsed)
    for entry in masked["bank_accounts"]:
        for name in _NUMBERS:
            if isinstance(entry, dict) and isinstance(entry.get(name), str):
                entry[name] = mask_number(normalize_bank_number(entry[name]) or "")
    stored = json.dumps(masked, ensure_ascii=False) if isinstance(annotation, str) else masked
    return raw | {"document_annotation": stored}
