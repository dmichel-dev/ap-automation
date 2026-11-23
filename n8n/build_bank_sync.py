"""Builds the 'AP – Bank sync & match' n8n workflow JSON.

Usage:
  python3 n8n/build_bank_sync.py > out.json                         # no credential IDs (committable)
  python3 n8n/build_bank_sync.py --credentials ids.json > out.json  # with local credential IDs, for import

What the workflow does (every hour, or by hand):
  1. Bank sync: Plaid /transactions/sync from the saved cursor, page by page; the core stores each
     page (idempotent) and saves the cursor only after the last page. If a page fails, the run
     stops and the next run restarts from the saved cursor (as Plaid requires).
  2. Matching: the core matches outgoing bank transactions with open bills (amount tolerance,
     date window, supplier name or invoice number; accepted only when unique on both sides).
  3. Payments: for each accepted match, a QuickBooks bill payment, searched by its number first
     (the core decides: reuse / create / review), then created with a fixed request ID, and
     reported to the core. Failures go to the core, which decides: retry next run, or stop.

The core decides everything; this workflow makes the calls and passes the answers through.
The Plaid access token is in the n8n credential "Plaid – Sandbox" (merged into the request body).
"""

from typing import Any

from nodes import (
    AUTO,
    CORE,
    FULL_RESPONSE,
    NETWORK_RETRY,
    Connections,
    Node,
    Section,
    code_node,
    core_call,
    describe_failure_js,
    if_node,
    lay_out,
    main,
    qbo_call,
    qbo_send,
    set_node,
    stop,
    switch_node,
    workflow,
)

ITEM_KEY = "plaid-sandbox"  # the bank connection's key in the core (scripts/plaid_sandbox.py)
PLAID_API = "https://sandbox.plaid.com"
MATCHING = "$('Run matching (core)').first().json.body"
LOOP = "$('One payment at a time').first().json"  # the payment being worked on (batch size 1)
MATCH_URL = "=" + CORE + "/matches/{{ " + LOOP + ".match_id }}"
PASS_THROUGH = "status_code: $json.statusCode ?? 0, body: $json.body ?? null"


def qbo(name: str, pos: list[int], method: str, path: str, query: dict[str, str] | None = None) -> Node:
    return qbo_call(name, pos, method, "={{ " + MATCHING + ".qbo_base }}" + path, MATCHING + ".minorversion", query)


def _trigger_nodes() -> list[Node]:
    return [
        {
            "name": "Every hour",
            "type": "n8n-nodes-base.scheduleTrigger",
            "typeVersion": 1.2,
            "position": AUTO,
            "parameters": {"rule": {"interval": [{"field": "hours", "hoursInterval": 1}]}},
        },
        {
            "name": "Manual run",
            "type": "n8n-nodes-base.manualTrigger",
            "typeVersion": 1,
            "position": AUTO,
            "parameters": {},
        },
    ]


def _sync_nodes() -> list[Node]:
    return [
        core_call("Sync state (core)", AUTO, "GET", f"{CORE}/bank/sync-state/{ITEM_KEY}"),
        if_node("State OK?", AUTO, "$json.statusCode === 200"),
        set_node("Start cursor", AUTO, {"cursor": "$json.body.cursor ?? ''"}),
        {
            "name": "Sync page (Plaid)",
            "type": "n8n-nodes-base.httpRequest",
            "typeVersion": 4.2,
            "position": AUTO,
            "onError": "continueRegularOutput",
            **NETWORK_RETRY,
            "parameters": {
                "method": "POST",
                "url": f"{PLAID_API}/transactions/sync",
                "authentication": "genericCredentialType",
                "genericAuthType": "httpCustomAuth",
                "sendBody": True,
                "specifyBody": "json",
                # No cursor on the very first sync: Plaid then sends the whole history
                "jsonBody": "={{ JSON.stringify(Object.assign({ count: 500 }, "
                "$json.cursor ? { cursor: $json.cursor } : {})) }}",
                "options": FULL_RESPONSE,
            },
            "credentials": {"httpCustomAuth": "PLAID"},
        },
        if_node("Page OK?", AUTO, "$json.statusCode === 200"),
        core_call(
            "Store page (core)",
            AUTO,
            "POST",
            f"{CORE}/bank/sync-page/{ITEM_KEY}",
            body="JSON.stringify({ added: $json.body.added, modified: $json.body.modified, "
            "removed: $json.body.removed, next_cursor: $json.body.next_cursor, has_more: $json.body.has_more })",
        ),
        if_node("Stored?", AUTO, "$json.statusCode === 200"),
        # The core saves the cursor only after the last page: until then, fetch the next one
        if_node("More pages?", AUTO, "!$json.body.cursor_saved"),
        set_node("Next cursor", AUTO, {"cursor": "$('Sync page (Plaid)').first().json.body.next_cursor"}),
        stop(
            "Bank sync failed",
            AUTO,
            "'bank run stopped (HTTP ' + ($json.statusCode ?? 'no response') + '): ' "
            "+ ($json.body?.error_code ?? $json.body?.detail ?? $json.error?.message ?? '') "
            "+ ' (Plaid error codes are upper case; the next run restarts from the saved cursor)'",
        ),
    ]


def _matching_nodes() -> list[Node]:
    return [
        core_call("Run matching (core)", AUTO, "POST", f"{CORE}/matching/run/{ITEM_KEY}"),
        if_node("Matching OK?", AUTO, "$json.statusCode === 200"),
        set_node(
            "Matching summary",
            AUTO,
            {
                "open_bills": "String($json.body.open_bills)",
                "accepted": "String($json.body.accepted)",
                "ambiguous": "String($json.body.ambiguous)",
                "unmatched": "String($json.body.unmatched)",
                "fx_match_pending": "String($json.body.fx_match_pending)",
                "payments_due": "String($json.body.payments_due.length)",
            },
        ),
        {
            "name": "Split payments",
            "type": "n8n-nodes-base.splitOut",
            "typeVersion": 1,
            "position": AUTO,
            "parameters": {"fieldToSplitOut": "body.payments_due", "options": {}},
        },
        {
            "name": "One payment at a time",
            "type": "n8n-nodes-base.splitInBatches",
            "typeVersion": 3,
            "position": AUTO,
            "parameters": {"batchSize": 1, "options": {}},
        },
    ]


def _payment_nodes() -> list[Node]:
    return [
        qbo(
            "Find existing payment (QBO)",
            AUTO,
            "GET",
            "/query",
            query={
                "query": '={{ "select Id, VendorRef, TotalAmt from BillPayment where DocNumber = \'" + '
                + LOOP
                + '.doc_number_q + "\'" }}'
            },
        ),
        core_call(
            "Payment decision (core)",
            AUTO,
            "POST",
            MATCH_URL + "/lookup",
            body=f"JSON.stringify({{ {PASS_THROUGH} }})",
        ),
        switch_node(
            "Route payment", AUTO, "$json.body?.decision", ["create", "exists", "review", "error"], "core error"
        ),
        qbo_send(
            qbo(
                "Create payment (QBO)",
                AUTO,
                "POST",
                "/billpayment",
                query={"requestid": "={{ " + LOOP + ".request_id }}"},
            ),
            f"JSON.stringify({LOOP}.document)",
        ),
        if_node("Payment created?", AUTO, "$json.statusCode === 200 && !!$json.body?.BillPayment?.Id"),
        set_node("Payment ready", AUTO, {"external_id": "String($json.body.external_id ?? $json.body.BillPayment.Id)"}),
        core_call(
            "Record payment (core)",
            AUTO,
            "POST",
            MATCH_URL + "/payment",
            body="JSON.stringify({ external_payment_id: $json.external_id, execution_id: $execution.id })",
        ),
        if_node("Recorded?", AUTO, "$json.statusCode === 200"),
        code_node("Collect failure facts", AUTO, describe_failure_js("match_id", LOOP + ".match_id")),
        core_call(
            "Report failure (core)",
            AUTO,
            "POST",
            "=" + CORE + "/matches/{{ $json.match_id }}/payment-failed",
            body="JSON.stringify($json.body)",
        ),
    ]


FAIL = [("Collect failure facts", 0)]
SYNC_FAILED = [("Bank sync failed", 0)]
NEXT = [("One payment at a time", 0)]

CONNECTIONS: Connections = {
    "Every hour": [[("Sync state (core)", 0)]],
    "Manual run": [[("Sync state (core)", 0)]],
    "Sync state (core)": [[("State OK?", 0)]],
    "State OK?": [[("Start cursor", 0)], SYNC_FAILED],
    "Start cursor": [[("Sync page (Plaid)", 0)]],
    "Sync page (Plaid)": [[("Page OK?", 0)]],
    "Page OK?": [[("Store page (core)", 0)], SYNC_FAILED],
    "Store page (core)": [[("Stored?", 0)]],
    "Stored?": [[("More pages?", 0)], SYNC_FAILED],
    "More pages?": [[("Next cursor", 0)], [("Run matching (core)", 0)]],
    "Next cursor": [[("Sync page (Plaid)", 0)]],
    "Run matching (core)": [[("Matching OK?", 0)]],
    "Matching OK?": [[("Matching summary", 0), ("Split payments", 0)], SYNC_FAILED],
    "Split payments": [[("One payment at a time", 0)]],
    "One payment at a time": [[], [("Find existing payment (QBO)", 0)]],
    "Find existing payment (QBO)": [[("Payment decision (core)", 0)]],
    "Payment decision (core)": [[("Route payment", 0)]],
    "Route payment": [[("Create payment (QBO)", 0)], [("Payment ready", 0)], FAIL, FAIL, FAIL],
    "Create payment (QBO)": [[("Payment created?", 0)]],
    "Payment created?": [[("Payment ready", 0)], FAIL],
    "Payment ready": [[("Record payment (core)", 0)]],
    "Record payment (core)": [[("Recorded?", 0)]],
    "Recorded?": [NEXT, FAIL],
    "Collect failure facts": [[("Report failure (core)", 0)]],
    "Report failure (core)": [NEXT],
}


SECTIONS = [
    Section(
        "Bank sync and payment matching",
        "Reads outgoing bank payments (Plaid), matches them with open bills, and records each matched payment "
        "in QuickBooks.\n\n"
        "The **core service decides**: which transactions are new, which payment pays which bill (amount "
        "tolerance, date window, supplier name or invoice number; accepted only when unique on both sides), "
        "and what each QuickBooks answer or failure means. A payment that fits several bills is never applied: "
        "it goes to review.",
        col=0,
        row=0,
        kind="overview",
        min_cols=7,
    ),
    Section(
        "Start",
        "Every hour, or by hand. The core returns the saved sync cursor.",
        col=0,
        row=2,
        grid=(("Every hour", "Sync state (core)", "State OK?", "Start cursor"), ("Manual run", None, None, None)),
    ),
    Section(
        "Bank transactions, page by page",
        "Plaid's transaction sync from the saved cursor. The core stores each page; the cursor is saved only "
        "after the last page, so a failed run restarts cleanly.",
        col=4,
        row=2,
        grid=(
            ("Sync page (Plaid)", "Page OK?", "Store page (core)", "Stored?", "More pages?"),
            (None, None, None, None, "Next cursor"),
        ),
    ),
    Section(
        "Sync stopped",
        "A failed step ends the run as failed; nothing is half-saved.",
        col=4,
        row=5,
        grid=(("Bank sync failed",),),
        kind="failure",
    ),
    Section(
        "Matching (core)",
        "The core matches bank payments with open bills and returns the payments to record.",
        col=9,
        row=2,
        grid=(("Run matching (core)", "Matching OK?", "Split payments"), (None, None, "Matching summary")),
    ),
    Section(
        "Bill payments in QuickBooks",
        "One at a time: searched by number first (the core decides: reuse, create or review), created with a "
        "fixed request ID, then recorded in the core.",
        col=12,
        row=2,
        grid=(
            (
                "One payment at a time",
                "Find existing payment (QBO)",
                "Payment decision (core)",
                "Route payment",
                "Create payment (QBO)",
                "Payment created?",
                "Payment ready",
                "Record payment (core)",
                "Recorded?",
            ),
        ),
    ),
    Section(
        "Payment failures",
        "Facts about the failed step go to the core, which decides: retry on a later run, stop, or review.",
        col=17,
        row=4,
        grid=(("Collect failure facts", "Report failure (core)"),),
        kind="failure",
    ),
]


def build(cred_ids: dict[str, str] | None) -> dict[str, Any]:
    nodes = lay_out(_trigger_nodes() + _sync_nodes() + _matching_nodes() + _payment_nodes(), SECTIONS)
    return workflow("AP – Bank sync & match", nodes, CONNECTIONS, cred_ids)


if __name__ == "__main__":
    main(build)
