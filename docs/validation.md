# Validation and routing

All checks are deterministic Python ([`validation.py`](../services/core/app/validation.py)). The OCR model only extracted the data; it takes no part in any decision.

## Routing principle

| Status | When | What happens next |
|---|---|---|
| `rejected` | **Only when a human could do nothing with it:** not an invoice, unreadable (no text), exact duplicate file (same SHA-256) | Logged with the reason; the file isn't kept |
| `needs_review` | Anything a human can check or fix | Review item opened: approve, correct or reject on the [review page](review.md) |
| `ready` | Every check passed | Posted to QuickBooks automatically (bill + original file) |

## Checks

| Check | Reason code(s) | Result |
|---|---|---|
| Readable text (≥ 20 characters) | `unreadable` | rejected |
| Document type | `not_invoice` | rejected (review if it asks to change bank details, see below) |
| … but it has an invoice number and a total | `classification_conflict` | review (guards against misclassification or a document that says "this is not an invoice") |
| Exact same file already received | `duplicate_file` | rejected (before any OCR cost) |
| Provider output invalid, but text is readable | `extraction_invalid` | review |
| Required fields: supplier, invoice number, invoice date, currency, total | `missing_field` | review |
| Normalization issues (ambiguous date, unreadable amount, ambiguous currency, currency not in the document…) | see [common-invoice-format.md](common-invoice-format.md) | review |
| Sign: invoice total < 0, or credit note total > 0 | `wrong_sign` | review |
| Due date before invoice date | `due_before_invoice_date` | review |
| No line items | `no_line_items` | review |
| Totals (below) | `lines_mismatch`, `tax_mismatch`, `total_mismatch` | review |
| Same supplier + document type + number, different file | `possible_duplicate` | review |
| Same supplier + document type + total + currency, invoice dates within 7 days, **another number** | `possible_duplicate` | review (the same bill resent with a new number, or a misread number) |
| Supplier not known yet (first invoice) | `new_vendor` | review: is it a genuine supplier? (supplier onboarding control) |
| The document or the email announces new bank details, or an account differs from the supplier's earlier invoices | `possible_bank_detail_change` | review as **possible fraud**: never posted automatically (see below) |
| Bank details on the invoice, none on the supplier's earlier invoices | `bank_details_first_seen` | information only |
| Supplier has no default account | `expense_account_missing` | review: the reviewer chooses the account, saved as the supplier's default |
| Confidence below the threshold | `low_confidence`, `no_ocr_confidence` | review |
| Service period ends before it starts | `service_period_inverted` | review |
| Service period ends before the month of the invoice date (cut-off), material | `cutoff_prior_period` | review |
| … not material | `cutoff_prior_period` | information only (posted; listed for month-end accruals) |
| An item reaches the capitalisation threshold | `possible_capex` | review (expense or fixed asset: a person decides) |
| At posting: invoice date or end of service period on or before the book close date | `period_closed` | posting review (nothing is posted into a closed period) |

`currency_from_country` (e.g. `$` read as USD for a US supplier) and a non-material `cutoff_prior_period` are information only, stored in `checks.info`.

**Known supplier:** it has a default account, or it is linked to a QuickBooks vendor (`vendor_links`), or **exactly one** active QuickBooks vendor has the same normalized name (legal forms such as LLC or SARL removed, our `(EUR)` currency suffix ignored). Name matching is exact on purpose: a fuzzy match would let a look-alike name ("Quilmoor Paper") pass a control meant to catch it. Two QuickBooks vendors with the same name count as unknown.

A supplier that is known but has no default account (e.g. a QuickBooks vendor, or a supplier posted before this rule existed) gets only `expense_account_missing`, once. Decisions and their rules: [review.md](review.md).

## Company accounting policy settings

These are company policies, not accounting rules, so they are settings (`.env`), in the home currency:

| Setting | Default | Meaning |
|---|---|---|
| `CUTOFF_MATERIALITY` | 1000 | A service period ending in an earlier month (calendar months) goes to review at or above this total; below it, the invoice is posted and only listed for the month-end accruals. |
| `CAPEX_THRESHOLD` | 2500 | **Capitalisation policy:** an item whose cost (unit price when printed, otherwise the line amount, before tax) reaches this amount may be a fixed asset, so a person decides. $2,500 is a common anchor (the IRS de minimis safe harbor for businesses without audited financial statements), not a GAAP rule; each company sets its own. |
| `app/tax_treatment.json` | non-recoverable | How each tax is booked, by tax type and supplier country (see the README, *Accounting considerations*). |

**Foreign-currency invoices (no home-currency conversion at validation yet; on the roadmap):** their amount in the home currency isn't known at validation. A prior-period service period therefore always goes to review (conservative), and the capitalisation check is skipped and marked `checked: false` in `checks.capex`, because comparing yen or euros with a dollar threshold would be wrong.

**Closed periods:** QuickBooks' book close date (`Preferences.AccountingInfoPrefs.BookCloseDate`) is read by the posting workflow before each claim. The core posts nothing on or before it, and nothing at all while it can't be read.

## Totals: lines, discounts, shipping, rounding, tax

Discounts, shipping, fees and rounding count wherever they're printed:
- **In the line-item table**, as rows with `kind = discount | shipping | fee`.
- **In the totals section**, as *adjustments* with their position (`before_tax` / `after_tax`). These are stored as lines too, so they become separate QuickBooks bill lines.

Sign rules:
- Discounts are always negative, whether or not a minus sign is printed.
- Withholding tax is negative.
- For credit notes, everything is flipped after that.

Counted once: when the model returns a tax (usually a withholding such as Spanish IRPF) both as a tax line and as an `other` totals adjustment with the same amount, the adjustment is dropped and recorded as the info issue `adjustment_repeats_tax`. Only `other` adjustments are affected; table rows and real discount/shipping/fee/rounding lines are never dropped. Found by the end-to-end test on 2026-11-22.

| Check | Formula | Tolerance |
|---|---|---|
| Lines | `subtotal` = all before-tax lines, **or** = item lines only (subtotal printed before the discount) | 1 minor unit × lines |
| Tax | `tax_total` = sum of tax lines (with or without withholding) | 1 minor unit × tax lines |
| Total, tax exclusive | `total` = before-tax lines + tax + after-tax lines (e.g. shipping after tax, rounding) | 1 minor unit × (lines + tax lines) |
| Total, tax inclusive | `total` = before-tax lines + after-tax lines (prices include tax, common in Japan) | same |

A "minor unit" follows the currency: 0.01 USD, 1 JPY, 0.001 KWD. The formula that matched is stored in `tax_mode`.

## Confidence

```
confidence = OCR page-average confidence × required-field completeness
```

- Both parts are stored separately (`ocr_confidence_avg`, `confidence_completeness`) so reviewers can see why a score is low. The threshold in force at the time is stored too (`confidence_threshold`).
- An ambiguous but printed date counts as present for completeness, since it's reported separately as `ambiguous_date`.
- The OCR *minimum* page score is stored but not used. It was 0.13–0.41 even on clean documents.
- **Threshold: 0.85** (`CONFIDENCE_THRESHOLD`), kept after a first calibration on the invoices processed so far; the full calibration comes with the larger test set. The score measures how well the page was read, not whether the data is right: see [calibration](calibration.md).

## Possible bank-detail change (fraud alert)

Payment diversion is the classic AP fraud: a letter or an invoice asks to pay "our new account". Bank details read from a document are **never trusted**, and a possible change is **never posted automatically**. Code in [`bank_details.py`](../services/core/app/bank_details.py), deterministic.

**Fields read** (any country, all optional): IBAN, BIC/SWIFT, account number, routing code (routing/ABA, sort code, BSB, IFSC, bank code), account holder.

**Two signals; either one gives `possible_bank_detail_change`:**

1. **Announcement.** Any of these three is enough:
   - the provider reports that the document announces new or changed bank details (any language);
   - the document's text uses change wording;
   - the email's subject or body uses change wording.

   The change wording comes from a phrase list in English, French, German, Spanish, Italian, Dutch and Portuguese, with accents and case ignored. The provider can only *raise* the flag: a document telling the AI to ignore the change still hits the phrase list and the comparison.
2. **Comparison.** An account on the invoice is not among the accounts on this supplier's earlier invoices. An account is identified by its IBAN, otherwise by routing code + account number. A BIC alone names a bank, not an account.

**What the invoice is compared with (the baseline):** accounts on earlier invoices of the same supplier (same supplier key: tax ID, else normalized name) that were **not rejected** and **never flagged** themselves. A flagged invoice stays out of the baseline even after a reviewer verified it: "verified with the supplier" does not save the details, because a record of verified details from a trusted source is on the roadmap. So after a genuine change, the supplier's next invoices are flagged again until then. The check runs under the supplier lock, so two invoices arriving together see each other.

**First-time bank details:**

| Case | Result |
|---|---|
| New supplier | Review anyway (`new_vendor`). The page shows the details as first seen and unverified. Once a person approves the invoice, its details become the baseline. |
| Known supplier whose earlier invoices showed no bank details | `bank_details_first_seen`, information only (stored in `checks.info` for the daily summary). |
| No bank details on the invoice | Nothing to compare; announcements still count. |

**A letter that is not an invoice** but asks to change bank details is a fraud alert (`needs_review`, `not_invoice` kept as information), not a plain rejection. The same applies to an unreadable file sent with a change-request email. An email with **no invoice attached** that asks for a change is labelled **AP/Fraud alert** and logged by the email workflow. It has no review item yet.

**Known limits:**
- Email text is checked only with the phrase list (seven languages). Documents in any language are also covered by the provider's flag.
- An OCR misread of one digit gives a false alert. That fails safe: a person looks at it.
- Changing `BANK_FINGERPRINT_KEY` resets the comparison: every supplier's next bank details are "first seen".
- At go-live the baseline is empty, because invoices processed before this check have no stored bank details.
- **An auto-posted invoice from a known supplier showing bank details for the first time becomes part of the baseline without a person seeing them** (`bank_details_first_seen` is information only, listed in the daily summary). Making it a review reason would send every known supplier's next invoice to review.

## Safety properties

- **Idempotent:** the same `(channel, source_ref)` returns the stored result, so n8n can retry safely.
- **No race conditions:** advisory locks on the source, the file hash and the duplicate key serialize concurrent copies. This is tested with two simultaneous submissions.
- **No long transactions:** OCR runs outside any transaction. Storing the invoice, lines, taxes, review item and log is one transaction, and the stored file is removed if it fails.
- **Privacy:** `processing_log` stores codes and IDs only, never document content (tested). Rejected files are not kept. Bank numbers are stored and logged masked only (see [security.md](security.md#bank-details)).
