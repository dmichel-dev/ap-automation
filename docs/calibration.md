# Confidence calibration

The core sends an invoice to review when its confidence score is below `CONFIDENCE_THRESHOLD` (0.85). The review page shows the same score as a coloured badge: red below the threshold, orange up to `REVIEW_CONFIDENCE_HIGH` (0.95), green from there. This page records how those two cut-offs were checked against real results (two calibrations, the second on 21 documents including degraded scans and photos), and what the score can and can't tell.

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

## Second calibration (2026-11-24): 21 documents, with degraded copies

**Data:** 21 distinct documents. The 16 of the first calibration (the first smoke test from its stored copy, as its file is no longer in the test set) and 5 new ones:

- **P:** UAE tax invoice, Arabic and English on the same page, AED, 5 % VAT, supplier TRN.
- **Q:** Indian GST invoice, Hindi (Devanagari) and English, INR, GSTIN and HSN/SAC codes, CGST 9 % + SGST 9 %, Indian digit grouping (₹ 1,35,818.00).
- **R:** invoice B (Japanese) as a fax-like copy: 90 dpi, greyscale, strong JPEG compression.
- **S:** invoice A (French) as a crooked scan: turned 3.5°, 150 dpi, light blur and noise.
- **T:** invoice I (US) as a phone photo on a desk: perspective, shadow across one corner, blur, JPEG quality 50.

P and Q are shaped with HarfBuzz (Arabic joined right to left, Devanagari conjuncts and vowel signs) and were checked by eye before use. R, S and T keep their original's expected result.

**Method:** `scripts/calibrate.sh` runs every document through the core's own code (file check, Mistral OCR with the JSON-schema annotation, conversion to the common format, bank-details screening, all checks) and compares the result with its expected file. Nothing is stored and nothing is looked up in the database, so the three checks that need stored history can't fire: `new_vendor`, `expense_account_missing` and `possible_duplicate`. They are left out of the comparison, so N1 and O count as correct when they come out `ready`. Beyond the compared fields (status, reasons, document type, number, currency, supplier country, dates, total, number of lines), the supplier name, the supplier tax ID and every tax line were checked against the generator by hand.

| Document | Input | Confidence | OCR average × fields found | Lowest word confidence | Routed (without history checks) | Correct? | Notes |
|---|---|---|---|---|---|---|---|
| A: French, EUR, discount | PDF | 0.983 | 0.9829 × 1.0 | 0.28 | ready | yes | |
| B: Japanese, JPY, two rates | PDF | 0.977 | 0.9772 × 1.0 | 0.29 | ready | yes | |
| C: German, printed total wrong | PDF | 0.982 | 0.9824 × 1.0 | 0.30 | needs_review (`total_mismatch`) | yes | the invoice itself is wrong |
| D: Spanish, IRPF withholding | PDF | 0.982 | 0.9815 × 1.0 | 0.43 | needs_review (`ambiguous_date`) | yes | withholding returned once by the OCR this time (1) |
| E: UK credit note | PNG scan | 0.973 | 0.9730 × 1.0 | 0.30 | ready | yes | stored negative |
| F: bank-details letter | PDF | 0.389 | 0.9724 × 0.4 | 0.31 | needs_review (`possible_bank_detail_change`) | yes | fraud alert, not an invoice |
| I: US, exact payment | PDF | 0.977 | 0.9770 × 1.0 | 0.33 | ready | yes | |
| J: US, tolerance | PDF | 0.978 | 0.9779 × 1.0 | 0.36 | ready | yes | |
| K1, K2: US, same amount | PDF | 0.976, 0.976 | 0.9759, 0.9761 × 1.0 | 0.32, 0.31 | ready | yes | |
| L: US, prior-period service | PDF | 0.975 | 0.9748 × 1.0 | 0.28 | ready (info `cutoff_prior_period`) | yes | |
| M: US, possible fixed asset | PDF | 0.976 | 0.9759 × 1.0 | 0.24 | needs_review (`possible_capex`) | yes | |
| N1: US, new supplier | PDF | 0.977 | 0.9765 × 1.0 | 0.37 | ready | yes | live: `new_vendor`, `expense_account_missing` |
| N2: US, known supplier | PDF | 0.977 | 0.9768 × 1.0 | 0.38 | ready | yes | |
| O: US, no default account | PDF | 0.974 | 0.9736 × 1.0 | 0.33 | ready | yes | live: `expense_account_missing` |
| First smoke test (US) | PDF | 0.978 | 0.9779 × 1.0 | 0.41 | ready | no expected file | same values as I |
| **P: UAE, Arabic/English, AED** | PDF | 0.977 | 0.9766 × 1.0 | 0.30 | ready | yes | 15-digit TRN, 5 % VAT line exact |
| **Q: India, Hindi/English, INR** | PDF | 0.982 | 0.9816 × 1.0 | 0.23 | ready | yes (2) | GSTIN exact, Indian grouping read right |
| **R: B as a 90 dpi fax-like copy** | JPEG | 0.974 | 0.9743 × 1.0 | 0.20 | ready | yes | same values as B |
| **S: A as a crooked scan** | JPEG | 0.979 | 0.9787 × 1.0 | 0.21 | ready | yes | same values as A |
| **T: I as a phone photo** | JPEG | 0.974 | 0.9736 × 1.0 | 0.24 | ready | yes | same values as I |

(1) The normalization rule added after the first calibration (`adjustment_repeats_tax`) did not fire: the OCR did not repeat the withholding in this run. The rule still covers it if it does, and the totals check would catch any other double count.

(2) CGST and SGST were read as two 9 % tax lines of the right amount, but typed `vat` instead of `gst`: a small **interpretation error**. It changes nothing today: both types are non-recoverable in the demo company's tax policy (`app/tax_treatment.json`), and the totals balance. It would matter for a company that reclaims Indian GST and sets a `gst` rule for it.

### Results

- **20/20 documents match their expected result.** No reading errors. One minor interpretation error (Q's tax type, above), which affects neither routing nor posting.
- **Every supplier name, tax ID, total and tax line came out exactly**, the degraded copies included: the TRN, the GSTIN, the Japanese registration number and the French VAT number were read correctly in every version.
- **The degradations barely moved the score.** Each copy is within 0.004 of its original: R 0.974 vs B 0.977, S 0.979 vs A 0.983, T 0.974 vs I 0.977. The lowest word confidence does fall on the copies (0.20 to 0.24, against 0.28 to 0.33 for the originals), but a clean page can be as low (Q: 0.23), so it doesn't separate good from poor input either.
- **Scores are stable from one run to the next:** the 16 earlier documents scored within 0.002 of their first-calibration values.
- **All 20 invoices and credit notes scored between 0.973 and 0.983**, as before. The bank-change letter stays at 0.389 (completeness 0.4). Still nothing between 0.39 and 0.97.

### Do 0.85 and green ≥ 0.95 still hold?

**Yes, both still hold, but for a limited reason.** Every document routes the same way at any threshold between 0.39 and 0.973, and every correctly read document is green. But these degradations were not strong enough to cause a single misreading, so the threshold has still not met the case it exists for: a page read badly enough to produce wrong values.

### Recommendation (no thresholds changed)

- **Keep `CONFIDENCE_THRESHOLD` at 0.85 and green at ≥ 0.95.** Raising either one to the 0.97 band would send about half of the clean invoices to review and catch nothing: every document in that band was read correctly.
- **Reading quality at this level is not a risk.** A 90 dpi CJK copy, a 3.5° skew and a phone photo were all read exactly. The deterministic checks (totals, signs, dates, document type) and the history checks stay the main controls, as the first calibration found.
- **Next test, if wanted: a harsher stress set** (about 60 dpi, 8 to 10° skew, a fold or stamp across the amounts, handwritten corrections). It would show where reading breaks, and whether the page average falls before it does. Only then can a threshold be set from evidence rather than kept by default.
- **Optional and small: tell GST apart from VAT** (Q's tax type). Useful only once a GST refund rule is needed.

### What this rests on, and its limits

- **21 documents, 20 with an expected result**, all generated by us. Still too few to set a threshold statistically.
- **The degradations are simulated** (resampling, rotation, perspective, shadow, compression). They are not real scans or real phone photos, and there are no stamps, folds or handwriting.
- **No misreading occurred**, so the score could not be tested on one.
- **One OCR provider** (`mistral-ocr-latest`). Another provider would need its own calibration.
- The history checks were not part of this run. They were checked live on the email pipeline for N1, O and the duplicate rules.

### How to repeat it

```
docker compose --profile test run --rm --no-deps --user "$(id -u):$(id -g)" -v ./test-data:/gen \
  core-test python /gen/generate.py --calibration      # P to T only; the other files keep their bytes
scripts/calibrate.sh --json /results/calibration.json   # one Mistral OCR call per document
```
