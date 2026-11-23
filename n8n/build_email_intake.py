"""Builds the 'AP – Email intake' n8n workflow JSON.

Usage:
  python3 n8n/build_email_intake.py > out.json                         # no credential IDs (committable)
  python3 n8n/build_email_intake.py --credentials ids.json > out.json  # with local credential IDs, for import

Flow: Gmail trigger → email metadata → the core screens it (alias guard, which attachments) →
one item per attachment → the core processes it → Gmail labels by status, posting started for
ready invoices. Emails without a valid attachment, or not for the alias, are labelled and logged.
"""

from typing import Any

from nodes import CORE, Connections, Node, code_node, condition, core_call, main, workflow

ALIAS = "invoices@davidmichel.dev"  # Gmail search filter; the core checks the recipients again
SPLIT = "Split by attachment"
ITEM = f"$('{SPLIT}').item.json"

DESCRIBE_EMAIL_JS = r"""
// Metadata only (recipients, attachment types, names, sizes) for the core's screening.
return $('Gmail Trigger').all().map((email, i) => {
  const mail = email.json;
  const files = Object.entries(email.binary ?? {}).filter(([key]) => /^attachment_\d+$/.test(key));
  return { json: {
    message_id: mail.id,
    thread_id: mail.threadId,
    recipients: [...(mail.to?.value ?? []), ...(mail.cc?.value ?? [])].map(a => String(a.address ?? '')),
    delivered_to: String(mail.headers?.['delivered-to'] ?? ''),
    attachments: files.map(([key, b]) => ({ key, mime_type: b.mimeType ?? null, file_name: b.fileName ?? null,
                                           size: b.fileSize ?? null })),
  }, pairedItem: { item: i } };
});
"""

SPLIT_JS = r"""
// One item per attachment the core accepted (with its file), or one item saying why there is none.
// File names are not passed on to the logs: they may be personal data.
const LABELS = { processed: 'AP/Processed', ready: 'AP/Ready', review: 'AP/Review',
                 rejected: 'AP/Rejected', error: 'AP/Error' };

// Label IDs, looked up by name so nothing is hard-coded
const byName = Object.fromEntries($('Get AP labels').all().map(i => [i.json.name, i.json.id]));
const labels = {};
for (const [key, name] of Object.entries(LABELS)) {
  if (!byName[name]) throw new Error(`Gmail label "${name}" not found: create it in Gmail first`);
  labels[key] = byName[name];
}

const emails = $('Describe email').all();
const files = $('Gmail Trigger').all();
const out = [];
$input.all().forEach((screen, i) => {
  if (screen.json.statusCode !== 200) {
    throw new Error(`email screening failed (core HTTP ${screen.json.statusCode ?? 'no response'})`);
  }
  const mail = emails[i].json, s = screen.json.body;
  const base = { message_id: mail.message_id, thread_id: mail.thread_id, labels };
  if (s.route !== 'process') {
    out.push({ json: { ...base, route: s.route, reason: s.reason,
                       details: { attachments: s.attachments, skipped: s.skipped } }, pairedItem: { item: i } });
    return;
  }
  for (const a of s.accepted) {
    out.push({
      json: { ...base, route: 'process', source_ref: `gmail:${mail.message_id}:${a.index}`, mime: a.mime,
              size: a.size, attachments_in_email: s.accepted.length },
      binary: { file: files[i].binary[a.key] },
      pairedItem: { item: i },
    });
  }
});
return out;
"""

LOG_SQL = (
    "INSERT INTO ap.processing_log (channel, source_ref, n8n_execution_id, step, level, message, details)\n"
    "VALUES ('email', $1, $2, $3, $4, $5, $6::jsonb)"
)


def switch_rule(output_key: str, expression: str) -> dict[str, Any]:
    """Switch rule that matches when a boolean expression is true."""
    return {
        "conditions": condition(expression, "strict", output_key.replace(" ", "-")),
        "renameOutput": True,
        "outputKey": output_key,
    }


def switch(name: str, position: list[int], rules: list[dict[str, Any]], options: dict[str, Any]) -> Node:
    return {
        "name": name,
        "type": "n8n-nodes-base.switch",
        "typeVersion": 3.2,
        "position": position,
        "parameters": {"rules": {"values": rules}, "options": options},
    }


def label_node(name: str, label_keys: list[str], position: list[int]) -> Node:
    labels = ", ".join(f"{ITEM}.labels.{k}" for k in label_keys)
    return {
        "name": name,
        "type": "n8n-nodes-base.gmail",
        "typeVersion": 2.1,
        "position": position,
        "parameters": {
            "resource": "message",
            "operation": "addLabels",
            "messageId": "={{ " + ITEM + ".message_id }}",
            "labelIds": "={{ [" + labels + "] }}",
        },
        "credentials": {"gmailOAuth2": "GMAIL"},
    }


def log_node(
    name: str, position: list[int], step: str, level: str, message: str, source_ref: str, details: str
) -> Node:
    replacements = [source_ref, "$execution.id", f"'{step}'", f"'{level}'", message, f"JSON.stringify({details})"]
    return {
        "name": name,
        "type": "n8n-nodes-base.postgres",
        "typeVersion": 2.6,
        "position": position,
        "parameters": {
            "operation": "executeQuery",
            "query": LOG_SQL,
            "options": {"queryReplacement": "={{ [" + ", ".join(replacements) + "] }}"},
        },
        "credentials": {"postgres": "POSTGRES"},
    }


def _intake_nodes() -> list[Node]:
    return [
        {
            "name": "Gmail Trigger",
            "type": "n8n-nodes-base.gmailTrigger",
            "typeVersion": 1.4,
            "position": [0, 300],
            "parameters": {
                "pollTimes": {"item": [{"mode": "everyMinute"}]},
                "simple": False,
                "filters": {"q": f"deliveredto:{ALIAS} -label:ap-processed", "readStatus": "both"},
                "options": {"downloadAttachments": True, "dataPropertyAttachmentsPrefixName": "attachment_"},
                "maxResults": 10,
            },
            "credentials": {"gmailOAuth2": "GMAIL"},
        },
        {
            "name": "Get AP labels",
            "type": "n8n-nodes-base.gmail",
            "typeVersion": 2.1,
            "position": [220, 300],
            "executeOnce": True,
            "parameters": {"resource": "label", "operation": "getAll", "returnAll": True},
            "credentials": {"gmailOAuth2": "GMAIL"},
        },
        code_node("Describe email", [440, 300], DESCRIBE_EMAIL_JS),
        core_call(
            "Screen email (core)",
            [660, 300],
            "POST",
            f"={CORE}/intake/email/screen",
            body="JSON.stringify({ recipients: $json.recipients, delivered_to: $json.delivered_to, "
            "attachments: $json.attachments })",
        ),
        code_node(SPLIT, [880, 300], SPLIT_JS),
        switch(
            "Route intake",
            [1100, 300],
            [
                switch_rule("invoice attachment", "$json.route === 'process'"),
                switch_rule("no valid attachment", "$json.route === 'no_valid_attachment'"),
                switch_rule("not for alias", "$json.route === 'not_for_alias'"),
            ],
            {},
        ),
    ]


def _processing_nodes() -> list[Node]:
    return [
        {
            "name": "Process invoice (core)",
            "type": "n8n-nodes-base.httpRequest",
            "typeVersion": 4.2,
            "position": [1340, 120],
            "onError": "continueErrorOutput",
            "parameters": {
                "method": "POST",
                "url": f"{CORE}/invoices/process",
                "authentication": "genericCredentialType",
                "genericAuthType": "httpHeaderAuth",
                "sendBody": True,
                "contentType": "multipart-form-data",
                "bodyParameters": {
                    "parameters": [
                        {"parameterType": "formBinaryData", "name": "file", "inputDataFieldName": "file"},
                        {"name": "channel", "value": "email"},
                        {"name": "source_ref", "value": "={{ $json.source_ref }}"},
                        {"name": "n8n_execution_id", "value": "={{ $execution.id }}"},
                    ]
                },
                "options": {
                    "timeout": 300000,
                    "response": {"response": {"fullResponse": True, "neverError": True, "responseFormat": "json"}},
                },
            },
            "credentials": {"httpHeaderAuth": "CORE"},
        },
        # Routing on the core's answer; 413/415 (too large / not a supported file): nothing a human can do
        switch(
            "Route by status",
            [1580, 100],
            [
                switch_rule("ready", "$json.statusCode === 200 && $json.body.status === 'ready'"),
                switch_rule("needs_review", "$json.statusCode === 200 && $json.body.status === 'needs_review'"),
                switch_rule(
                    "rejected",
                    "($json.statusCode === 200 && $json.body.status === 'rejected')"
                    " || [413, 415].includes($json.statusCode)",
                ),
            ],
            {"fallbackOutput": "extra", "renameFallbackOutput": "error"},
        ),
        label_node("Label: ready", ["ready", "processed"], [1840, -100]),
        # Post ready invoices right away (the 15-minute schedule is the safety net).
        # Fire and forget: the intake doesn't wait for QuickBooks.
        {
            "name": "Post to QuickBooks",
            "type": "n8n-nodes-base.executeWorkflow",
            "typeVersion": 1.2,
            "position": [2060, -100],
            "executeOnce": True,
            "parameters": {
                "source": "database",
                "workflowId": {"__rl": True, "value": "apPostQbo0000001", "mode": "id"},
                "options": {"waitForSubWorkflow": False},
            },
        },
        label_node("Label: review", ["review", "processed"], [1840, 40]),
        label_node("Label: rejected", ["rejected", "processed"], [1840, 180]),
        # Technical error: AP/Error only, NOT AP/Processed (re-checked later)
        label_node("Label: error", ["error"], [1840, 340]),
        log_node(
            "Log core error",
            [2060, 340],
            "core_call",
            "error",
            "'core service call failed'",
            f"{ITEM}.source_ref",
            "{ status_code: $('Process invoice (core)').item.json.statusCode ?? null, "
            "error: $('Process invoice (core)').item.json.body?.error"
            " ?? $('Process invoice (core)').item.json.error?.message ?? null }",
        ),
    ]


def _not_processed_nodes() -> list[Node]:
    return [
        label_node("Label: no valid attachment", ["rejected", "processed"], [1340, 360]),
        log_node(
            "Log no valid attachment",
            [1560, 360],
            "intake",
            "warning",
            "'email without a valid invoice attachment'",
            f"'gmail:' + {ITEM}.message_id",
            f"{ITEM}.details",
        ),
        log_node(
            "Log not for alias",
            [1340, 540],
            "intake",
            "warning",
            "'email not addressed to the invoices alias'",
            "'gmail:' + $json.message_id",
            "{}",
        ),
    ]


CONNECTIONS: Connections = {
    "Gmail Trigger": [[("Get AP labels", 0)]],
    "Get AP labels": [[("Describe email", 0)]],
    "Describe email": [[("Screen email (core)", 0)]],
    "Screen email (core)": [[(SPLIT, 0)]],
    SPLIT: [[("Route intake", 0)]],
    "Route intake": [
        [("Process invoice (core)", 0)],
        [("Label: no valid attachment", 0)],
        [("Log not for alias", 0)],
    ],
    "Process invoice (core)": [[("Route by status", 0)], [("Label: error", 0)]],
    "Route by status": [
        [("Label: ready", 0)],
        [("Label: review", 0)],
        [("Label: rejected", 0)],
        [("Label: error", 0)],
    ],
    "Label: ready": [[("Post to QuickBooks", 0)]],
    "Label: error": [[("Log core error", 0)]],
    "Label: no valid attachment": [[("Log no valid attachment", 0)]],
}


def build(cred_ids: dict[str, str] | None) -> dict[str, Any]:
    nodes = _intake_nodes() + _processing_nodes() + _not_processed_nodes()
    return workflow("AP – Email intake", nodes, CONNECTIONS, cred_ids)


if __name__ == "__main__":
    main(build)
