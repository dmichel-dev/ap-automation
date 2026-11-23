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
    CORE,
    FULL_RESPONSE,
    NETWORK_RETRY,
    Connections,
    Node,
    code_node,
    core_call,
    describe_failure_js,
    if_node,
    main,
    qbo_call,
    qbo_send,
    set_node,
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


def stop(name: str, pos: list[int], message: str) -> Node:
    """Ends the run as failed (visible in the executions list; Day 3 adds the alert)."""
    return {
        "name": name,
        "type": "n8n-nodes-base.stopAndError",
        "typeVersion": 1,
        "position": pos,
        "parameters": {"errorMessage": "={{ " + message + " }}"},
    }


def _trigger_nodes() -> list[Node]:
    return [
        {
            "name": "Every hour",
            "type": "n8n-nodes-base.scheduleTrigger",
            "typeVersion": 1.2,
            "position": [0, 200],
            "parameters": {"rule": {"interval": [{"field": "hours", "hoursInterval": 1}]}},
        },
        {
            "name": "Manual run",
            "type": "n8n-nodes-base.manualTrigger",
            "typeVersion": 1,
            "position": [0, 400],
            "parameters": {},
        },
    ]


def _sync_nodes() -> list[Node]:
    return [
        core_call("Sync state (core)", [240, 300], "GET", f"{CORE}/bank/sync-state/{ITEM_KEY}"),
        if_node("State OK?", [460, 300], "$json.statusCode === 200"),
        set_node("Start cursor", [680, 220], {"cursor": "$json.body.cursor ?? ''"}),
        {
            "name": "Sync page (Plaid)",
            "type": "n8n-nodes-base.httpRequest",
            "typeVersion": 4.2,
            "position": [900, 300],
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
        if_node("Page OK?", [1120, 300], "$json.statusCode === 200"),
        core_call(
            "Store page (core)",
            [1340, 220],
            "POST",
            f"{CORE}/bank/sync-page/{ITEM_KEY}",
            body="JSON.stringify({ added: $json.body.added, modified: $json.body.modified, "
            "removed: $json.body.removed, next_cursor: $json.body.next_cursor, has_more: $json.body.has_more })",
        ),
        if_node("Stored?", [1560, 220], "$json.statusCode === 200"),
        # The core saves the cursor only after the last page: until then, fetch the next one
        if_node("More pages?", [1780, 140], "!$json.body.cursor_saved"),
        set_node("Next cursor", [2000, 40], {"cursor": "$('Sync page (Plaid)').first().json.body.next_cursor"}),
        stop(
            "Bank sync failed",
            [1340, 480],
            "'bank run stopped (HTTP ' + ($json.statusCode ?? 'no response') + '): ' "
            "+ ($json.body?.error_code ?? $json.body?.detail ?? $json.error?.message ?? '') "
            "+ ' (Plaid error codes are upper case; the next run restarts from the saved cursor)'",
        ),
    ]


def _matching_nodes() -> list[Node]:
    return [
        core_call("Run matching (core)", [2000, 260], "POST", f"{CORE}/matching/run/{ITEM_KEY}"),
        if_node("Matching OK?", [2220, 260], "$json.statusCode === 200"),
        set_node(
            "Matching summary",
            [2440, 120],
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
            "position": [2440, 300],
            "parameters": {"fieldToSplitOut": "body.payments_due", "options": {}},
        },
        {
            "name": "One payment at a time",
            "type": "n8n-nodes-base.splitInBatches",
            "typeVersion": 3,
            "position": [2660, 300],
            "parameters": {"batchSize": 1, "options": {}},
        },
    ]


def _payment_nodes() -> list[Node]:
    return [
        qbo(
            "Find existing payment (QBO)",
            [2880, 320],
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
            [3100, 320],
            "POST",
            MATCH_URL + "/lookup",
            body=f"JSON.stringify({{ {PASS_THROUGH} }})",
        ),
        switch_node(
            "Route payment", [3320, 320], "$json.body?.decision", ["create", "exists", "review", "error"], "core error"
        ),
        qbo_send(
            qbo(
                "Create payment (QBO)",
                [3540, 200],
                "POST",
                "/billpayment",
                query={"requestid": "={{ " + LOOP + ".request_id }}"},
            ),
            f"JSON.stringify({LOOP}.document)",
        ),
        if_node("Payment created?", [3760, 200], "$json.statusCode === 200 && !!$json.body?.BillPayment?.Id"),
        set_node(
            "Payment ready", [3980, 300], {"external_id": "String($json.body.external_id ?? $json.body.BillPayment.Id)"}
        ),
        core_call(
            "Record payment (core)",
            [4200, 300],
            "POST",
            MATCH_URL + "/payment",
            body="JSON.stringify({ external_payment_id: $json.external_id, execution_id: $execution.id })",
        ),
        if_node("Recorded?", [4420, 300], "$json.statusCode === 200"),
        code_node("Describe failure", [4200, 560], describe_failure_js("match_id", LOOP + ".match_id")),
        core_call(
            "Report failure (core)",
            [4420, 560],
            "POST",
            "=" + CORE + "/matches/{{ $json.match_id }}/payment-failed",
            body="JSON.stringify($json.body)",
        ),
    ]


FAIL = [("Describe failure", 0)]
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
    "Describe failure": [[("Report failure (core)", 0)]],
    "Report failure (core)": [NEXT],
}


def build(cred_ids: dict[str, str] | None) -> dict[str, Any]:
    nodes = _trigger_nodes() + _sync_nodes() + _matching_nodes() + _payment_nodes()
    return workflow("AP – Bank sync & match", nodes, CONNECTIONS, cred_ids)


if __name__ == "__main__":
    main(build)
