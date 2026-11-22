"""Builds the 'AP – Email intake' n8n workflow JSON.

Usage:
  python3 n8n/build_email_intake.py > out.json                       # no credential IDs (committable)
  python3 n8n/build_email_intake.py --credentials ids.json > out.json  # with local credential IDs, for import

Generated rather than hand-edited so the canvas layout and wiring stay consistent and reviewable.
"""

import json
import sys

ALIAS = "invoices@davidmichel.dev"
CORE_URL = "http://core:8000/invoices/process"

GUARD_JS = r"""
// Guard and split: one item per invoice attachment, or one item explaining why there is none.
// Deterministic code only: nothing here reads the document content.
const ALIAS = '__ALIAS__';
const LABELS = { processed: 'AP/Processed', ready: 'AP/Ready', review: 'AP/Review', rejected: 'AP/Rejected', error: 'AP/Error' };
const BY_MIME = { 'application/pdf': 1, 'image/png': 1, 'image/jpeg': 1, 'image/webp': 1, 'image/tiff': 1 };
const BY_EXT = { pdf: 'application/pdf', png: 'image/png', jpg: 'image/jpeg', jpeg: 'image/jpeg', webp: 'image/webp', tif: 'image/tiff', tiff: 'image/tiff' };
const MIN_IMAGE_BYTES = 15000;   // smaller images are almost always signature logos

// Label IDs, looked up by name so nothing is hard-coded
const byName = Object.fromEntries($('Get AP labels').all().map(i => [i.json.name, i.json.id]));
const labels = {};
for (const [key, name] of Object.entries(LABELS)) {
  if (!byName[name]) throw new Error(`Gmail label "${name}" not found: create it in Gmail first`);
  labels[key] = byName[name];
}

// n8n stores a human-readable size ("45.2 kB"); convert it back to bytes
function toBytes(size) {
  const m = String(size ?? '').match(/([\d.]+)\s*(B|kB|MB|GB)/i);
  if (!m) return 0;
  return Math.round(parseFloat(m[1]) * { b: 1, kb: 1e3, mb: 1e6, gb: 1e9 }[m[2].toLowerCase()]);
}

const out = [];
for (const email of $('Gmail Trigger').all()) {
  const mail = email.json;
  const base = { message_id: mail.id, thread_id: mail.threadId, labels };

  // Second guard after the Gmail search: the alias must be a recipient
  const recipients = [...(mail.to?.value ?? []), ...(mail.cc?.value ?? [])].map(a => String(a.address ?? '').toLowerCase());
  const deliveredTo = String(mail.headers?.['delivered-to'] ?? '').toLowerCase();
  if (!recipients.includes(ALIAS) && !deliveredTo.includes(ALIAS)) {
    out.push({ json: { ...base, route: 'not_for_alias', reason: 'not_addressed_to_alias' }, pairedItem: { item: 0 } });
    continue;
  }

  const binary = email.binary ?? {};
  const keys = Object.keys(binary).filter(k => k.startsWith('attachment_'))
    .sort((a, b) => Number(a.split('_')[1]) - Number(b.split('_')[1]));
  const accepted = [], skipped = [];
  for (const key of keys) {
    const b = binary[key];
    const ext = String(b.fileName ?? '').split('.').pop().toLowerCase();
    const mime = BY_MIME[b.mimeType] ? b.mimeType : BY_EXT[ext];
    const size = toBytes(b.fileSize);
    if (!mime) { skipped.push({ type: b.mimeType ?? 'unknown', size, why: 'unsupported_type' }); continue; }
    if (mime.startsWith('image/') && size < MIN_IMAGE_BYTES) { skipped.push({ type: mime, size, why: 'small_image' }); continue; }
    accepted.push({ key, index: Number(key.split('_')[1]), mime, size });
  }

  if (accepted.length === 0) {
    // Never silently ignored: logged and labelled (file names are not logged, they may be personal data)
    out.push({ json: { ...base, route: 'no_valid_attachment', reason: 'no_valid_attachment',
                       details: { attachments: keys.length, skipped } }, pairedItem: { item: 0 } });
    continue;
  }
  for (const a of accepted) {
    out.push({
      json: { ...base, route: 'process', source_ref: `gmail:${mail.id}:${a.index}`, mime: a.mime, size: a.size,
              attachments_in_email: accepted.length },
      binary: { file: binary[a.key] },
      pairedItem: { item: 0 },
    });
  }
}
return out;
""".replace("__ALIAS__", ALIAS).strip()

LOG_SQL = (
    "INSERT INTO ap.processing_log (channel, source_ref, n8n_execution_id, step, level, message, details)\n"
    "VALUES ('email', $1, $2, $3, $4, $5, $6::jsonb)"
)


def switch_rule(output_key: str, expression: str) -> dict:
    """Switch v3 rule that matches when a boolean expression is true."""
    return {
        "conditions": {
            "options": {"caseSensitive": True, "leftValue": "", "typeValidation": "strict", "version": 2},
            "conditions": [{
                "id": output_key.replace(" ", "-"),
                "leftValue": "={{ " + expression + " }}",
                "rightValue": "",
                "operator": {"type": "boolean", "operation": "true", "singleValue": True},
            }],
            "combinator": "and",
        },
        "renameOutput": True,
        "outputKey": output_key,
    }


def label_node(name: str, label_keys: list[str], position: list[int]) -> dict:
    labels = ", ".join(f"$('Guard and split').item.json.labels.{k}" for k in label_keys)
    return {
        "name": name, "type": "n8n-nodes-base.gmail", "typeVersion": 2.1, "position": position,
        "parameters": {
            "resource": "message", "operation": "addLabels",
            "messageId": "={{ $('Guard and split').item.json.message_id }}",
            "labelIds": "={{ [" + labels + "] }}",
        },
        "credentials": {"gmailOAuth2": "GMAIL"},
    }


def log_node(name: str, position: list[int], step: str, level: str, message: str, source_ref: str, details: str) -> dict:
    return {
        "name": name, "type": "n8n-nodes-base.postgres", "typeVersion": 2.6, "position": position,
        "parameters": {
            "operation": "executeQuery",
            "query": LOG_SQL,
            "options": {"queryReplacement": "={{ [" + ", ".join([
                source_ref, "$execution.id", f"'{step}'", f"'{level}'", message, f"JSON.stringify({details})",
            ]) + "] }}"},
        },
        "credentials": {"postgres": "POSTGRES"},
    }


def build(cred_ids: dict | None) -> dict:
    g = "$('Guard and split').item.json"
    nodes = [
        {
            "name": "Gmail Trigger", "type": "n8n-nodes-base.gmailTrigger", "typeVersion": 1.4, "position": [0, 300],
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
            "name": "Get AP labels", "type": "n8n-nodes-base.gmail", "typeVersion": 2.1, "position": [220, 300],
            "executeOnce": True,
            "parameters": {"resource": "label", "operation": "getAll", "returnAll": True},
            "credentials": {"gmailOAuth2": "GMAIL"},
        },
        {
            "name": "Guard and split", "type": "n8n-nodes-base.code", "typeVersion": 2, "position": [440, 300],
            "parameters": {"mode": "runOnceForAllItems", "language": "javaScript", "jsCode": GUARD_JS},
        },
        {
            "name": "Route intake", "type": "n8n-nodes-base.switch", "typeVersion": 3.2, "position": [660, 300],
            "parameters": {"rules": {"values": [
                switch_rule("invoice attachment", "$json.route === 'process'"),
                switch_rule("no valid attachment", "$json.route === 'no_valid_attachment'"),
                switch_rule("not for alias", "$json.route === 'not_for_alias'"),
            ]}, "options": {}},
        },
        {
            "name": "Process invoice (core)", "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2,
            "position": [900, 120], "onError": "continueErrorOutput",
            "parameters": {
                "method": "POST", "url": CORE_URL,
                "authentication": "genericCredentialType", "genericAuthType": "httpHeaderAuth",
                "sendBody": True, "contentType": "multipart-form-data",
                "bodyParameters": {"parameters": [
                    {"parameterType": "formBinaryData", "name": "file", "inputDataFieldName": "file"},
                    {"name": "channel", "value": "email"},
                    {"name": "source_ref", "value": "={{ $json.source_ref }}"},
                    {"name": "n8n_execution_id", "value": "={{ $execution.id }}"},
                ]},
                "options": {
                    "timeout": 300000,
                    "response": {"response": {"fullResponse": True, "neverError": True, "responseFormat": "json"}},
                },
            },
            "credentials": {"httpHeaderAuth": "CORE"},
        },
        {
            "name": "Route by status", "type": "n8n-nodes-base.switch", "typeVersion": 3.2, "position": [1140, 100],
            "parameters": {
                "rules": {"values": [
                    switch_rule("ready", "$json.statusCode === 200 && $json.body.status === 'ready'"),
                    switch_rule("needs_review", "$json.statusCode === 200 && $json.body.status === 'needs_review'"),
                    # 413/415: file too large / not a supported file → nothing a human can do with it
                    switch_rule("rejected", "($json.statusCode === 200 && $json.body.status === 'rejected') || [413, 415].includes($json.statusCode)"),
                ]},
                "options": {"fallbackOutput": "extra", "renameFallbackOutput": "error"},
            },
        },
        label_node("Label: ready", ["ready", "processed"], [1400, -100]),
        label_node("Label: review", ["review", "processed"], [1400, 40]),
        label_node("Label: rejected", ["rejected", "processed"], [1400, 180]),
        # Technical error: AP/Error only, NOT AP/Processed (re-checked on Day 3)
        label_node("Label: error", ["error"], [1400, 340]),
        log_node("Log core error", [1620, 340], "core_call", "error", "'core service call failed'",
                 f"{g}.source_ref",
                 "{ status_code: $('Process invoice (core)').item.json.statusCode ?? null, "
                 "error: $('Process invoice (core)').item.json.body?.error ?? $('Process invoice (core)').item.json.error?.message ?? null }"),
        label_node("Label: no valid attachment", ["rejected", "processed"], [900, 360]),
        log_node("Log no valid attachment", [1120, 360], "intake", "warning", "'email without a valid invoice attachment'",
                 f"'gmail:' + {g}.message_id", f"{g}.details"),
        log_node("Log not for alias", [900, 540], "intake", "warning", "'email not addressed to the invoices alias'",
                 "'gmail:' + $json.message_id", "{}"),
    ]
    connections = {
        "Gmail Trigger": [[("Get AP labels", 0)]],
        "Get AP labels": [[("Guard and split", 0)]],
        "Guard and split": [[("Route intake", 0)]],
        "Route intake": [[("Process invoice (core)", 0)], [("Label: no valid attachment", 0)], [("Log not for alias", 0)]],
        "Process invoice (core)": [[("Route by status", 0)], [("Label: error", 0)]],
        "Route by status": [[("Label: ready", 0)], [("Label: review", 0)], [("Label: rejected", 0)], [("Label: error", 0)]],
        "Label: error": [[("Log core error", 0)]],
        "Label: no valid attachment": [[("Log no valid attachment", 0)]],
    }

    names = {"GMAIL": "Gmail – david@davidmichel.dev", "CORE": "AP core token", "POSTGRES": "AP database (ap_app)"}
    for node in nodes:
        node["id"] = node["name"].lower().replace(" ", "-").replace(":", "").replace("(", "").replace(")", "")
        for ctype, key in node.get("credentials", {}).items():
            ref = {"name": names[key]}
            if cred_ids:
                ref["id"] = cred_ids[key]
            node["credentials"][ctype] = ref
    return {
        "name": "AP – Email intake",
        "nodes": nodes,
        "connections": {
            src: {"main": [[{"node": n, "type": "main", "index": i} for n, i in out] for out in outs]}
            for src, outs in connections.items()
        },
        "settings": {"executionOrder": "v1", "saveDataErrorExecution": "all", "saveDataSuccessExecution": "all",
                     "saveManualExecutions": True, "timezone": "Europe/Paris"},
        "pinData": {},
        "active": False,
    }


if __name__ == "__main__":
    ids = None
    if len(sys.argv) == 3 and sys.argv[1] == "--credentials":
        ids = json.load(open(sys.argv[2]))
    print(json.dumps(build(ids), indent=2, ensure_ascii=False, sort_keys=True))
