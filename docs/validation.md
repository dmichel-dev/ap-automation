# Validation and routing

All checks are deterministic Python ([`validation.py`](../services/core/app/validation.py)). The OCR model only extracted the data; it takes no part in any decision.

## Routing principle

| Status | When | What happens next |
|---|---|---|
| `rejected` | **Only when a human could do nothing with it:** not an invoice, unreadable (no text), exact duplicate file (same SHA-256) | Logged with the reason; the file isn't kept |
| `needs_review` | Anything a human can check or fix | Review item opened (Day 3: approve / correct / reject) |
| `ready` | Every check passed | Posted to QuickBooks automatically (bill + original file) |

## Checks

| Check | Reason code(s) | Result |
|---|---|---|
| Readable text (≥ 20 characters) | `unreadable` | rejected |
| Document type | `not_invoice` | rejected |
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
| Confidence below the threshold | `low_confidence`, `no_ocr_confidence` | review |
| Service period ends before it starts | `service_period_inverted` | review |
| Service period ends before the month of the invoice date (cut-off), material | `cutoff_prior_period` | review |
| … not material | `cutoff_prior_period` | information only (posted; listed for month-end accruals) |
| An item reaches the capitalisation threshold | `possible_capex` | review (expense or fixed asset: a person decides) |
| At posting: invoice date or end of service period on or before the book close date | `period_closed` | posting review (nothing is posted into a closed period) |

`currency_from_country` (e.g. `$` read as USD for a US supplier) and a non-material `cutoff_prior_period` are information only, stored in `checks.info`.

## Company accounting policy settings

These are company policies, not accounting rules, so they are settings (`.env`), in the home currency:

| Setting | Default | Meaning |
|---|---|---|
| `CUTOFF_MATERIALITY` | 1000 | A service period ending in an earlier month (calendar months) goes to review at or above this total; below it, the invoice is posted and only listed for the month-end accruals. |
| `CAPEX_THRESHOLD` | 2500 | **Capitalisation policy:** an item whose cost (unit price when printed, otherwise the line amount, before tax) reaches this amount may be a fixed asset, so a person decides. $2,500 is a common anchor (the IRS de minimis safe harbor for businesses without audited financial statements), not a GAAP rule; each company sets its own. |
| `app/tax_treatment.json` | non-recoverable | How each tax is booked, by tax type and supplier country (see the README, *Accounting considerations*). |

**Foreign-currency invoices, until FX conversion exists (Day 4):** their amount in the home currency isn't known at validation. A prior-period service period therefore always goes to review (conservative), and the capitalisation check is skipped and marked `checked: false` in `checks.capex`, because comparing yen or euros with a dollar threshold would be wrong.

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
- **The threshold (0.85, `CONFIDENCE_THRESHOLD`) is provisional.** It will be calibrated on the test set on Day 3.

## Safety properties

- **Idempotent:** the same `(channel, source_ref)` returns the stored result, so n8n can retry safely.
- **No race conditions:** advisory locks on the source, the file hash and the duplicate key serialize concurrent copies. This is tested with two simultaneous submissions.
- **No long transactions:** OCR runs outside any transaction. Storing the invoice, lines, taxes, review item and log is one transaction, and the stored file is removed if it fails.
- **Privacy:** `processing_log` stores codes and IDs only, never document content (tested). Rejected files are not kept.
