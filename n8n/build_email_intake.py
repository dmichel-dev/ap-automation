"""Builds the 'AP – Email intake' n8n workflow JSON.

Usage:
  python3 n8n/build_email_intake.py > out.json                         # no credential IDs (committable)
  python3 n8n/build_email_intake.py --credentials ids.json > out.json  # with local credential IDs, for import

Flow: Gmail trigger → email metadata → the core screens it (alias guard, which attachments) →
one item per attachment → the core processes it → Gmail labels by status, posting started for
ready invoices. Emails without a valid attachment, or not for the alias, are labelled and logged.
A demo email the core refuses (invoices+CODE@ with no live session, or over its sender's daily limit)
is deleted permanently at once; the core has logged it and put it on the demo clean-up's list too.

A failure (OCR service down, core unreachable) labels the email AP/Error and is reported to the core,
which decides: retried later or given up. Every 15 minutes the re-check lists AP/Error emails, asks the
core which are due (one with no failure on record is due at once) and sends them down the same path.
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
    gmail,
    if_node,
    lay_out,
    log_node,
    main,
    post_to_quickbooks,
    process_invoice,
    route_by_status,
    switch,
    switch_rule,
    workflow,
)

ALIAS = "invoices@davidmichel.dev"  # Gmail search filter; the core checks the recipients again
E2E_ADDRESS = "invoices+e2e@davidmichel.dev"  # the daily end-to-end test's emails (scope decided by the core)
TO_US = f"{{deliveredto:{ALIAS} deliveredto:{E2E_ADDRESS} to:{E2E_ADDRESS}}}"
EMAIL_IN = "Email in"  # new emails and re-checked ones meet here
SPLIT = "Split by attachment"
ITEM = f"$('{SPLIT}').item.json"
ERR = "$('Error facts').item.json"

DESCRIBE_EMAIL_JS = r"""
// For the core's screening: metadata (recipients, sender, attachment types, names, sizes) and the
// text (subject + body, which the core checks for bank-change wording and never stores; it accepts
// up to 20,000 characters). The sender counts toward the demo's daily limit (stored only as a hash).
return $('Email in').all().map((email, i) => {
  const mail = email.json;
  const files = Object.entries(email.binary ?? {}).filter(([key]) => /^attachment_\d+$/.test(key));
  return { json: {
    message_id: mail.id,
    thread_id: mail.threadId,
    recipients: [...(mail.to?.value ?? []), ...(mail.cc?.value ?? [])].map(a => String(a.address ?? '')),
    delivered_to: String(mail.headers?.['delivered-to'] ?? ''),
    sender: String(mail.from?.value?.[0]?.address ?? '').slice(0, 500),
    subject: String(mail.subject ?? '').slice(0, 2000),
    attachments: files.map(([key, b]) => ({ key, mime_type: b.mimeType ?? null, file_name: b.fileName ?? null,
                                           size: b.fileSize ?? null })),
    text: [mail.subject, mail.text].filter(Boolean).join('\n\n').slice(0, 20000),
  }, pairedItem: { item: i } };
});
"""

SPLIT_JS = r"""
// One item per attachment the core accepted (with its file), or one item saying why there is none.
// File names are not passed on to the logs: they may be personal data.
const LABELS = { processed: 'AP/Processed', ready: 'AP/Ready', review: 'AP/Review',
                 rejected: 'AP/Rejected', error: 'AP/Error', fraud: 'AP/Fraud alert' };

// Label IDs, looked up by name so nothing is hard-coded
const byName = Object.fromEntries($('Get AP labels').all().map(i => [i.json.name, i.json.id]));
const labels = {};
for (const [key, name] of Object.entries(LABELS)) {
  if (!byName[name]) throw new Error(`Gmail label "${name}" not found: create it in Gmail first`);
  labels[key] = byName[name];
}

const emails = $('Email facts').all();
const files = $('Email in').all();
const out = [];
$input.all().forEach((screen, i) => {
  const mail = emails[i].json, s = screen.json.body;
  const base = { message_id: mail.message_id, thread_id: mail.thread_id, labels, scope: s?.scope ?? null };
  if (screen.json.statusCode !== 200) {  // the screening itself failed: what was seen, for the core
    out.push({ json: { ...base, route: 'error', stage: 'screen', source_ref: `gmail:${mail.message_id}`,
                       status_code: screen.json.statusCode ?? null, error: s?.error ?? null,
                       message: String(s?.message ?? screen.json.error?.message ?? '').slice(0, 300) },
               pairedItem: { item: i } });
    return;
  }
  if (s.route !== 'process') {
    out.push({ json: { ...base, route: s.route, reason: s.reason,
                       details: { attachments: s.attachments, skipped: s.skipped } }, pairedItem: { item: i } });
    return;
  }
  for (const a of s.accepted) {
    out.push({
      json: { ...base, route: 'process', source_ref: `gmail:${mail.message_id}:${a.index}`, mime: a.mime,
              size: a.size, attachments_in_email: s.accepted.length, email_text: mail.text },
      binary: { file: files[i].binary[a.key] },
      pairedItem: { item: i },
    });
  }
});
return out;
"""

ERROR_FACTS_JS = r"""
// What failed, for the log and the core: the screening (already described by the split) or the
// processing of one attachment. The core decides what it means (retried later, or given up).
return $input.all().map((item, i) => {
  if (item.json.route === 'error') return { json: item.json, pairedItem: { item: i } };
  const { email_text, ...source } = $('Split by attachment').itemMatching(i).json;
  const r = item.json;
  return { json: { ...source, route: 'error', stage: 'process', status_code: r.statusCode ?? null,
                   error: r.body?.error ?? null,
                   message: String(r.body?.message ?? r.error?.message ?? '').slice(0, 300) },
           pairedItem: { item: i } };
});
"""

DUE_JS = r"""
// One item per email the core says to process now (it decides which, and counts the attempt).
const answer = $input.first().json;
if (answer.statusCode !== 200) {
  throw new Error(`re-check: the core answered ${answer.statusCode ?? 'nothing'}; the next run tries again`);
}
return answer.body.due.map(id => ({ json: { id } }));
"""

# Emails that failed and are not given up; the core also hands out due ones whose label was lost
RECHECK_QUERY = f"{TO_US} label:ap-error -label:ap-processed newer_than:14d"
GMAIL_ATTACHMENTS = {"downloadAttachments": True, "dataPropertyAttachmentsPrefixName": "attachment_"}


def label_node(name: str, label_keys: list[str], operation: str = "addLabels") -> Node:
    labels = ", ".join(f"{ITEM}.labels.{k}" for k in label_keys)
    return gmail(
        name,
        {
            "resource": "message",
            "operation": operation,
            "messageId": "={{ " + ITEM + ".message_id }}",
            "labelIds": "={{ [" + labels + "] }}",
        },
    )


def _intake_nodes() -> list[Node]:
    return [
        {
            "name": "New email (Gmail)",
            "type": "n8n-nodes-base.gmailTrigger",
            "typeVersion": 1.4,
            "position": AUTO,
            "parameters": {
                "pollTimes": {"item": [{"mode": "everyMinute"}]},
                "simple": False,
                "filters": {"q": f"{TO_US} -label:ap-processed", "readStatus": "both"},
                "options": GMAIL_ATTACHMENTS,
                "maxResults": 10,
            },
            "credentials": {"gmailOAuth2": "GMAIL"},
        },
        {"name": EMAIL_IN, "type": "n8n-nodes-base.noOp", "typeVersion": 1, "position": AUTO, "parameters": {}},
        gmail("Get AP labels", {"resource": "label", "operation": "getAll", "returnAll": True}, executeOnce=True),
        code_node("Email facts", AUTO, DESCRIBE_EMAIL_JS),
        core_call(
            "Screen email (core)",
            AUTO,
            "POST",
            f"={CORE}/intake/email/screen",
            body="JSON.stringify({ recipients: $json.recipients, delivered_to: $json.delivered_to, "
            "sender: $json.sender, attachments: $json.attachments, text: $json.text, subject: $json.subject, "
            "message_id: $json.message_id, execution_id: $execution.id })",
        ),
        code_node(SPLIT, AUTO, SPLIT_JS),
        switch(
            "Route intake",
            AUTO,
            [
                switch_rule("invoice attachment", "$json.route === 'process'"),
                switch_rule("no valid attachment", "$json.route === 'no_valid_attachment'"),
                switch_rule("bank detail notice", "$json.route === 'bank_detail_notice'"),
                switch_rule("not for alias", "$json.route === 'not_for_alias'"),
                switch_rule("core error", "$json.route === 'error'"),
                switch_rule("demo refused", "$json.route === 'demo_refused'"),
            ],
            {},
        ),
    ]


def _recheck_nodes() -> list[Node]:
    return [
        {
            "name": "Every 15 minutes",
            "type": "n8n-nodes-base.scheduleTrigger",
            "typeVersion": 1.2,
            "position": AUTO,
            "parameters": {"rule": {"interval": [{"field": "minutes", "minutesInterval": 15}]}},
        },
        gmail(
            "Find AP/Error emails",
            {
                "resource": "message",
                "operation": "getAll",
                "returnAll": False,
                "limit": 50,
                "simple": True,
                "filters": {"q": RECHECK_QUERY},
            },
            alwaysOutputData=True,  # none listed: the core may still have due emails whose label was lost
        ),
        core_call(
            "Which are due? (core)",
            AUTO,
            "POST",
            f"={CORE}/intake/failures/due",
            body="JSON.stringify({ found: $input.all().map(i => i.json.id).filter(Boolean), "
            "execution_id: $execution.id })",
        )
        | {"executeOnce": True},
        code_node("One item per due email", AUTO, DUE_JS),
        gmail(
            "Get email (Gmail)",
            {
                "resource": "message",
                "operation": "get",
                "messageId": "={{ $json.id }}",
                "simple": False,
                "options": GMAIL_ATTACHMENTS,
            },
        ),
    ]


def _processing_nodes() -> list[Node]:
    return [
        process_invoice(
            "email",
            [
                ("source_ref", "$json.source_ref"),
                ("message_id", "$json.message_id"),
                ("n8n_execution_id", "$execution.id"),
                ("email_text", "$json.email_text"),
                ("scope", "$json.scope"),
            ],
        ),
        route_by_status(),
        label_node("Label: ready", ["ready", "processed"]),
        post_to_quickbooks(),
        label_node("Label: review", ["review", "processed"]),
        label_node("Label: rejected", ["rejected", "processed"]),
        # An email that got an answer is no longer in error (a re-checked one, or nothing to remove)
        label_node("Clear AP/Error", ["error"], operation="removeLabels"),
    ]


def _error_nodes() -> list[Node]:
    return [
        code_node("Error facts", AUTO, ERROR_FACTS_JS),
        # AP/Error only, NOT AP/Processed: the re-check picks it up
        label_node("Label: error", ["error"]),
        # Logged here first: if the core is unreachable, this is the only record (the label is the other)
        log_node(
            "Log core error",
            "email",
            "core_call",
            "error",
            f"'core service call failed (' + {ERR}.stage + ')'",
            f"{ERR}.source_ref",
            f"{{ stage: {ERR}.stage, status_code: {ERR}.status_code, error: {ERR}.error, message: {ERR}.message }}",
            f"{ERR}.scope",
        ),
        core_call(
            "Report failure (core)",
            AUTO,
            "POST",
            f"={CORE}/intake/failures",
            body=f"JSON.stringify({{ message_id: {ERR}.message_id, stage: {ERR}.stage, "
            f"status_code: {ERR}.status_code, error: {ERR}.error, scope: {ERR}.scope, execution_id: $execution.id }})",
        ),
        if_node("Given up?", AUTO, "$json.statusCode === 200 && $json.body?.state === 'given_up'"),
        # Done, failed: a person looks (log, daily summary). AP/Error stays.
        label_node("Label: given up", ["processed"]),
    ]


def _not_processed_nodes() -> list[Node]:
    return [
        label_node("Label: no valid attachment", ["rejected", "processed"]),
        log_node(
            "Log no valid attachment",
            "email",
            "intake",
            "warning",
            "'email without a valid invoice attachment'",
            f"'gmail:' + {ITEM}.message_id",
            f"{ITEM}.details",
            f"{ITEM}.scope",
        ),
        label_node("Label: fraud alert", ["fraud", "processed"]),
        log_node(
            "Log fraud alert",
            "email",
            "intake",
            "warning",
            "'possible_bank_detail_change: email asks to change bank details, no invoice attached'",
            f"'gmail:' + {ITEM}.message_id",
            f"{ITEM}.details",
            f"{ITEM}.scope",
        ),
        log_node(
            "Log not for alias",
            "email",
            "intake",
            "warning",
            "'email not addressed to the invoices alias'",
            "'gmail:' + $json.message_id",
            "{}",
            "$json.scope",
        ),
        # Permanent (not the trash). A failure is harmless: the demo clean-up deletes it later.
        gmail(
            "Delete refused demo email",
            {"resource": "message", "operation": "delete", "messageId": "={{ $json.message_id }}"},
            onError="continueRegularOutput",
        ),
    ]


CLEAR = [("Clear AP/Error", 0)]
CONNECTIONS: Connections = {
    "New email (Gmail)": [[(EMAIL_IN, 0)]],
    "Every 15 minutes": [[("Find AP/Error emails", 0)]],
    "Find AP/Error emails": [[("Which are due? (core)", 0)]],
    "Which are due? (core)": [[("One item per due email", 0)]],
    "One item per due email": [[("Get email (Gmail)", 0)]],
    "Get email (Gmail)": [[(EMAIL_IN, 0)]],
    EMAIL_IN: [[("Get AP labels", 0)]],
    "Get AP labels": [[("Email facts", 0)]],
    "Email facts": [[("Screen email (core)", 0)]],
    "Screen email (core)": [[(SPLIT, 0)]],
    SPLIT: [[("Route intake", 0)]],
    "Route intake": [
        [("Process invoice (core)", 0)],
        [("Label: no valid attachment", 0)],
        [("Label: fraud alert", 0)],
        [("Log not for alias", 0)],
        [("Error facts", 0)],
        [("Delete refused demo email", 0)],
    ],
    "Process invoice (core)": [[("Route by status", 0)], [("Error facts", 0)]],
    "Route by status": [
        [("Label: ready", 0)],
        [("Label: review", 0)],
        [("Label: rejected", 0)],
        [("Error facts", 0)],
    ],
    "Label: ready": [[("Post to QuickBooks", 0), *CLEAR]],
    "Label: review": [CLEAR],
    "Label: rejected": [CLEAR],
    "Error facts": [[("Label: error", 0)]],
    "Label: error": [[("Log core error", 0)]],
    "Log core error": [[("Report failure (core)", 0)]],
    "Report failure (core)": [[("Given up?", 0)]],
    "Given up?": [[("Label: given up", 0)]],
    "Label: no valid attachment": [[("Log no valid attachment", 0), *CLEAR]],
    "Label: fraud alert": [[("Log fraud alert", 0), *CLEAR]],
}


SECTIONS = [
    Section(
        "Email intake",
        "Turns emails sent to **invoices@** into processed invoices.\n\n"
        "n8n receives the email and applies the Gmail labels. The **core service decides** everything else: "
        "which emails and attachments to process, the extracted data, the checks and the status, and what a "
        "failure means (retried later, or given up). The AI only extracts data; every decision is deterministic "
        "code.",
        col=0,
        row=0,
        kind="overview",
        min_cols=7,
    ),
    Section(
        "Receive",
        "Checks Gmail every minute for emails delivered to the alias (or its **+e2e** test address) and not "
        "yet processed. Re-checked emails join here.",
        col=0,
        row=2,
        grid=(("New email (Gmail)", EMAIL_IN, "Get AP labels", "Email facts"),),
    ),
    Section(
        "Re-check AP/Error",
        "Every 15 minutes: emails labelled **AP/Error** go to the core, which says which are due (backoff 15 min, "
        "1 h, 4 h, 12 h; 5 attempts). One with no failure on record (the core was down) is due at once.",
        col=0,
        row=4,
        grid=(
            ("Every 15 minutes", "Find AP/Error emails", "Which are due? (core)"),
            (None, "One item per due email", "Get email (Gmail)"),
        ),
    ),
    Section(
        "Screen (core)",
        "The core checks the recipients, says whose data it is (live, or the daily end-to-end test's) and keeps "
        "only PDF and image attachments. One item per attachment.",
        col=4,
        row=2,
        grid=(("Screen email (core)", SPLIT, "Route intake"),),
    ),
    Section(
        "Process (core)",
        "Extract, normalize, check, store. The answer is **ready**, **needs review** or **rejected**.",
        col=7,
        row=2,
        grid=(("Process invoice (core)", "Route by status"),),
    ),
    Section(
        "Label, then post",
        "Gmail labels show the outcome; AP/Error is removed once an email has an answer. Ready invoices start "
        "the posting workflow.",
        col=9,
        row=2,
        grid=(("Label: ready", "Post to QuickBooks"), ("Label: review", "Clear AP/Error"), ("Label: rejected", None)),
    ),
    Section(
        "Not processed",
        "No PDF or image attached, or not addressed to the alias: labelled and logged, never ignored silently. "
        "An email asking to change bank details, with no invoice, is a **fraud alert** (AP/Fraud alert). "
        "A **demo** email the core refuses (session over, sender limit) is deleted permanently at once.",
        col=4,
        row=4,
        grid=(
            ("Label: no valid attachment", "Log no valid attachment"),
            ("Label: fraud alert", "Log fraud alert"),
            ("Log not for alias", "Delete refused demo email"),
        ),
        kind="failure",
    ),
    Section(
        "Technical error",
        "The screening or processing failed (e.g. OCR service down, core unreachable): labelled **AP/Error**, "
        "logged, reported to the core. Temporary → the re-check retries it. Permanent, or 5 attempts used → "
        "**given up** (AP/Processed added): a person looks.",
        col=6,
        row=5,
        grid=(
            ("Error facts", "Label: error", "Log core error"),
            ("Report failure (core)", "Given up?", "Label: given up"),
        ),
        kind="failure",
    ),
]


def build(cred_ids: dict[str, str] | None) -> dict[str, Any]:
    nodes = _intake_nodes() + _recheck_nodes() + _processing_nodes() + _error_nodes() + _not_processed_nodes()
    return workflow("AP – Email intake", lay_out(nodes, SECTIONS), CONNECTIONS, cred_ids)


if __name__ == "__main__":
    main(build)
