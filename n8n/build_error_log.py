"""Builds the 'AP – Error log' n8n workflow JSON.

Usage:
  python3 n8n/build_error_log.py > out.json                         # no credential IDs (committable)
  python3 n8n/build_error_log.py --credentials ids.json > out.json  # with local credential IDs, for import

Every other AP workflow names this one as its error workflow (nodes.ERROR_WORKFLOW_ID): when one of their
runs fails (a node error n8n could not continue from), n8n starts this workflow, which writes the failure
to the AP log (step workflow_error) for the daily summary. The error message is shortened: it is for
finding the run in n8n, not for storing document data.
"""

from typing import Any

from nodes import AUTO, Node, Section, lay_out, main, postgres_query, workflow

LOG_SQL = (
    "INSERT INTO ap.processing_log (n8n_execution_id, step, level, message, details)\n"
    "VALUES ($1, 'workflow_error', 'error', $2, $3::jsonb)"
)
FAILED = "$json.execution"


def _nodes() -> list[Node]:
    return [
        {
            "name": "Workflow failed",
            "type": "n8n-nodes-base.errorTrigger",
            "typeVersion": 1,
            "position": AUTO,
            "parameters": {},
        },
        postgres_query(
            "Log workflow error",
            AUTO,
            LOG_SQL,
            [
                f"String({FAILED}?.id ?? '')",
                "'workflow failed: ' + ($json.workflow?.name ?? 'unknown')",
                "JSON.stringify({ workflow_id: $json.workflow?.id ?? null, workflow: $json.workflow?.name ?? null, "
                f"node: {FAILED}?.lastNodeExecuted ?? null, mode: {FAILED}?.mode ?? null, "
                f"error: String({FAILED}?.error?.message ?? '').slice(0, 300) }})",
            ],
        ),
    ]


SECTIONS = [
    Section(
        "Error log",
        "Every AP workflow names this one as its **error workflow**. A failed run (e.g. Gmail or the database "
        "unreachable, a missing label) is written to the AP log, so the daily summary shows it. Open the run in "
        "n8n's executions list to see the details.",
        col=0,
        row=0,
        kind="overview",
        min_cols=4,
    ),
    Section(
        "Log the failed run",
        "Workflow, last node, short error message. Never document data.",
        col=0,
        row=2,
        grid=(("Workflow failed", "Log workflow error"),),
        kind="failure",
    ),
]


def build(cred_ids: dict[str, str] | None) -> dict[str, Any]:
    wf = workflow(
        "AP – Error log", lay_out(_nodes(), SECTIONS), {"Workflow failed": [[("Log workflow error", 0)]]}, cred_ids
    )
    del wf["settings"]["errorWorkflow"]  # not its own error workflow
    return wf


if __name__ == "__main__":
    main(build)
