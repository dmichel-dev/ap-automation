# Confidence calibration

The core sends an invoice to review when its confidence score is below `CONFIDENCE_THRESHOLD` (0.85). The review page shows the same score as a coloured badge: red below the threshold, orange up to `REVIEW_CONFIDENCE_HIGH` (0.95), green from there. This page records how those two cut-offs were checked against real results, and what the score can and can't tell.

```
confidence = OCR page-average confidence × required-field completeness
```

Both parts are computed by code and stored separately ([validation](validation.md#confidence)). The model never reports its own confidence.

## Key finding

**The score measures how well the page was read, not whether the data is right.** The only wrong extraction so far (invoice D, below) scored **0.982**, higher than most correct ones. The **totals check** caught it, not the score.

So the threshold is one control among several, and not the main one for data correctness. Most errors are caught by the deterministic checks.

## Two kinds of error, and what catches each

| | Reading error | Interpretation error |
|---|---|---|
| **What goes wrong** | The OCR sees the wrong characters: a 3 read as an 8, a lost decimal separator, a missed line, a blurred tax ID | The text is read correctly but put in the wrong place or given the wrong meaning: a tax counted twice, day and month swapped, net taken for gross, a credit note read as an invoice, a letter read as an invoice |
| **Typical cause** | Poor input: low resolution, skew, phone photos, stamps, handwriting | Layout and conventions: withholding taxes, discounts printed after the total, local date formats, unusual documents |
| **Does the confidence score see it?** | **Partly.** A poorly read page has a lower page-average score, so the threshold sends it to review. A single misread digit on an otherwise clean page barely moves the average | **No.** Every character was read correctly, so the score stays high |
| **Controls that catch it** | Confidence threshold (poor page) · required-field completeness (field not found) · **totals check** (a misread amount almost never balances) · format checks in normalization (unreadable amount, date or currency) · the second duplicate rule (same supplier, total and dates, another number: a misread invoice number) | **Totals check** (lines + adjustments + tax = total) · sign check (invoice positive, credit note negative) · ambiguous-date flag with a suggested reading · document-type check and `classification_conflict` · due date before invoice date · normalization rules for known patterns (e.g. `adjustment_repeats_tax`) · the same checks again when a reviewer approves |
| **What can still get through** | A misread digit in a field outside the arithmetic (invoice number, tax ID, a date that is still a valid date) on a page that otherwise reads well | A wrong field that still balances, e.g. supplier and buyer swapped. This is why a supplier's first invoice always goes to review (`new_vendor`) |

Every one of these controls is deterministic code ([validation](validation.md)). The model only extracts data and takes no part in any decision.

## First calibration (2026-11-24)

**Data:** every document processed on the live pipeline so far (16 distinct documents; 15 have an expected result, the first smoke test has none), compared with its expected result in `test-data/invoices/*.expected.json` by `scripts/run_e2e.sh` (read-only), plus the review queue (decisions and corrections).

| Document | Routed at intake | Confidence | OCR average × fields found | Extraction correct? |
|---|---|---|---|---|
| A: French invoice, EUR, discount | ready | 0.983 | 0.9831 × 1.0 | yes |
| B: Japanese invoice, JPY, two rates | ready | 0.978 | 0.9777 × 1.0 | yes |
| C: German invoice, printed total wrong | needs_review (`total_mismatch`) | 0.982 | 0.9823 × 1.0 | yes: the invoice itself is wrong; the reviewer rejected it |
| **D: Spanish invoice, IRPF withholding** | needs_review (`ambiguous_date`, `total_mismatch`) | **0.982** | 0.9821 × 1.0 | **no: interpretation error** (below) |
| E: UK credit note, scanned image (PNG) | ready | 0.974 | 0.9737 × 1.0 | yes, stored negative |
| F: letter asking to change bank details | rejected (`not_invoice`) | 0.389 | 0.9723 × 0.4 | yes: not an invoice, so most invoice fields are absent (1) |
| I: US invoice, exact bank payment | ready | 0.976 | 0.9757 × 1.0 | yes |
| J: US invoice, payment within tolerance | ready | 0.978 | 0.9779 × 1.0 | yes |
| K1, K2: two US invoices, same amount | ready | 0.976, 0.976 | 0.9759, 0.9756 × 1.0 | yes |
| L: US invoice, prior-period service | ready | 0.975 | 0.9752 × 1.0 | yes |
| M: US invoice, possible fixed asset | needs_review (`possible_capex`) | 0.976 | 0.9760 × 1.0 | yes; approved |
| N1: US invoice, new supplier | needs_review (`new_vendor`, `expense_account_missing`) | 0.977 | 0.9765 × 1.0 | yes; approved |
| N2: US invoice, known supplier | ready | 0.977 | 0.9766 × 1.0 | yes |
| O: US invoice, supplier with no default account | needs_review (`expense_account_missing`) | 0.974 | 0.9736 × 1.0 | yes; approved |
| First smoke test (US invoice, before the test set) | ready | 0.978 | 0.9775 × 1.0 | no expected file; no correction |
| A and C sent again | rejected (`duplicate_file`) | n/a | no OCR call | n/a |
| Daily end-to-end test invoices (2) | ready / needs_review (`total_mismatch`) | 0.977, 0.977 | 0.9765, 0.9772 × 1.0 | yes (the test checks them every day) |

(1) F's expected result has since changed: a bank-change request now goes to review as a fraud alert (`possible_bank_detail_change`). This copy was processed before that check existed. Its reading was correct either way.

**Invoice D, an interpretation error.** The OCR read every value on the page correctly. The structured output then returned the 15 % IRPF withholding twice, once as a tax line and once as an "other" adjustment after tax, so it was deducted twice: 1,560.00 + 327.60 VAT − 234.00 − 234.00 = 1,419.60 against a printed total of 1,653.60 (`total_mismatch`). The routing worked as intended: the invoice went to review, not to QuickBooks. The cause was fixed with a deterministic normalization rule (an "other" adjustment repeating a tax line is counted once and recorded as `adjustment_repeats_tax`), with unit tests and a live re-check. The reviewer later rejected this copy as superseded test data.

**Reviewer corrections:** none. No review item, live or test, needed a field corrected because of a misreading. All 9 review decisions so far were judgement calls (fixed asset, new supplier, expense account, bank matching, write-off) or rejections (C's wrong total, D as superseded test data).

**Scores:** all 15 invoices and credit notes scored between **0.974 and 0.983**. The one non-invoice scored 0.389 because its completeness was 0.4. **No document scored between 0.39 and 0.97.**

### Recommendation

- **Keep the threshold at 0.85.** Any threshold between 0.39 and 0.97 routes these documents exactly the same way, so the data gives no reason to move it. Above 0.974 it would send every clean invoice to review, with no wrong extraction caught: D scored 0.982.
- **Keep green at ≥ 0.95.** Every correctly read document is green. So is D, which is expected: green means "the page read well", not "the data is right". The totals and other checks answer that.

### What this rests on, and its limits

- **16 distinct documents** (15 invoices or credit notes, 1 letter), plus 2 daily-test invoices from the same generator. That is too few to set a threshold statistically.
- **All generated by us** from clean templates: 15 PDFs and 1 synthetic PNG scan. There are no real scans, phone photos, skewed or low-resolution pages, stamps or handwriting, which is where reading errors happen. So **the threshold has not yet been tested where it matters.**
- **Only 1 wrong extraction**, and it was the kind the score can't see.
- **One OCR provider** (`mistral-ocr-latest`). Another provider's scores would need their own calibration.
- **Fields compared:** routing status and reasons, document type, invoice number, currency, supplier country, dates, total and number of lines. Supplier name, tax IDs and individual line amounts are not compared one by one. Line amounts are covered indirectly by the totals check.

## Next: full calibration on the 20-invoice test set

The 20-invoice test set will include **deliberately degraded scans: low resolution, skewed pages and phone photos**, so that reading errors actually occur and the threshold is tested on the documents it exists for. For each document: the confidence score and its parts, and every extracted field compared with the expected result, counting reading and interpretation errors separately. The threshold is then set as the lowest value that sends every badly read document to review that the deterministic checks would not already catch, and the badge cut-offs are reviewed with it. Results will be added to this page.
