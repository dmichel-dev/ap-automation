"""Builds the 'AP – Demo processing' n8n workflow JSON.

Usage:
  python3 n8n/build_demo_processing.py > out.json                         # no credential IDs (committable)
  python3 n8n/build_demo_processing.py --credentials ids.json > out.json  # with local credential IDs, for import

What the workflow does: processes one file a visitor queued on the public demo page (a sample or an
upload). The demo page stores the file in the database, then calls this workflow's webhook with the
file's ID only:

  POST http://n8n:5678/webhook/ap-demo-upload   body {"upload_id": 123}   header X-Demo-Key

The webhook is reached on the Docker network only (no proxy route leads to n8n). Its key only lets a
caller ask for a queued file to be processed now rather than at the next sweep; the core refuses any
file that isn't waiting (409). The demo clean-up also calls this workflow, as a sub-workflow, for
files whose webhook call was missed.

The core does the work (same processing as every channel, scope demo, the session's limits and
budget) and records this run with the session, so its record is deleted when the session ends.
"""

from typing import Any

from nodes import AUTO, CORE, Node, Section, core_call, lay_out, main, route_by_status, set_node, stop, workflow

WORKFLOW_ID = "apDemoProcess001"
OUTCOMES = {
    "ready": "Ready: bill preview kept",
    "needs_review": "Needs review: on the page",
    "rejected": "Rejected: on the page",
    "refused": "Refused: not a supported file",
}


def _outcome(name: str) -> Node:
    return {"name": name, "type": "n8n-nodes-base.noOp", "typeVersion": 1, "position": AUTO, "parameters": {}}


def _nodes() -> list[Node]:
    return [
        {
            "name": "Queued file (webhook)",
            "type": "n8n-nodes-base.webhook",
            "typeVersion": 2,
            "position": AUTO,
            "webhookId": "49a9b6d7-4935-4e23-860f-e18e627ddc7e",
            "parameters": {
                "httpMethod": "POST",
                "path": "ap-demo-upload",
                "authentication": "headerAuth",
                "responseMode": "onReceived",
                "responseCode": 202,
                "options": {"noResponseBody": True},
            },
            "credentials": {"httpHeaderAuth": "DEMO_HOOK"},
        },
        {
            "name": "Missed file (clean-up)",
            "type": "n8n-nodes-base.executeWorkflowTrigger",
            "typeVersion": 1.1,
            "position": AUTO,
            "parameters": {"inputSource": "passthrough"},
        },
        set_node("Upload ID", AUTO, {"upload_id": "String($json.body?.upload_id ?? $json.upload_id ?? '')"}),
        core_call(
            "Process upload (core)",
            AUTO,
            "POST",
            "=" + CORE + "/demo/uploads/{{ $json.upload_id }}/process",
            body="JSON.stringify({ execution_id: $execution.id })",
            options={"timeout": 300000},
        ),
        route_by_status(refused=True),
        *[_outcome(name) for name in OUTCOMES.values()],
        stop(
            "Stop: not processed",
            AUTO,
            "'demo upload ' + $('Upload ID').item.json.upload_id + ': the core answered ' + "
            "($json.statusCode ?? 'nothing') + ' ' + JSON.stringify($json.body?.detail ?? $json.error?.message ?? '')",
        ),
    ]


CONNECTIONS = {
    "Queued file (webhook)": [[("Upload ID", 0)]],
    "Missed file (clean-up)": [[("Upload ID", 0)]],
    "Upload ID": [[("Process upload (core)", 0)]],
    "Process upload (core)": [[("Route by status", 0)]],
    "Route by status": [*[[(name, 0)] for name in OUTCOMES.values()], [("Stop: not processed", 0)]],
}

SECTIONS = [
    Section(
        "Demo processing",
        "Processes one file a visitor queued on the **public demo page** (a sample or an upload).\n\n"
        "The page stores the file in the database and calls the webhook with its **ID only**: the webhook is "
        "reachable on the internal Docker network only. The **core service** does all the work, exactly as for "
        "the other channels, inside the visitor's session: limits, OCR budget, checks, duplicates within the "
        "session. Nothing is posted to QuickBooks: a ready invoice gets a **bill preview**.",
        col=0,
        row=0,
        kind="overview",
        min_cols=6,
    ),
    Section(
        "Receive",
        "From the demo page (header **X-Demo-Key**, answered 202 at once) or from the clean-up, for a file "
        "still queued after a minute.",
        col=0,
        row=2,
        grid=(("Queued file (webhook)", "Upload ID"), ("Missed file (clean-up)", None)),
    ),
    Section(
        "Process (core)",
        "Extract, normalize, check, store, in the visitor's session. The core records this run with the session: "
        "its record is deleted when the session ends.",
        col=2,
        row=2,
        grid=(("Process upload (core)", "Route by status"),),
    ),
    Section(
        "Outcome",
        "The demo page shows the result; nothing else to do here.",
        col=4,
        row=2,
        grid=tuple((name,) for name in OUTCOMES.values()),
    ),
    Section(
        "Not processed",
        "No answer, a server error, or a file that isn't waiting (409): the run fails and the error workflow logs "
        "it. The demo page shows the file as failed when the core recorded why.",
        col=0,
        row=5,
        grid=(("Stop: not processed",),),
        kind="failure",
    ),
]


def build(cred_ids: dict[str, str] | None) -> dict[str, Any]:
    return workflow("AP – Demo processing", lay_out(_nodes(), SECTIONS), CONNECTIONS, cred_ids)


if __name__ == "__main__":
    main(build)
