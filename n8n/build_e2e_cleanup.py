"""Builds the 'AP – E2E QuickBooks clean-up' n8n workflow JSON (called by 'AP – Daily e2e test').

Usage:
  python3 n8n/build_e2e_cleanup.py > out.json                         # no credential IDs (committable)
  python3 n8n/build_e2e_cleanup.py --credentials ids.json > out.json  # with local credential IDs, for import

Deletes the QuickBooks objects (bills and their attached PDFs) created by the end-to-end test's postings.
For each one the core names, n8n reads it back from QuickBooks and the core decides: delete it (only if it
is provably the test's own: test invoice, test vendor, E2E- number, the invoice named in its note), skip it
(already gone), or keep it (refused: logged, and the test fails until a person looks). n8n then sends the
delete request the core wrote, and reports QuickBooks' answer.
"""

from typing import Any

from nodes import (
    AUTO,
    CORE,
    Connections,
    Node,
    Section,
    code_node,
    core_call,
    if_node,
    lay_out,
    main,
    qbo_call,
    qbo_send,
    stop,
    workflow,
)

WORKFLOW_ID = "apE2eCleanup0001"
LIST = "$('Objects to delete (core)').first().json.body"
OBJECT = "$('One item per object').item.json"
CHECK = "$('Check object (core)').item.json.body"
# A QuickBooks response, passed through to the core as is
PASS_THROUGH = "status_code: $json.statusCode ?? 0, body: $json.body ?? null, execution_id: $execution.id"

ONE_PER_OBJECT_JS = r"""
// One item per QuickBooks object the core listed (none: nothing to clean up, the run ends here)
return $input.first().json.body.objects.map(o => ({ json: o }));
"""


def qbo(name: str, method: str, url: str, query: dict[str, str] | None = None) -> Node:
    return qbo_call(name, AUTO, method, url, LIST + ".minorversion", query)


def _nodes() -> list[Node]:
    return [
        {
            "name": "Called by the e2e test",
            "type": "n8n-nodes-base.executeWorkflowTrigger",
            "typeVersion": 1.1,
            "position": AUTO,
            "parameters": {"inputSource": "passthrough"},
        },
        core_call(
            "Objects to delete (core)",
            AUTO,
            "GET",
            "=" + CORE + "/e2e/qbo-objects?run_id={{ $json.run_id }}",
        )
        | {"executeOnce": True},
        if_node("Listed?", AUTO, "$json.statusCode === 200"),
        code_node("One item per object", AUTO, ONE_PER_OBJECT_JS),
        qbo(
            "Read object (QBO)",
            "GET",
            "={{ " + LIST + ".qbo_base }}/{{ $json.entity.toLowerCase() }}/{{ $json.external_id }}",
        ),
        core_call(
            "Check object (core)",
            AUTO,
            "POST",
            "=" + CORE + "/e2e/qbo-objects/{{ " + OBJECT + ".id }}/check",
            body="JSON.stringify({ " + PASS_THROUGH + " })",
        ),
        if_node("Delete it?", AUTO, "$json.statusCode === 200 && $json.body.action === 'delete'"),
        qbo_send(
            qbo(
                "Delete object (QBO)",
                "POST",
                "={{ " + LIST + ".qbo_base }}{{ $json.body.path }}",
                {"operation": "delete"},
            ),
            "JSON.stringify(" + CHECK + ".body)",
        ),
        core_call(
            "Record deletion (core)",
            AUTO,
            "POST",
            "=" + CORE + "/e2e/qbo-objects/{{ " + OBJECT + ".id }}/deleted",
            body="JSON.stringify({ " + PASS_THROUGH + " })",
        ),
        stop("Stop: no list", AUTO, "'e2e clean-up: the core answered ' + ($json.statusCode ?? 'nothing')"),
    ]


CONNECTIONS: Connections = {
    "Called by the e2e test": [[("Objects to delete (core)", 0)]],
    "Objects to delete (core)": [[("Listed?", 0)]],
    "Listed?": [[("One item per object", 0)], [("Stop: no list", 0)]],
    "One item per object": [[("Read object (QBO)", 0)]],
    "Read object (QBO)": [[("Check object (core)", 0)]],
    "Check object (core)": [[("Delete it?", 0)]],
    "Delete it?": [[("Delete object (QBO)", 0)]],
    "Delete object (QBO)": [[("Record deletion (core)", 0)]],
}

SECTIONS = [
    Section(
        "E2E QuickBooks clean-up",
        "Deletes the QuickBooks bills and attached PDFs created by the **daily end-to-end test**, so the books "
        "stay clean. Called by *AP – Daily e2e test* before and after each run.\n\n"
        "The **core decides** what may be deleted: only an object it recorded for a test invoice, read back with "
        "the test vendor, an **E2E-** number and the test invoice named in its note. Anything else is kept, "
        "logged, and the test fails until a person looks.",
        col=0,
        row=0,
        kind="overview",
        min_cols=6,
    ),
    Section(
        "List",
        "The core lists the test's objects not deleted yet (attachments before bills).",
        col=0,
        row=2,
        grid=(("Called by the e2e test", "Objects to delete (core)", "Listed?", "One item per object"),),
    ),
    Section(
        "Check, then delete",
        "Each object is read back from QuickBooks; the core checks it and writes the delete request. "
        "QuickBooks' answer goes back to the core.",
        col=4,
        row=2,
        grid=(
            ("Read object (QBO)", "Check object (core)", "Delete it?"),
            (None, "Delete object (QBO)", "Record deletion (core)"),
        ),
    ),
    Section(
        "No list",
        "The core did not answer: the run fails (logged by the error workflow); the test's reset then refuses "
        "to delete anything.",
        col=2,
        row=4,
        grid=(("Stop: no list",),),
        kind="failure",
    ),
]


def build(cred_ids: dict[str, str] | None) -> dict[str, Any]:
    return workflow("AP – E2E QuickBooks clean-up", lay_out(_nodes(), SECTIONS), CONNECTIONS, cred_ids)


if __name__ == "__main__":
    main(build)
