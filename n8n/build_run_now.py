"""Builds the 'AP – Run now' n8n workflow JSON.

Usage:
  python3 n8n/build_run_now.py > out.json                         # no credential IDs (committable)
  python3 n8n/build_run_now.py --credentials ids.json > out.json  # with local credential IDs, for import

What the workflow does: starts the posting or the bank workflow at once instead of waiting for its
schedule (for a demo, or after fixing something in QuickBooks).

  POST /webhook/ap-run-now   body {"target": "posting" | "bank"}
    X-Run-Timestamp: <unix seconds>
    X-Run-Signature: hex HMAC-SHA256(RUN_NOW_TOKEN, "<timestamp>.<target>")    (scripts/run_now.py)

The token itself never travels: n8n stores every webhook request, headers included, even for a
workflow that saves no execution data (it is soft-deleted, then pruned). A signature is useless
once seen: the core accepts it for a few minutes and only once. The core decides whether the request
may run: the signature (compared in constant time), its age, the target and the rate limits, and it
records every request.
"""

from typing import Any

from nodes import AUTO, CORE, Node, Section, core_call, if_node, lay_out, main, switch_node, workflow

ANSWER = "$('Authorize (core)').first().json"
WORKFLOWS = {"posting": ("Start posting", "apPostQbo0000001"), "bank": ("Start bank sync", "apBankSync000001")}


def _start(name: str, workflow_id: str) -> Node:
    return {
        "name": name,
        "type": "n8n-nodes-base.executeWorkflow",
        "typeVersion": 1.2,
        "position": AUTO,
        "executeOnce": True,
        "parameters": {
            "source": "database",
            "workflowId": {"__rl": True, "value": workflow_id, "mode": "id"},
            "options": {"waitForSubWorkflow": False},
        },
    }


def _respond(name: str, body: str, code: str) -> Node:
    return {
        "name": name,
        "type": "n8n-nodes-base.respondToWebhook",
        "typeVersion": 1.1,
        "position": AUTO,
        "parameters": {
            "respondWith": "json",
            "responseBody": "={{ JSON.stringify(" + body + ") }}",
            "options": {"responseCode": "={{ " + code + " }}"},
        },
    }


def _nodes() -> list[Node]:
    return [
        {
            "name": "Run now request",
            "type": "n8n-nodes-base.webhook",
            "typeVersion": 2,
            "position": AUTO,
            "webhookId": "5f0c2b7e-9a41-4d6b-8e3a-2c71d0a9b6f4",
            "parameters": {"httpMethod": "POST", "path": "ap-run-now", "responseMode": "responseNode", "options": {}},
        },
        # The signature goes to the core, which checks it; nothing here decides
        core_call(
            "Authorize (core)",
            AUTO,
            "POST",
            f"{CORE}/run-now/authorize",
            body="JSON.stringify({ timestamp: String($json.headers['x-run-timestamp'] ?? '').slice(0, 20), "
            "signature: String($json.headers['x-run-signature'] ?? '').slice(0, 128), "
            "target: String($json.body?.target ?? '').slice(0, 40) })",
        ),
        if_node("Allowed?", AUTO, "$json.statusCode === 200 && $json.body?.decision === 'run'"),
        switch_node("Which workflow?", AUTO, "$json.body.target", list(WORKFLOWS)),
        *[_start(name, wid) for name, wid in WORKFLOWS.values()],
        _respond("Started (202)", "{ started: " + ANSWER + ".body.target }", "202"),
        _respond(
            "Refused",
            "{ error: "
            + ANSWER
            + ".body?.message ?? 'not available', retry_after: "
            + ANSWER
            + ".body?.retry_after ?? null }",
            ANSWER + ".body?.status ?? 503",
        ),
    ]


CONNECTIONS = {
    "Run now request": [[("Authorize (core)", 0)]],
    "Authorize (core)": [[("Allowed?", 0)]],
    "Allowed?": [[("Which workflow?", 0)], [("Refused", 0)]],
    "Which workflow?": [[("Start posting", 0)], [("Start bank sync", 0)]],
    "Start posting": [[("Started (202)", 0)]],
    "Start bank sync": [[("Started (202)", 0)]],
}

SECTIONS = [
    Section(
        "Run now",
        "Starts the posting or the bank workflow at once, on request (a demo, or after a fix in QuickBooks).\n\n"
        "Requests are **signed** (scripts/run_now.py): the secret token never travels, and a signature is "
        "accepted for a few minutes and only once. The **core service decides** whether the request may run: "
        "the signature (compared in constant time), its age, the target and the rate limits; it records every "
        "request.",
        col=0,
        row=0,
        kind="overview",
        min_cols=5,
    ),
    Section(
        "Request and decision",
        'POST {"target": "posting" | "bank"} with the headers X-Run-Timestamp and X-Run-Signature. '
        "The core answers run or refuse.",
        col=0,
        row=2,
        grid=(("Run now request", "Authorize (core)", "Allowed?"),),
    ),
    Section(
        "Start the workflow",
        "Started without waiting; the request gets 202 at once.",
        col=3,
        row=2,
        grid=(("Which workflow?", "Start posting", "Started (202)"), (None, "Start bank sync", None)),
    ),
    Section(
        "Refused",
        "401 for a wrong, expired or reused signature, 400 for an unknown target, 429 when rate-limited, "
        "503 when not enabled.",
        col=0,
        row=5,
        grid=(("Refused",),),
        kind="failure",
    ),
]


def build(cred_ids: dict[str, str] | None) -> dict[str, Any]:
    wf = workflow("AP – Run now", lay_out(_nodes(), SECTIONS), CONNECTIONS, cred_ids)
    # Nothing kept beyond n8n's pruning: the requests hold no secret, only one-time signatures
    wf["settings"].update(saveDataErrorExecution="none", saveDataSuccessExecution="none", saveManualExecutions=False)
    return wf


if __name__ == "__main__":
    main(build)
