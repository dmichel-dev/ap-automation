# Human review

Invoices that validation can't pass alone go to a person. The core decides which actions each review item allows; the review page (Day 3, step 3) only offers those. Code: [`review.py`](../services/core/app/review.py).

## Actions

| Action | When | Effect |
|---|---|---|
| **Approve** | validation review, no fraud flag | The invoice becomes `ready` and is posted to the chosen account. |
| **Verified with supplier** | fraud items only (`possible_bank_detail_change`) | Same as approve. A note saying how it was verified is required (e.g. "called the number on file, not the one in the letter"). Fraud items have **no plain Approve**. |
| **Correct** | validation review, no fraud flag | Fix a header field that was printed or read wrongly (supplier, number, dates, currency, subtotal, tax total, total, document type). The item stays open; the reviewer then approves or rejects. Line items can't be edited yet (roadmap): an invoice with wrong lines is rejected. |
| **Reject** | any validation review | A reason is required. The invoice is not posted and its stored file is deleted. |
| Write off, choose the bank match, retry/exclude a posting | posting and matching reviews | Day 3, step 4 (they need QuickBooks actions). |

A decided item can't be decided again (409).

## What approving still checks

Approving re-runs the deterministic checks a bill needs (`posting_blockers` in `validation.py`) on the invoice **as stored now**, with any corrections:

- the facts a bill needs: supplier, number, a real invoice date (not an ambiguous one), currency, total, document type;
- sign (an invoice is positive, a credit note negative);
- due date not before the invoice date; service period not inverted;
- totals: lines + adjustments + tax = total.

If one fails, the approval is refused with the list of problems (`409 blocked`). Judgement reasons are settled by the decision itself: possible duplicate, new supplier, possible capex, cut-off, low confidence.

**Ambiguous dates:** "accept the suggested dates" applies the core's suggested reading (from the supplier's country) to the invoice date and any other ambiguous date, recorded as a correction. `dates_ambiguous` stays true as a record of what was printed.

## Expense coding

- Every invoice is posted to **one account chosen by a person**. Per-line coding is on the roadmap.
- The account comes from the supplier's **default account** (`supplier_defaults`), which is set when a reviewer approves the supplier's **first** invoice with an account.
- On a later review, the reviewer may pick another account for that invoice only (e.g. a machine coded to a fixed-asset account). The default changes only if they tick **"update the supplier's default"** (`set_default: true`). A first invoice can also be approved without saving a default (`set_default: false`).
- The picker lists the accounts copied from QuickBooks (see [integrations](integrations.md)): active **Expense, Other Expense, Cost of Goods Sold, Fixed Asset and Other Current Asset** accounts. QuickBooks' system account *Unapplied Cash Bill Payment Expense* is hidden.
- The account actually used, and its source (`supplier_default` or `reviewer`), is stored on the invoice.
- **No defaults were back-filled.** Suppliers posted before this rule existed had every bill coded to *Supplies*, which is wrong for some of them (hosting, courier…). Their next invoice goes to review with `expense_account_missing`, and the reviewer's choice becomes their default.

## Audit trail

Each review item keeps:

- the reasons it was opened for, plus any possible duplicate found after a correction;
- each correction, with the reviewer, the time, and the values before and after;
- the decision: action, account and its source, whether the supplier default was created or updated, whether the suggested dates were accepted, the reviewer, the note, and the time.

`processing_log` records each correction and decision (step `review`), with field names and codes but no document content.

## Scope

Invoices, review items and log entries have a `scope`: `live`, `e2e` (the daily end-to-end test, Day 3 step 8) or `demo` (the public demo, Day 6). The review list, the daily summary and the alerts filter on it, so test and demo data never mix with live data.

## Touchless rate (daily summary, step 7)

```
touchless rate = invoices posted with no human touch / invoices received
```

- **No human touch:** no review item at any stage (validation, posting or matching) and no manual action (retry, exclusion).
- **Invoices received:** documents in the period that aren't `rejected` automatically. Non-invoices, unreadable files and exact duplicate files are excluded, because they aren't invoices to process.
- The summary also shows why the others were touched, by reason code (e.g. `new_vendor 2, possible_capex 1`).

New suppliers always need one review (onboarding and expense coding). The rate rises as the supplier base becomes known, which is the expected pattern for AP automation.
