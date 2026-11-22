# Validation and routing

All checks are deterministic Python ([`validation.py`](../services/core/app/validation.py)). The OCR model only extracted the data; it takes no part in any decision.

## Routing principle

| Status | When | What happens next |
|---|---|---|
| `rejected` | **Only when a human could do nothing with it:** not an invoice, unreadable (no text), exact duplicate file (same SHA-256) | Logged with the reason; the file isn't kept |
| `needs_review` | Anything a human can check or fix | Review item opened (Day 3: approve / correct / reject) |
| `ready` | Every check passed | Auto-posted to QuickBooks (Day 2) |

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

`currency_from_country` (e.g. `$` read as USD for a US supplier) is information only, stored in `checks.info`.

## Totals: lines, discounts, shipping, rounding, tax

Discounts, shipping, fees and rounding count wherever they're printed:
- **In the line-item table**, as rows with `kind = discount | shipping | fee`.
- **In the totals section**, as *adjustments* with their position (`before_tax` / `after_tax`). These are stored as lines too, so on Day 2 they become separate QuickBooks bill lines.

Sign rules:
- Discounts are always negative, whether or not a minus sign is printed.
- Withholding tax is negative.
- For credit notes, everything is flipped after that.

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
