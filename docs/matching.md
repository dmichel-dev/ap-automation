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
- the open difference is written off in the Day 3 review (decided 2026-11-23), not automatically.

## Recording the payment in QuickBooks

Each payment carries the number `AP-PAY-<match id>` and a fixed request ID. Before creating it, the workflow searches QuickBooks for that number and the core decides: reuse it (a run that stopped after creating it), create it, or open a review. A failure is reported to the core, which decides whether it is retried on the next run or stopped.

Verified live on 2026-11-23 (test set I, J, K1, K2): exact match paid in full; $1,212.00 paid for $1,212.50 with $0.50 left open; two $86.80 bills and one $86.80 payment left unpaid with a review item each; a second run created nothing; after a simulated crash the existing payment was found again, not duplicated.

## Guarantees enforced by the database

- A bank transaction pays **at most one invoice**, and an invoice is paid by **at most one transaction** (partial unique indexes on accepted matches).
- Matching runs are serialized per bank account (advisory lock), and payments to record are leased, so overlapping runs never record the same payment twice.
