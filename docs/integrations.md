# Integrations: verified behaviour (Plaid, QuickBooks Online)

Each fact below was checked on **2026-11-22** (and 2026-11-23 for the n8n posting workflow): against the vendor documentation where it was readable, and with live sandbox calls in every case. The design of Day 2 depends on these facts, so if one changes, the matching design or the workflows must change too.

## Plaid (sandbox)

| Fact | Source | Consequence for the design |
|---|---|---|
| `amount` is **positive when money leaves the account**, negative when it comes in | Docs (*"Positive values when money moves out of the account; negative values when money moves in."*); confirmed live: custom +12.34 payment / −56.78 deposit, sandbox deposits and interest are negative | Only `amount > 0` transactions are matching candidates |
| One Item has several accounts (checking, credit card, loans), all USD in the sandbox | Live `/accounts/get` | Matching is restricted to the configured account ID; the account currency is read from `balances.iso_currency_code` |
| Amounts can have more than 2 decimals (e.g. `2.8096795`) | Live, sandbox default transactions | Stored as received (`numeric`); rounded to the currency's minor unit only for comparison |
| Custom transactions have no `merchant_name` | Live `/sandbox/transactions/create` | Name matching uses `name` (and `merchant_name` when present) |
| First `/transactions/sync` right after Item creation returns `NOT_READY` with no data | Docs + live | The setup script waits for `HISTORICAL_UPDATE_COMPLETE` |
| Custom transactions: max 10 per call, date today or up to 730 days back, no future dates; they land in the checking account | Docs + live | Test transactions are dated on or before the run date |
| After `/transactions/refresh`, custom transactions appear in `/transactions/sync` within seconds | Live | The e2e test calls refresh before syncing |
| `/transactions/sync` pagination: if a page fails, restart from the cursor of the first page | Docs | The cursor is saved only after the last page (`has_more = false`) |

## QuickBooks Online (sandbox company, Intuit demo data)

Company: multicurrency **ON**, home currency **USD** (read from `Preferences.CurrencyPrefs`).

| Fact | Evidence | Consequence |
|---|---|---|
| A vendor's currency is set at creation; a bill in another currency is rejected (*"You can only use one foreign currency per transaction"*) | Live | Vendors are keyed by (supplier, currency) |
| `DisplayName` is unique across all vendors, **whatever the currency** (error 6240 *Duplicate Name Exists*) | Live | Non-home-currency vendors get a suffix: `Name (EUR)` |
| `DisplayName` must not contain `:`, tab or newline (error 2010) | Live | Names are sanitised before use |
| Apostrophes in queries are escaped as `\'` | Live | Query builder escapes them |
| Query results can lag a few seconds behind a create | Live (a vendor created 1 s earlier was not found; found later) | Vendor and bill IDs come from our own tables, not from a search right after a create |
| Creating a vendor in a new currency activates that currency automatically | Live (GBP) | No separate `CompanyCurrency` step is needed |
| A foreign-currency **bill without `ExchangeRate` is rejected** (error 2410) | Live | Every foreign bill carries a rate |
| A foreign-currency **vendor credit without `ExchangeRate` is accepted at rate 1** (£152.40 booked as $152.40) | Live | A rate is always sent explicitly; never rely on a default |
| QuickBooks exposes its own daily rates: `GET /exchangerate?sourcecurrencycode=EUR&asofdate=…` | Live (EUR→USD 1.096876, JPY→USD 0.006306 on 2026-11-20) | Day 2 uses the QuickBooks rate of the invoice date; Day 4 adds Frankfurter (ECB) for conversion and FX matching |
| A bill without `CurrencyRef` is rejected for a foreign-currency vendor | Live | `CurrencyRef` is always sent |
| Negative bill lines are allowed | Live (100 − 5 + 19 = 114) | Discounts become negative lines; bill total = invoice total |
| `DocNumber` max **21 characters** | Live (error 2050) | Longer invoice numbers are shortened deterministically; the full number goes in `PrivateNote` |
| **The same `DocNumber` for the same vendor is accepted** (no duplicate protection) | Live | Idempotency is ours: a posting row is reserved before the call, and an existing bill is looked up first |
| Repeating a request with the same `requestid` returns **HTTP 425** with no body (not the original response) | Live | 425 = "already sent": the workflow looks the object up instead of creating it again |
| A bill payment smaller than the bill leaves the difference open (`Balance` 0.40 after paying 99.60 of 100.00) | Live | A within-tolerance difference is recorded as `payment_difference`; the bill is not shown as fully paid |
| A **vendor credit** applied to a bill by a **bill payment with `TotalAmt` 0** (one line linking the Bill, one the VendorCredit) closes the open part: bill `Balance` 0, credit `Balance` 0, no bank movement | Live (2026-11-23: $10.00 bill, $9.50 paid, $0.50 credit on *Payment differences*) | This is how a reviewer's write-off is recorded (route chosen before building it) |
| `POST /upload` with the metadata part as `text/plain` fails **with HTTP 200** and a `Fault` body | Live | Upload results are checked for `Fault`, not only for the status code |
| `POST /upload` works when `file_metadata_0` is sent as an `application/json` part (n8n: a binary field typed `application/json`) | Live (file linked to the bill, size identical) | This is how the workflow attaches the PDF |
| The upload response contains a temporary signed download URL | Live | Only the attachment ID is stored; the raw response is never logged |
| The **Vendor** entity has **no default expense account** field in the API (33 vendors read, 2026-11-23), though the UI shows a default category | Live | The supplier's default account is kept in our database (`supplier_defaults`), set by a reviewer |
| `select * from Account` / `Vendor` with `maxresults 1000` returns the whole list in one page (94 accounts, 33 vendors in the sandbox) | Live | The sync workflow reads one page; the core refuses a full page (there may be more) or an empty account list, and keeps its copy |
| `GET /preferences`: `AccountingInfoPrefs.BookCloseDate` is **absent** when no closing date is set | Live (2026-11-23) | Absent = no closed period; an unreadable response = unknown, nothing is posted |
| The closing date **can't be set through the API** in the demo company: a sparse preferences update is validated as a whole and fails on unrelated inventory settings (Fault 6000-range business validation) | Live | It is set in the QuickBooks UI (*Settings → Account and settings → Advanced → Close the books*). Set to 2026-09-30 there on 2026-11-23: the API then returned `BookCloseDate: "2026-09-30"` (preferences SyncToken 3 → 4) and the posting run reported `closed_through: 2026-09-30` |

Expense accounts are **chosen per supplier by a reviewer** (see [review](review.md)); until Day 3 every bill went to **Supplies** (20). Fixed accounts (sandbox IDs, configurable): bank **Checking** (35), A/P (33), and **Withholding Tax Payable** (`QBO_WITHHOLDING_ACCOUNT_ID`, an *Other Current Liability* created through the API on 2026-11-23, since the demo company had none). Until Day 3, all taxes went to **Taxes & Licenses** (21); now non-recoverable tax is part of the expense lines and withholding goes to the liability account.

## n8n

| Fact | Consequence |
|---|---|
| The native QuickBooks node chooses the API host from the credential's **Environment** field, whose default is *Production* (defaults are not stored) | The credential must be set to **Sandbox** |
| The native node covers Vendor, Bill, Payment (customer), Purchase…; not BillPayment, VendorCredit, Attachable or ExchangeRate | Those use HTTP Request nodes with the same QuickBooks credential |
| The HTTP Request node sends items **concurrently** unless batching is set | QuickBooks calls use batch size 1 |
| QuickBooks calls must go through n8n: Intuit rotates the refresh token on every refresh | No script uses the tokens directly |
| The native QuickBooks node can't set a vendor's `CurrencyRef` (checked in the node's source, n8n 2.48.7) | Vendors are created with an HTTP Request node, otherwise they would all be USD |
| `requestid` is limited to **50 characters** (error 6000) | Request IDs are `ap-bill-<uuid>`, `ap-vc-<uuid>`, `ap-vendor-<n>`, `ap-billpay-<n>` (tested ≤ 50) |
| "Never error" covers HTTP error responses, **not network errors** (a DNS failure stopped the whole run on 2026-11-23) | QuickBooks and core calls use *continue on error* + 3 tries for network errors; an item without status code is treated as retryable |
| `query` can't select `HomeBalance` on Bill (error 4001) | Not queried; `Balance` and `ExchangeRate` are |
| A *Custom Auth* credential's `body` is merged into the JSON body of each request (checked live with `/accounts/get` and `/transactions/sync`) | The Plaid access token and keys live only in the encrypted n8n credential "Plaid – Sandbox"; workflow JSON never contains them |
| `$('Node').first()` inside a loop returns the node's latest run | The sync loop reads the next cursor this way; tested with 4 pages of 100 (cursor saved only after the last page, no duplicate rows) |

## Probe cleanup

The probe created 4 vendors, 4 bills, 1 vendor credit, 1 bill payment and 1 attachment, all prefixed `ZZ AP Probe` / `ZZPROBE`. All transactions and the attachment were deleted; the 4 vendors were made inactive (QuickBooks doesn't allow deleting vendors). EUR and GBP stay active as company currencies.
