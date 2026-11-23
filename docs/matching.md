# Bank matching

Deterministic rules in [`services/core/app/matching.py`](../services/core/app/matching.py) (rule version `m1`), unit-tested in `tests/test_matching.py`. No AI is involved: a bill is marked paid only when the rules point to exactly one bank transaction.

## What is compared

| Side | Taken into account |
|---|---|
| Bills | Posted in QuickBooks, **unpaid**, not excluded, in the **bank account's currency** |
| Bank transactions | The configured account only; **outgoing** (Plaid amount > 0, verified: positive = money out); posted, not pending; not removed; not already used by an accepted match |

Until FX matching (Day 4), bills in another currency are not compared at all: they stay unpaid with the reason `fx_match_pending`.

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
| Bill not in the account's currency | not compared | `fx_match_pending` |

## Amount differences

When the bank amount differs from the bill within the tolerance:

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
