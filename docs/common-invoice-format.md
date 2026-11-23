# Common invoice format

Every intake channel produces the same JSON (`CommonInvoice` in [`services/core/app/models.py`](../services/core/app/models.py)), whatever the country, language or file type.

## Two-layer extraction

1. **The provider copies values exactly as printed** (Mistral OCR with a strict JSON-schema annotation): `"1.234,56 €"`, `"03/04/2026"`, `"US$"`. It is told to ignore any instruction written in the document.
2. **Deterministic Python code interprets them** ([`normalize.py`](../services/core/app/normalize.py)). Nothing is guessed. Anything uncertain becomes an `issue`, and the validation step (step 4) decides the routing.

So the model never decides what a number, a date or a currency *means*: that is plain, unit-tested code.

## Fields

| Field | Notes |
|---|---|
| `is_invoice`, `document_type` | `invoice` or `credit_note`; anything else → `is_invoice: false` |
| `supplier` / `buyer` | name, tax ID, `country` (ISO 3166-1 alpha-2: from the address, or from an EU VAT prefix) |
| `invoice_number`, `related_invoice_number` | the related number is the invoice a credit note cancels |
| `invoice_date`, `due_date` | ISO 8601; **null if ambiguous** (see below) |
| `service_period_start`, `service_period_end` | ISO 8601, only when a service or delivery period is printed; **null if ambiguous**. One date convention per document: a service date like `31/10/2026` also settles an ambiguous invoice date |
| `dates` | printed values, `ambiguous`, suggestion (`suggested_order`, `suggested_invoice_date`, `suggested_due_date`, `suggested_service_period_start` / `_end`, `suggestion_basis`) |
| `currency` | ISO 4217 |
| `lines[]`, `taxes[]` | taxes are generic: `vat`, `gst`, `sales_tax`, `withholding`, `other`, `none` |
| `subtotal`, `tax_total`, `total` | **decimal strings** (exact precision); **credit notes are negative** |
| `language`, `ocr_text` | BCP 47 code; the full original-language text |
| `normalized` | keys for the duplicate check: invoice number, supplier name (legal form removed), tax ID |
| `extraction` | provider, model, pages, OCR confidence (page average and minimum) |
| `issues[]` | `{code, field, message}` |

## Normalization rules

**Amounts:**
- Accepted formats include `1.234,56` · `1 234,56` · `1'234.56` · `1,23,456.78` · `(50.00)` · `50.00-`, plus fullwidth and Arabic-Indic digits.
- If one separator is followed by exactly 3 digits, it is read as a thousands separator, except for 3-decimal currencies (KWD, BHD…).
- A wrong reading is caught by the arithmetic checks.

**Dates:**
- Unambiguous: year first (`2026-11-18`), CJK (`2026年11月18日`, Reiwa era), month names in EN/FR/DE/ES/IT/PT/NL, and numeric dates where one part is > 12.
- One document uses one convention: if the due date `30/04/2026` proves day-first, the invoice date `03/04/2026` is resolved as 3 April.
- **Still ambiguous → the date stays null, `ambiguous_date` is raised, and the invoice goes to review.** A suggestion is stored for one-click confirmation:
  - US and its territories: month first.
  - Canada: no suggestion, because it uses both conventions.
  - Every other known country: day first.
  - Unknown country: no suggestion.

**Currency:**
- An explicit ISO code wins over a symbol.
- Unambiguous symbols map directly (`€`, `円`, `R$`, `zł`…).
- Ambiguous symbols (`$`, `¥`, `kr`) are resolved from the supplier country (noted as `currency_from_country`). Without a country they raise `ambiguous_currency`.
- **The currency must appear in the document text** (code or symbol), otherwise `currency_not_in_document` is raised. This was found in a live test: the model returned a currency that wasn't printed.

**Credit notes:** if the printed total is positive, every amount is negated (quantities are kept).

## Issue codes

| Code | Meaning |
|---|---|
| `not_invoice` | Document isn't an invoice or a credit note |
| `extraction_invalid` | Provider output missing, or not matching the schema |
| `ambiguous_date` | Day/month order unknown |
| `inconsistent_dates` | The two dates prove different orders |
| `unparseable_date` / `unparseable_amount` / `unparseable_rate` | Printed value can't be read |
| `missing_currency` / `ambiguous_currency` | No currency found / ambiguous symbol without a known country |
| `currency_from_country` | Currency deduced from a symbol plus the supplier country (information) |
| `currency_not_in_document` | Extracted currency isn't supported by the document text |

## Files accepted

PDF, PNG, JPEG and WebP; TIFF is converted to PDF first. The type is detected from the file's bytes, not its name or declared type. Maximum 20 MB; the first 10 pages are processed.
