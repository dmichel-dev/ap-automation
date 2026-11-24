# Bank matching

Deterministic rules in [`services/core/app/matching.py`](../services/core/app/matching.py) (rule version `m2`), unit-tested in `tests/test_matching.py`; exchange rates and the payment rate in [`fx.py`](../services/core/app/fx.py). No AI is involved: a bill is marked paid only when the rules point to exactly one bank transaction.

## What is compared

| Side | Taken into account |
|---|---|
| Bills | Posted in QuickBooks, **unpaid**, not excluded, in **any currency** (see [Bills in another currency](#bills-in-another-currency)) |
| Bank transactions | The configured account only; **outgoing** (Plaid amount > 0, verified: positive = money out); posted, not pending; not removed; not already used by an accepted match |


## Rules (all three must pass)

| Rule | Test | Default |
|---|---|---|
| Amount | \\|bank − bill\\| ≤ the smaller of an absolute and a percentage tolerance. The bank amount is first rounded to the currency's minor unit (Plaid can send more decimals). | $1.00 / 0.5 % |
| Date | invoice date − 3 days ≤ transaction date ≤ (due date, or invoice date + 30 days) + 30 days | 3 / 30 / 30 |
| Name | share of the supplier's name words found in the bank text ≥ 0.6 (bank abbreviations like `SUP` for `SUPPLY` count; words such as `ACH`, `PAYMENT` and legal forms are ignored), **or** the invoice number appears in the bank text (at least 4 characters) | 0.6 |

Settings: `MATCH_*` in `.env` (see `.env.example`).

## Decision

| Situation | Result | Payment reason |
|---|---|---|
| The bill has exactly one candidate, and that transaction fits no other bill | `accepted` → a bill payment is recorded in QuickBooks | `payment_pending`, then paid |
| Several candidates on either side | nothing is paid; all candidates are stored, and a review item (stage `matching`) names the candidate transactions | `ambiguous_match` |
| No candidate | re-checked on the next run | `no_match_yet` |
| Bill in another currency, a transaction fits date and name but not the amount | review (stage `matching`) with expected vs actual | `fx_amount_difference` |
| Bill in another currency, no usable rate | review if a transaction fits date and name; otherwise waits | `fx_rate_unavailable` |

## Bills in another currency

The bank account is in USD; a bill can be in EUR, JPY, GBP… It is compared with the bank amount through the **ECB reference rate of the transaction date**.

**Rates.** Before matching, the bank workflow asks the core which rate series the open bills need (one per bill currency, from a few days before the earliest invoice date to today), fetches them from the [Frankfurter API](https://frankfurter.dev) with the ECB as the only provider (`/v2/providers/ecb/rates?base=EUR&quotes=USD&from=…&to=…`), and passes the answers to the core, which stores them in `fx_rates`. The ECB publishes on business days only, so a transaction takes the **latest rate on or before its date**.

| Frankfurter answers | The core |
|---|---|
| 200 with the daily rates | stores them (and forgets any earlier "not quoted") |
| 422 "ECB doesn't quote AED" | remembers the currency as not quoted (`fx_unsupported`); its bills get `fx_rate_unavailable` |
| Anything else, or no answer | stores nothing and logs a warning; the next run fetches again (an outage never sends a bill to review) |

Covered by the ECB: EUR, JPY, GBP, INR, CHF and about 25 more. **Not** covered: AED, SAR and other currencies the ECB doesn't publish.

**Rules.** Date and name are the same as above. The amount rule becomes:

| Rule | Test | Default |
|---|---|---|
| Rate | the rate is at most 4 days older than the transaction (weekends and holidays) | `MATCH_FX_MAX_RATE_AGE_DAYS=4` |
| Amount | \|bank − bill × rate\| ≤ 3 % of bill × rate (no absolute cap: rates move between the invoice and the payment) | `MATCH_FX_PCT_TOLERANCE=0.03` |

Uniqueness applies across all bills: one USD payment that fits a USD bill and a EUR bill is ambiguous.

A foreign bill with no candidate, but a transaction that fits its **date and name**, goes to a person instead of waiting silently: `fx_amount_difference` when the amount is outside the tolerance, `fx_rate_unavailable` when there is no usable rate. The review item shows expected vs actual, e.g.:

> 2026-11-24 bank 1900.00 USD, expected 1748.72 USD (1598.61 EUR × 1.0939, ECB 2026-11-23), +8.65 %

The reviewer confirms the transaction or rejects it, as for an ambiguous match. A transaction that another bill can take by rule is never proposed for review.

**Payment.** The bill payment is in the **bill's currency**, settles the **whole bill**, and carries the actual rate:

- `TotalAmt` = the bill amount, `CurrencyRef` = the bill currency, paid from the USD bank account;
- `ExchangeRate` = bank amount ÷ bill amount, rounded to **7 decimals** (what QuickBooks keeps, verified live: 1.1009564559 was stored as 1.1009565);
- **QuickBooks posts the exchange gain or loss** to *Exchange Gain or Loss*. The ECB rate is only used to decide the match; the gain or loss comes from the bill's rate and the payment's rate;
- an FX difference is **never a payment difference** and never written off: the posting is `paid`.

With 7 decimals, bill × rate lands on the exact bank amount for any bill under 100,000 currency units. Above that, see the known limit below.

Verified live on 2026-11-24 (bill AV-2026-0147, 1,598.61 EUR, posted at 1.099904; Plaid sandbox payment of 1,760.00 USD on 2026-11-24, from `test-data/bank/fx-transactions.json`): matched at the ECB rate of 2026-11-24 (1.0963: 1,752.56 USD expected, +0.42 %); bill payment `AP-PAY-29` of 1,598.61 EUR at 1.1009565. In QuickBooks: Cr Checking **1,760.00** (the bank amount to the cent), Dr A/P-EUR 1,758.32, Dr *Exchange Gain or Loss* 1.68; bill balance 0. A second run created nothing.

### Known limit: cents on large foreign-currency bills

QuickBooks keeps **7 decimals** of an exchange rate. On a bill over **100,000 currency units** (common in JPY, INR, KRW…), one rate step is worth more than a cent, so the bank account can be credited a few cents more or less than the bank amount (at most half a step: e.g. 2,345,678 JPY paid with 14,801.00 USD is booked as 14,800.99 USD).

- The payment is recorded as usual and the bill is paid; nothing is adjusted silently.
- The gap is logged (`bank amount not booked to the cent`) and listed in the **daily summary** under *Bank amounts not booked to the cent*: the bill, the bank amount, the amount booked and the gap.
- **Fix it by hand** with a journal entry between the bank account and *Exchange Gain or Loss* for the gap, so the bank account matches the statement.
- An automatic adjustment is on the roadmap.

A 0.01 line on *Exchange Gain or Loss* when a foreign-currency **bill** is posted is QuickBooks' own rounding (it converts each line and the total separately), not an error.

## Amount differences

When the bank amount differs from a bill **in the same currency** within the tolerance:

- the **actual bank amount** is kept on the match (`bank_amount`, `difference`);
- the payment recorded in QuickBooks never exceeds the bill: an under-payment leaves the difference open on the bill, an over-payment is applied up to the bill amount;
- the posting gets `payment_status = paid_with_difference` and `payment_reason = payment_difference`, never plain `paid`, and the difference is logged as a warning;
- an **under-payment** opens a `payment_difference` review item (e.g. "bill 1212.50, bank 1212.00, open 0.50 USD"). The reviewer writes the open part off or leaves it open; nothing is written off automatically;
- an **over-payment** is not recorded in QuickBooks beyond the bill amount. It is logged as its own warning (`over-payment not recorded in QuickBooks`, with the amount and the match ID), so it stays visible.

## Writing off a payment difference

When the reviewer writes the open part off, the bank workflow records it in two steps, each searched by the number `AP-WO-<write-off id>` before it is created:

1. a **vendor credit** for the open part, on the **Payment differences** account (`QBO_WRITE_OFF_ACCOUNT_ID`; an *Other Expense* account that takes both debits and credits, created by the chart-of-accounts script);
2. a **bill payment of 0** that links the bill and the vendor credit. It closes the bill, and no money moves.

The posting then gets `payment_status = paid_with_write_off`, with `amount_paid + amount_written_off = amount` (enforced by the database), and the review item closes as `written_off`.

## Ambiguous matches

One bank transaction that fits several bills (or one bill several transactions) is never applied: each bill gets a review item with the candidate transactions. Confirming one accepts that match (the payment is recorded by the next run, as for an automatic match) and dismisses the other candidates on both sides. Rejecting them dismisses them for good: they are never proposed again for that bill (`matching.decide` skips dismissed pairs).

## Recording the payment in QuickBooks

Each payment carries the number `AP-PAY-<match id>` and a fixed request ID. Before creating it, the workflow searches QuickBooks for that number and the core decides: reuse it (a run that stopped after creating it), create it, or open a review. A failure is reported to the core, which decides whether it is retried on the next run or stopped.

Verified live on 2026-11-23 (test set I, J, K1, K2): exact match paid in full; $1,212.00 paid for $1,212.50 with $0.50 left open; two $86.80 bills and one $86.80 payment left unpaid with a review item each; a second run created nothing; after a simulated crash the existing payment was found again, not duplicated.

Review decisions, verified live the same day:

- **J (write-off):** the $0.50 was written off from the review. The bank run created vendor credit `AP-WO-1` ($0.50, *Payment differences*) and a $0 bill payment `AP-WO-1` linking it to the bill. In QuickBooks, the bill balance and the credit balance are both 0.
- **K1/K2 (ambiguous match):** the reviewer confirmed the transaction for K1. K2's item closed on its own, and its bill stays unpaid. The next run recorded K1's payment: bill balance 0.
- **Rerun:** a second run created nothing new (one document of each number in QuickBooks).

## Guarantees enforced by the database

- A bank transaction pays **at most one invoice**, and an invoice is paid by **at most one transaction** (partial unique indexes on accepted matches).
- Matching runs are serialized per bank account (advisory lock), and payments to record are leased, so overlapping runs never record the same payment twice.
