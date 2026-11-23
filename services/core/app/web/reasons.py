"""Why an item needs a person, in plain language: the one place that turns the core's reason codes
into words for a reviewer. Display only; the codes and what they mean for routing stay in the core.

Every code the core can produce has an entry: a test scans the core's source for them.
"""

from typing import NamedTuple


class Reason(NamedTuple):
    label: str  # a few words, for the badge in the list
    sentence: str  # a full sentence, for the item page


REASONS: dict[str, Reason] = {
    # Reading the document
    "unreadable": Reason("Unreadable", "The document has no readable text."),
    "not_invoice": Reason("Not an invoice", "This document doesn't look like an invoice or a credit note."),
    "extraction_invalid": Reason(
        "Data incomplete", "The data read from the document is incomplete or malformed, so it can't be checked."
    ),
    "low_confidence": Reason(
        "Hard to read",
        "The document was hard to read, so the extracted data may contain mistakes. Compare it with the original.",
    ),
    "no_ocr_confidence": Reason(
        "Reading quality unknown", "The reading service gave no quality score, so the data can't be trusted unchecked."
    ),
    "unparseable_amount": Reason("Amount unreadable", "An amount on the invoice couldn't be read as a number."),
    "unparseable_date": Reason("Date unreadable", "A date on the invoice couldn't be read."),
    "unparseable_rate": Reason("Tax rate unreadable", "A tax rate on the invoice couldn't be read as a number."),
    "missing_field": Reason("Information missing", "Information needed to post the bill is missing."),
    "no_line_items": Reason("No line items", "No line items were found on the invoice."),
    # Dates
    "ambiguous_date": Reason(
        "Date reads two ways",
        "A date on the invoice can be read two ways, day first or month first. Confirm the right reading.",
    ),
    "inconsistent_dates": Reason("Dates disagree", "Dates on the invoice contradict each other."),
    "due_before_invoice_date": Reason("Due before issued", "The due date is earlier than the invoice date."),
    "service_period_inverted": Reason("Service period reversed", "The service period ends before it starts."),
    "cutoff_prior_period": Reason(
        "Earlier period",
        "The service was delivered in an earlier accounting period, for an amount above the materiality limit. "
        "Check which period the cost belongs to.",
    ),
    # Currency
    "missing_currency": Reason("No currency", "No currency was found on the invoice."),
    "ambiguous_currency": Reason(
        "Currency unclear", "The currency sign could mean more than one currency. Confirm which one is billed."
    ),
    "currency_from_country": Reason(
        "Currency assumed", "No currency is printed, so it was taken from the supplier's country. Confirm it."
    ),
    "currency_not_in_document": Reason(
        "Currency not printed", "The currency read from the invoice doesn't appear in the document. Confirm it."
    ),
    # Amounts
    "total_mismatch": Reason("Total doesn't add up", "The lines and tax don't add up to the invoice total."),
    "lines_mismatch": Reason("Lines don't add up", "The line items don't add up to the subtotal."),
    "tax_mismatch": Reason("Tax doesn't add up", "The tax amounts don't match their rates and taxable bases."),
    "adjustment_repeats_tax": Reason(
        "Tax counted twice", "A tax amount also appears as a separate charge line, so it may be counted twice."
    ),
    "wrong_sign": Reason(
        "Wrong sign", "The amounts have the wrong sign for this kind of document, such as a negative invoice."
    ),
    "classification_conflict": Reason(
        "Invoice or credit note?", "It's unclear whether this document is an invoice or a credit note."
    ),
    # Coding and suppliers
    "expense_account_missing": Reason(
        "Account needed",
        "No expense account yet: the invoice doesn't name one and the supplier has no default. Choose one.",
    ),
    "possible_capex": Reason(
        "Possible fixed asset",
        "An item costs more than the capitalisation threshold. Decide whether it's an expense or a fixed asset.",
    ),
    "new_vendor": Reason(
        "New supplier", "This supplier isn't in QuickBooks yet. Check it's a genuine supplier before it's created."
    ),
    # Duplicates and fraud
    "duplicate_file": Reason("Already received", "This exact file was already received."),
    "possible_duplicate": Reason(
        "Possible duplicate", "An invoice with the same supplier and number was already received."
    ),
    "possible_bank_detail_change": Reason(
        "Bank details changed?",
        "Possible fraud: the supplier's bank details may have changed. Confirm with the supplier through a "
        "contact you already had, not one given in this document.",
    ),
    # Posting to QuickBooks
    "vendor_currency_conflict": Reason(
        "Supplier currency clash",
        "QuickBooks already has this supplier in another currency, and a supplier's currency can't change. "
        "Choose or create a different supplier.",
    ),
    "vendor_inactive": Reason("Supplier inactive", "The matching supplier is inactive in QuickBooks."),
    "period_closed": Reason(
        "Books closed",
        "The books are closed for this invoice's date. Post it in an open period, or reopen the period.",
    ),
    "duplicate_in_accounting": Reason(
        "Already in QuickBooks", "QuickBooks already has several documents with this number for this supplier."
    ),
    # Bank matching
    "ambiguous_match": Reason(
        "Several bank matches", "Several bank transactions could be this payment. Choose the right one."
    ),
}
FALLBACK = Reason("Needs a check", "Something on this invoice needs a person's check.")


def explain(code: str) -> Reason:
    return REASONS.get(code, FALLBACK)
