"""Builds the 'AP – Daily e2e test' n8n workflow JSON.

Usage:
  python3 n8n/build_e2e_test.py > out.json                         # no credential IDs (committable)
  python3 n8n/build_e2e_test.py --credentials ids.json > out.json  # with local credential IDs, for import

Every day at 07:30 (Paris time), before the 08:00 summary, or by hand:
  1. The core starts a run. The QuickBooks objects of earlier test postings are deleted (sub-workflow
     'AP – E2E QuickBooks clean-up'), then the core deletes the earlier test invoices (scope e2e only)
     and hands out the run's four messages.
  2. They are put in the mailbox with the Gmail API (messages.insert), addressed to invoices+e2e@: mail
     sent from the mailbox to its own alias may never reach the inbox the intake watches. The intake
     workflow processes them like any email; the core marks them as test data (scope e2e).
  3. After 10 minutes: the Gmail labels are read, the test emails go to the trash, QuickBooks is cleaned
     up, and the core checks everything and decides pass or fail.
  4. A failed run (or one that could not finish) sends the core's alert email at once. The daily summary
     shows the result either way.
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
    gmail,
    if_node,
    json_body,
    lay_out,
    main,
    set_node,
    stop,
    workflow,
)

WORKFLOW_ID = "apDailyE2eTest01"
CLEANUP_ID = "apE2eCleanup0001"
RUN = "$('Run ID').first().json.run_id"
RUN_URL = "=" + CORE + "/e2e/runs/{{ " + RUN + " }}"
GMAIL_API = "https://gmail.googleapis.com/gmail/v1/users/me/messages"

ONE_PER_MESSAGE_JS = r"""
// One item per message the core wrote (scenario + RFC 822 message, base64url)
return $input.first().json.body.messages.map(m => ({ json: m }));
"""

GMAIL_IDS_JS = r"""
// The Gmail ID of each inserted message, by scenario (the core records them)
const written = $('One item per message').all();
return [{ json: {
  inserted: $input.all().filter(i => i.json.statusCode === 200).length,
  expected: written.length,
  messages: $input.all().map((i, n) => ({ scenario: written[n].json.scenario, gmail_id: i.json.body?.id ?? '' }))
                        .filter(m => m.gmail_id),
} }];
"""

SENT_JS = r"""
// One item per inserted message, to read its labels
return $('Gmail IDs').first().json.messages.map(m => ({ json: m }));
"""

LABELS_JS = r"""
// What Gmail shows for each test email: its labels by name, for the core's checks
const names = Object.fromEntries($('Get labels').all().map(l => [l.json.id, l.json.name]));
const sent = $('One item per sent message').all();
return [{ json: {
  run_id: $('Run ID').first().json.run_id,
  messages: $('Read labels (Gmail)').all().map((r, n) => ({
    scenario: sent[n].json.scenario, gmail_id: sent[n].json.gmail_id,
    labels: (r.json.body?.labelIds ?? []).map(id => names[id] ?? id),
    trashed: $('Move to trash (Gmail)').all()[n].json.statusCode === 200,
  })),
} }];
"""

FAILED_STEP_JS = r"""
// Which step stopped the run, and what it saw (the core records it and writes the alert)
const r = $input.first().json;
return [{ json: { step: $prevNode.name,
  message: String(r.body?.message ?? r.body?.detail ?? r.error?.message ?? `HTTP ${r.statusCode ?? 0}`)
    .slice(0, 400) } }];
"""


def gmail_api(name: str, method: str, url: str, body: str | None = None) -> Node:
    """A Gmail API call on the AP mailbox's credential. Full response, never throws."""
    params: dict[str, Any] = {
        "method": method,
        "url": url,
        "authentication": "predefinedCredentialType",
        "nodeCredentialType": "gmailOAuth2",
        "options": FULL_RESPONSE,
    }
    if body is not None:
        params.update(json_body(body))
    return {
        "name": name,
        "type": "n8n-nodes-base.httpRequest",
        "typeVersion": 4.2,
        "position": AUTO,
        "onError": "continueRegularOutput",
        **NETWORK_RETRY,
        "parameters": params,
        "credentials": {"gmailOAuth2": "GMAIL"},
    }


def cleanup(name: str) -> Node:
    """The QuickBooks clean-up sub-workflow, given the run ID; it may end with no item (nothing to clean)."""
    return {
        "name": name,
        "type": "n8n-nodes-base.executeWorkflow",
        "typeVersion": 1.2,
        "position": AUTO,
        "executeOnce": True,
        "alwaysOutputData": True,
        "parameters": {
            "source": "database",
            "workflowId": {"__rl": True, "value": CLEANUP_ID, "mode": "id"},
            "options": {"waitForSubWorkflow": True},
        },
    }


def _start_nodes() -> list[Node]:
    return [
        {
            "name": "Every day at 07:30",
            "type": "n8n-nodes-base.scheduleTrigger",
            "typeVersion": 1.2,
            "position": AUTO,
            "parameters": {
                "rule": {"interval": [{"field": "days", "daysInterval": 1, "triggerAtHour": 7, "triggerAtMinute": 30}]}
            },
        },
        {
            "name": "Manual run",
            "type": "n8n-nodes-base.manualTrigger",
            "typeVersion": 1,
            "position": AUTO,
            "parameters": {},
        },
        core_call(
            "Start run (core)", AUTO, "POST", f"{CORE}/e2e/runs", body="JSON.stringify({ execution_id: $execution.id })"
        ),
        if_node("Started?", AUTO, "$json.statusCode === 200"),
        set_node("Run ID", AUTO, {"run_id": "$json.body.run_id"}),
        stop(
            "Stop: not started",
            AUTO,
            "'e2e test not started: HTTP ' + ($json.statusCode ?? 0) + ' ' + ($json.body?.message ?? '')",
        ),
        cleanup("Clean up QuickBooks (before)"),
        core_call(
            "Reset test data (core)",
            AUTO,
            "POST",
            RUN_URL + "/reset",
            body="JSON.stringify({ execution_id: $execution.id })",
        )
        | {"executeOnce": True},
        if_node("Reset done?", AUTO, "$json.statusCode === 200"),
    ]


def _send_nodes() -> list[Node]:
    return [
        code_node("One item per message", AUTO, ONE_PER_MESSAGE_JS),
        gmail_api(
            "Put in Gmail", "POST", GMAIL_API, "JSON.stringify({ raw: $json.raw, labelIds: ['INBOX', 'UNREAD'] })"
        ),
        code_node("Gmail IDs", AUTO, GMAIL_IDS_JS),
        if_node("All in Gmail?", AUTO, "$json.inserted === $json.expected"),
        core_call(
            "Record sent (core)",
            AUTO,
            "POST",
            RUN_URL + "/sent",
            body="JSON.stringify({ messages: $json.messages, execution_id: $execution.id })",
        ),
        if_node("Recorded?", AUTO, "$json.statusCode === 200"),
        {
            "name": "Wait 10 minutes",
            "type": "n8n-nodes-base.wait",
            "typeVersion": 1.1,
            "position": AUTO,
            "parameters": {"resume": "timeInterval", "amount": 10, "unit": "minutes"},
        },
    ]


def _collect_nodes() -> list[Node]:
    return [
        gmail("Get labels", {"resource": "label", "operation": "getAll", "returnAll": True}, executeOnce=True),
        code_node("One item per sent message", AUTO, SENT_JS),
        gmail_api("Read labels (Gmail)", "GET", "=" + GMAIL_API + "/{{ $json.gmail_id }}?format=minimal"),
        code_node("Labels by name", AUTO, LABELS_JS),
        # The message ID from the message item: this node's input is the label read's response
        gmail_api(
            "Move to trash (Gmail)",
            "POST",
            "=" + GMAIL_API + "/{{ $('One item per sent message').item.json.gmail_id }}/trash",
        ),
        cleanup("Clean up QuickBooks (after)"),
        core_call(
            "Verify (core)",
            AUTO,
            "POST",
            RUN_URL + "/verify",
            body="JSON.stringify({ messages: $('Labels by name').first().json.messages, execution_id: $execution.id })",
        )
        | {"executeOnce": True},
        if_node("Checked?", AUTO, "$json.statusCode === 200"),
        if_node("Failed?", AUTO, "$json.body.status === 'failed'"),
    ]


def _alert_nodes() -> list[Node]:
    return [
        code_node("Failed step", AUTO, FAILED_STEP_JS),
        core_call(
            "Abort run (core)",
            AUTO,
            "POST",
            RUN_URL + "/abort",
            body="JSON.stringify({ step: $json.step, message: $json.message, execution_id: $execution.id })",
        ),
        if_node("Alert ready?", AUTO, "$json.statusCode === 200 && !!$json.body.alert"),
        gmail(
            "Send alert email",
            {
                "resource": "message",
                "operation": "send",
                "sendTo": "={{ $json.body.alert.to }}",
                "subject": "={{ $json.body.alert.subject }}",
                "emailType": "html",
                "message": "={{ $json.body.alert.html }}",
                "options": {"appendAttribution": False},
            },
        ),
        stop("Stop: no alert", AUTO, "'e2e test failed and the core wrote no alert: HTTP ' + ($json.statusCode ?? 0)"),
    ]


FAILED = [("Failed step", 0)]
CONNECTIONS: Connections = {
    "Every day at 07:30": [[("Start run (core)", 0)]],
    "Manual run": [[("Start run (core)", 0)]],
    "Start run (core)": [[("Started?", 0)]],
    "Started?": [[("Run ID", 0)], [("Stop: not started", 0)]],
    "Run ID": [[("Clean up QuickBooks (before)", 0)]],
    "Clean up QuickBooks (before)": [[("Reset test data (core)", 0)]],
    "Reset test data (core)": [[("Reset done?", 0)]],
    "Reset done?": [[("One item per message", 0)], FAILED],
    "One item per message": [[("Put in Gmail", 0)]],
    "Put in Gmail": [[("Gmail IDs", 0)]],
    "Gmail IDs": [[("All in Gmail?", 0)]],
    "All in Gmail?": [[("Record sent (core)", 0)], FAILED],
    "Record sent (core)": [[("Recorded?", 0)]],
    "Recorded?": [[("Wait 10 minutes", 0)], FAILED],
    "Wait 10 minutes": [[("Get labels", 0)]],
    "Get labels": [[("One item per sent message", 0)]],
    "One item per sent message": [[("Read labels (Gmail)", 0)]],
    "Read labels (Gmail)": [[("Move to trash (Gmail)", 0)]],
    "Move to trash (Gmail)": [[("Labels by name", 0)]],
    "Labels by name": [[("Clean up QuickBooks (after)", 0)]],
    "Clean up QuickBooks (after)": [[("Verify (core)", 0)]],
    "Verify (core)": [[("Checked?", 0)]],
    "Checked?": [[("Failed?", 0)], FAILED],
    "Failed?": [[("Send alert email", 0)]],
    "Failed step": [[("Abort run (core)", 0)]],
    "Abort run (core)": [[("Alert ready?", 0)]],
    "Alert ready?": [[("Send alert email", 0)], [("Stop: no alert", 0)]],
}

SECTIONS = [
    Section(
        "Daily end-to-end test",
        "Every morning before the summary, four test emails go through the **whole pipeline**: one invoice "
        "posted straight to QuickBooks, one sent to review, one rejected, one bank-details change (fraud "
        "alert). Two OCR calls per run.\n\n"
        "Test data is kept apart (scope **e2e**): never in live figures, the touchless rate or the live review "
        "list. The **core** writes the messages, decides what may be deleted and whether the run passed. "
        "A failure sends an alert at once; the 08:00 summary shows the result.",
        col=0,
        row=0,
        kind="overview",
        min_cols=9,
    ),
    Section(
        "Start and reset",
        "The core starts a run. QuickBooks objects of earlier test postings are deleted first (sub-workflow), "
        "then the core deletes the earlier test invoices (**scope e2e only**) and writes the run's messages.",
        col=0,
        row=2,
        grid=(
            ("Every day at 07:30", "Start run (core)", "Started?", "Run ID"),
            ("Manual run", None, "Stop: not started", "Clean up QuickBooks (before)"),
            (None, None, "Reset test data (core)", "Reset done?"),
        ),
    ),
    Section(
        "Put in Gmail",
        "The messages are inserted in the mailbox (Gmail API **messages.insert**), addressed to "
        "**invoices+e2e@**: mail sent from the mailbox to its own alias may never reach the inbox. The email "
        "intake picks them up within a minute.",
        col=5,
        row=2,
        grid=(
            ("One item per message", "Put in Gmail", "Gmail IDs", "All in Gmail?"),
            (None, "Record sent (core)", "Recorded?", "Wait 10 minutes"),
        ),
    ),
    Section(
        "Collect and verify",
        "Gmail labels read, test emails moved to the trash, QuickBooks cleaned up. The core checks status, "
        "labels, log, QuickBooks objects, the clean-up, isolation from live data and the masking of bank numbers.",
        col=5,
        row=5,
        grid=(
            ("Get labels", "One item per sent message", "Read labels (Gmail)", "Move to trash (Gmail)"),
            ("Labels by name", "Clean up QuickBooks (after)", "Verify (core)", "Checked?"),
            (None, None, None, "Failed?"),
        ),
    ),
    Section(
        "Alert",
        "A failed run, or one that could not finish (the step is recorded), sends the core's alert email at "
        "once to the summary's recipients.",
        col=0,
        row=6,
        grid=(("Failed step", "Abort run (core)", "Alert ready?"), (None, "Send alert email", "Stop: no alert")),
        kind="failure",
    ),
]


def build(cred_ids: dict[str, str] | None) -> dict[str, Any]:
    nodes = _start_nodes() + _send_nodes() + _collect_nodes() + _alert_nodes()
    return workflow("AP – Daily e2e test", lay_out(nodes, SECTIONS), CONNECTIONS, cred_ids)


if __name__ == "__main__":
    main(build)
