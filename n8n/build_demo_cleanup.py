"""Builds the 'AP – Demo clean-up' n8n workflow JSON.

Usage:
  python3 n8n/build_demo_cleanup.py > out.json                         # no credential IDs (committable)
  python3 n8n/build_demo_cleanup.py --credentials ids.json > out.json  # with local credential IDs, for import

Every 5 minutes:
1. The core deletes the demo sessions that ended, with their invoices and stored files, and lists what
   they left outside the database: Gmail messages and the n8n runs that handled their items (plus
   refused demo emails and their runs, listed at once).
2. n8n deletes each one permanently: the message through the Gmail API (never to the trash), the run
   record through n8n's own API (its stored files go with it). The answer's status code goes back to
   the core, which decides: gone (deleted, or already gone) or tried again at the next run.
3. Files still queued a minute after they were sent (the demo page's webhook call was missed) are
   handed to 'AP – Demo processing', one run each, without waiting.

The deletions use HTTP requests rather than the Gmail and n8n nodes because the core needs the status
code of each answer (404 = already gone). This workflow's successful runs are not saved: they hold IDs
only and would fill the run history (288 a day); failed runs are.
"""

from typing import Any

from nodes import (
    AUTO,
    CORE,
    FULL_RESPONSE,
    NETWORK_RETRY,
    Node,
    Section,
    code_node,
    core_call,
    if_node,
    lay_out,
    main,
    stop,
    switch_node,
    workflow,
)

WORKFLOW_ID = "apDemoCleanup001"
PROCESSING_ID = "apDemoProcess001"
ANSWER = "$('Clean up (core)').first().json.body"
ITEM = "$('Which service?').item.json"
N8N_API = "http://localhost:5678/api/v1"  # this instance, from inside its container

ONE_PER_DELETION_JS = r"""
// One item per thing to delete ({ channel, item_id }); none: the run ends here
return $input.first().json.body.deletions.map(d => ({ json: d }));
"""

ONE_PER_MISSED_FILE_JS = r"""
// One item per file still queued after a minute; none: the run ends here
return $input.first().json.body.stale_uploads.map(id => ({ json: { upload_id: id } }));
"""


def _delete(name: str, url: str, credential_type: str, credential: str) -> Node:
    """DELETE with a predefined credential: full response, never throws (the core reads the status)."""
    return {
        "name": name,
        "type": "n8n-nodes-base.httpRequest",
        "typeVersion": 4.2,
        "position": AUTO,
        "onError": "continueRegularOutput",
        **NETWORK_RETRY,
        "parameters": {
            "method": "DELETE",
            "url": url,
            "authentication": "predefinedCredentialType",
            "nodeCredentialType": credential_type,
            "options": {"batching": {"batch": {"batchSize": 1, "batchInterval": 200}}, **FULL_RESPONSE},
        },
        "credentials": {credential_type: credential},
    }


def _nodes() -> list[Node]:
    return [
        {
            "name": "Every 5 minutes",
            "type": "n8n-nodes-base.scheduleTrigger",
            "typeVersion": 1.2,
            "position": AUTO,
            "parameters": {"rule": {"interval": [{"field": "minutes", "minutesInterval": 5}]}},
        },
        core_call("Clean up (core)", AUTO, "POST", f"{CORE}/demo/cleanup"),
        if_node("Answered?", AUTO, "$json.statusCode === 200"),
        code_node("One item per deletion", AUTO, ONE_PER_DELETION_JS),
        switch_node("Which service?", AUTO, "$json.channel", ["email", "n8n"]),
        _delete(
            "Delete email (Gmail API)",
            "=https://gmail.googleapis.com/gmail/v1/users/me/messages/{{ encodeURIComponent($json.item_id) }}",
            "gmailOAuth2",
            "GMAIL",
        ),
        _delete(
            "Delete run record (n8n API)",
            "=" + N8N_API + "/executions/{{ encodeURIComponent($json.item_id) }}",
            "n8nApi",
            "N8N_API",
        ),
        core_call(
            "Report answer (core)",
            AUTO,
            "POST",
            f"{CORE}/demo/deletions/done",
            body="JSON.stringify({ channel: " + ITEM + ".channel, "
            "results: [{ item_id: " + ITEM + ".item_id, status_code: $json.statusCode ?? 0 }] })",
        ),
        code_node("One item per missed file", AUTO, ONE_PER_MISSED_FILE_JS),
        {
            "name": "Process missed file",
            "type": "n8n-nodes-base.executeWorkflow",
            "typeVersion": 1.2,
            "position": AUTO,
            "parameters": {
                "source": "database",
                "workflowId": {"__rl": True, "value": PROCESSING_ID, "mode": "id"},
                "mode": "each",  # one run per file: each run is recorded with its own session
                "options": {"waitForSubWorkflow": False},  # this run holds no processing result
            },
        },
        stop("Stop: no answer", AUTO, "'demo clean-up: the core answered ' + ($json.statusCode ?? 'nothing')"),
    ]


CONNECTIONS = {
    "Every 5 minutes": [[("Clean up (core)", 0)]],
    "Clean up (core)": [[("Answered?", 0)]],
    "Answered?": [[("One item per deletion", 0), ("One item per missed file", 0)], [("Stop: no answer", 0)]],
    "One item per deletion": [[("Which service?", 0)]],
    "Which service?": [[("Delete email (Gmail API)", 0)], [("Delete run record (n8n API)", 0)]],
    "Delete email (Gmail API)": [[("Report answer (core)", 0)]],
    "Delete run record (n8n API)": [[("Report answer (core)", 0)]],
    "One item per missed file": [[("Process missed file", 0)]],
}

SECTIONS = [
    Section(
        "Demo clean-up",
        "Every 5 minutes, what a **public demo session** leaves behind is deleted once it ends (60 minutes "
        "after it starts).\n\n"
        "The **core** deletes the session's invoices, files and data, and lists what is held elsewhere: its "
        "Gmail messages and the records of the n8n runs that handled its files. n8n deletes them "
        "**permanently** (no trash) and reports each answer; the core decides what is gone and what is tried "
        "again. A refused demo email is listed at once.",
        col=0,
        row=0,
        kind="overview",
        min_cols=7,
    ),
    Section(
        "Ask the core",
        "Ended sessions are deleted in the database; the answer lists what to delete elsewhere and the files "
        "still waiting.",
        col=0,
        row=2,
        grid=(("Every 5 minutes", "Clean up (core)", "Answered?"),),
    ),
    Section(
        "Delete permanently",
        "Gmail API: the message is deleted, not moved to the trash. n8n API: the run record and its stored "
        "files. The **status code** goes to the core (404 = already gone).",
        col=3,
        row=2,
        grid=(
            ("One item per deletion", "Which service?", "Delete email (Gmail API)", "Report answer (core)"),
            (None, None, "Delete run record (n8n API)", None),
        ),
    ),
    Section(
        "Missed files",
        "A file still queued a minute after it was sent (the demo page's call didn't reach n8n) is processed "
        "now, one run each.",
        col=3,
        row=5,
        grid=(("One item per missed file", "Process missed file"),),
    ),
    Section(
        "No answer",
        "The core did not answer: the run fails (logged by the error workflow); the next run tries again.",
        col=0,
        row=5,
        grid=(("Stop: no answer",),),
        kind="failure",
    ),
]


def build(cred_ids: dict[str, str] | None) -> dict[str, Any]:
    wf = workflow("AP – Demo clean-up", lay_out(_nodes(), SECTIONS), CONNECTIONS, cred_ids)
    # Successful runs hold IDs only and would fill the history (288 a day); failed runs are kept
    wf["settings"].update(saveDataSuccessExecution="none", saveManualExecutions=False)
    return wf


if __name__ == "__main__":
    main(build)
