"""Builds the 'AP – Drive intake' n8n workflow JSON.

Usage:
  python3 n8n/build_drive_intake.py > out.json                         # no credential IDs (committable)
  python3 n8n/build_drive_intake.py --credentials ids.json > out.json  # with local credential IDs, for import

Flow: every minute → find the AP folders in Google Drive (created when missing) → list the Inbox folder →
the core screens the files (process now, wait, or Error) → each file is downloaded and processed by the
same core endpoint as email → moved to Processed (any status) or Error; ready invoices start the posting.

The Inbox folder is the queue: a file leaves it only once it has an answer. A technical failure (OCR
service down, core unreachable) is reported to the core and the file stays in Inbox; the core says when
its retry is due (same backoff as email) or that it is given up (→ Error).
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

DRIVE_API = "https://www.googleapis.com/drive/v3/files"
ROOT_FOLDER = "AP Automation"  # in My Drive; Inbox, Processed and Error inside it
FOLDER_MIME = "application/vnd.google-apps.folder"
FIND_FOLDERS_QUERY = (
    f"mimeType = '{FOLDER_MIME}' and trashed = false and "
    f"((name = '{ROOT_FOLDER}' and 'root' in parents) or name = 'Inbox' or name = 'Processed' or name = 'Error')"
)
INBOX_QUERY = "='{{ $json.inbox }}' in parents and trashed = false and mimeType != '" + FOLDER_MIME + "'"
SPLIT = "Split by file"
FILE = f"$('{SPLIT}').item.json"
ERR = "$('Error facts').item.json"

FOLDER_PLAN_JS = r"""
// The AP folders' IDs (the oldest of each name, if there are several), and the ones still missing.
// A subfolder can only be created once its parent exists: a new 'AP Automation' gets its subfolders
// on the next run.
const r = $input.first().json;
if (r.statusCode !== 200) throw new Error(`Drive folder search: HTTP ${r.statusCode ?? 'no answer'}`);
const folders = r.body.files ?? [];
const root = folders.find(f => f.name === '__ROOT__');
const inRoot = name => root && folders.find(f => f.name === name && (f.parents ?? []).includes(root.id));
const ids = { root: root?.id ?? null, inbox: inRoot('Inbox')?.id ?? null,
              processed: inRoot('Processed')?.id ?? null, error: inRoot('Error')?.id ?? null };
const missing = !root ? [{ name: '__ROOT__', parent: 'root' }]
  : [['inbox', 'Inbox'], ['processed', 'Processed'], ['error', 'Error']]
      .filter(([key]) => !ids[key]).map(([, name]) => ({ name, parent: root.id }));
return [{ json: { ...ids, missing } }];
""".replace("__ROOT__", ROOT_FOLDER)

MISSING_JS = r"""
return $input.first().json.missing.map(folder => ({ json: folder }));
"""

FILES_TO_SCREEN_JS = r"""
// The Inbox files' metadata for the core (never their content). Drive sends sizes as strings.
const r = $input.first().json;
if (r.statusCode !== 200) throw new Error(`Drive Inbox listing: HTTP ${r.statusCode ?? 'no answer'}`);
const files = (r.body.files ?? []).map(f => ({ id: f.id, name: f.name ?? null, mime_type: f.mimeType ?? null,
                                               size: f.size ?? null }));
return [{ json: { files } }];
"""

SPLIT_JS = r"""
// One item per file the core sends somewhere now: 'process' or 'error'. Files it says to 'wait'
// (being processed by another run, retry not due yet, over this run's limit) stay in Inbox.
const screen = $input.first().json;
if (screen.statusCode !== 200) {
  throw new Error(`Drive screening: the core answered ${screen.statusCode ?? 'nothing'}; the next run tries again`);
}
const folders = $('Folder plan').first().json;
return screen.body.files
  .filter(f => f.route !== 'wait')
  .map(f => ({ json: { id: f.id, route: f.route, reason: f.reason, source_ref: `drive:${f.id}`,
                       folders: { processed: folders.processed, error: folders.error } } }));
"""

ERROR_FACTS_JS = r"""
// What failed, for the log and the core: the download or the processing of one file. The core decides
// what it means (retried later: the file stays in Inbox; or given up: it moves to Error).
const stage = $prevNode.name === 'Download file' ? 'fetch' : 'process';
return $input.all().map((item, i) => {
  const file = $('Split by file').itemMatching(i).json;
  const r = item.json;
  return { json: { id: file.id, source_ref: file.source_ref, stage,
                   status_code: stage === 'process' ? (r.statusCode ?? null) : null,
                   error: r.body?.error ?? null,
                   message: String(r.body?.message ?? r.error?.message ?? r.error ?? '').slice(0, 300) },
           pairedItem: { item: i } };
});
"""


def drive_list(name: str, query: str, fields: str, extra: dict[str, str] | None = None) -> Node:
    """Drive API files.list (the Google Drive credential). Full response, never throws: the next
    Code node checks the status."""
    params = {"q": query, "fields": fields, "orderBy": "createdTime", "pageSize": "100", **(extra or {})}
    return {
        "name": name,
        "type": "n8n-nodes-base.httpRequest",
        "typeVersion": 4.2,
        "position": AUTO,
        "executeOnce": True,
        "onError": "continueRegularOutput",
        **NETWORK_RETRY,
        "parameters": {
            "method": "GET",
            "url": DRIVE_API,
            "authentication": "predefinedCredentialType",
            "nodeCredentialType": "googleDriveOAuth2Api",
            "sendQuery": True,
            "queryParameters": {"parameters": [{"name": k, "value": v} for k, v in params.items()]},
            "options": FULL_RESPONSE,
        },
        "credentials": {"googleDriveOAuth2Api": "DRIVE"},
    }


def drive_node(name: str, parameters: dict[str, Any], **extra: Any) -> Node:
    """A Google Drive node on the AP account's credential."""
    return {
        "name": name,
        "type": "n8n-nodes-base.googleDrive",
        "typeVersion": 3,
        "position": AUTO,
        **extra,
        "parameters": parameters,
        "credentials": {"googleDriveOAuth2Api": "DRIVE"},
    }


def by_id(expression: str) -> dict[str, Any]:
    return {"__rl": True, "value": "={{ " + expression + " }}", "mode": "id"}


MY_DRIVE = {"__rl": True, "value": "My Drive", "mode": "list", "cachedResultName": "My Drive"}


def move_node(name: str, folder_key: str) -> Node:
    return drive_node(
        name,
        {
            "operation": "move",
            "fileId": by_id(f"{FILE}.id"),
            "driveId": MY_DRIVE,
            "folderId": by_id(f"{FILE}.folders.{folder_key}"),
        },
    )


def _folder_nodes() -> list[Node]:
    return [
        {
            "name": "Every minute",
            "type": "n8n-nodes-base.scheduleTrigger",
            "typeVersion": 1.2,
            "position": AUTO,
            "parameters": {"rule": {"interval": [{"field": "minutes", "minutesInterval": 1}]}},
        },
        drive_list("Find AP folders", FIND_FOLDERS_QUERY, "files(id,name,parents)"),
        code_node("Folder plan", AUTO, FOLDER_PLAN_JS),
        if_node("Folders ready?", AUTO, "$json.missing.length === 0"),
        code_node("Missing folders", AUTO, MISSING_JS),
        drive_node(
            "Create folder",
            {
                "resource": "folder",
                "name": "={{ $json.name }}",
                "driveId": MY_DRIVE,
                "folderId": by_id("$json.parent"),
                "options": {},
            },
        ),
    ]


def _screen_nodes() -> list[Node]:
    return [
        drive_list("List Inbox", INBOX_QUERY, "files(id,name,mimeType,size)", {"pageSize": "50"}),
        code_node("Files to screen", AUTO, FILES_TO_SCREEN_JS),
        core_call(
            "Screen files (core)",
            AUTO,
            "POST",
            f"={CORE}/intake/drive/screen",
            body="JSON.stringify({ files: $json.files, execution_id: $execution.id })",
        ),
        code_node(SPLIT, AUTO, SPLIT_JS),
        switch(
            "Route file",
            AUTO,
            [switch_rule("process", "$json.route === 'process'"), switch_rule("error", "$json.route === 'error'")],
            {},
        ),
    ]


def _processing_nodes() -> list[Node]:
    return [
        drive_node(
            "Download file",
            {"operation": "download", "fileId": by_id("$json.id"), "options": {"binaryPropertyName": "file"}},
            onError="continueErrorOutput",
            **NETWORK_RETRY,
        ),
        process_invoice(
            "drive",
            [
                ("source_ref", f"{FILE}.source_ref"),
                ("message_id", f"{FILE}.id"),
                ("n8n_execution_id", "$execution.id"),
            ],
        ),
        route_by_status(refused=True),
        move_node("Move to Processed", "processed"),
        if_node("Ready?", AUTO, "$('Route by status').item.json.body?.status === 'ready'"),
        post_to_quickbooks(),
        move_node("Move to Error", "error"),
    ]


def _error_nodes() -> list[Node]:
    return [
        code_node("Error facts", AUTO, ERROR_FACTS_JS),
        # Logged here first: if the core is unreachable, this is the only record (the file in Inbox is the other)
        log_node(
            "Log core error",
            "drive",
            "core_call",
            "error",
            f"'core service call failed (' + {ERR}.stage + ')'",
            f"{ERR}.source_ref",
            f"{{ stage: {ERR}.stage, status_code: {ERR}.status_code, error: {ERR}.error, message: {ERR}.message }}",
            "'live'",
        ),
        core_call(
            "Report failure (core)",
            AUTO,
            "POST",
            f"={CORE}/intake/failures",
            body=f"JSON.stringify({{ channel: 'drive', message_id: {ERR}.id, stage: {ERR}.stage, "
            f"status_code: {ERR}.status_code, error: {ERR}.error, execution_id: $execution.id }})",
        ),
        if_node("Given up?", AUTO, "$json.statusCode === 200 && $json.body?.state === 'given_up'"),
    ]


TO_ERROR = [("Move to Error", 0)]
CONNECTIONS: Connections = {
    "Every minute": [[("Find AP folders", 0)]],
    "Find AP folders": [[("Folder plan", 0)]],
    "Folder plan": [[("Folders ready?", 0)]],
    "Folders ready?": [[("List Inbox", 0)], [("Missing folders", 0)]],
    "Missing folders": [[("Create folder", 0)]],
    "List Inbox": [[("Files to screen", 0)]],
    "Files to screen": [[("Screen files (core)", 0)]],
    "Screen files (core)": [[(SPLIT, 0)]],
    SPLIT: [[("Route file", 0)]],
    "Route file": [[("Download file", 0)], TO_ERROR],
    "Download file": [[("Process invoice (core)", 0)], [("Error facts", 0)]],
    "Process invoice (core)": [[("Route by status", 0)], [("Error facts", 0)]],
    "Route by status": [
        [("Move to Processed", 0)],
        [("Move to Processed", 0)],
        [("Move to Processed", 0)],
        TO_ERROR,
        [("Error facts", 0)],
    ],
    "Move to Processed": [[("Ready?", 0)]],
    "Ready?": [[("Post to QuickBooks", 0)]],
    "Error facts": [[("Log core error", 0)]],
    "Log core error": [[("Report failure (core)", 0)]],
    "Report failure (core)": [[("Given up?", 0)]],
    "Given up?": [TO_ERROR],
}


SECTIONS = [
    Section(
        "Drive intake",
        "Turns files dropped in the Google Drive folder **AP Automation/Inbox** into processed invoices, "
        "through the same core as email: same extraction, checks, duplicate detection and review.\n\n"
        "n8n lists the folder and moves the files. The **core service decides** which files to process, "
        "the status, and what a failure means. The AI only extracts data; every decision is deterministic code.",
        col=0,
        row=0,
        kind="overview",
        min_cols=7,
    ),
    Section(
        "Find folders",
        "Every minute: finds **AP Automation** and its **Inbox**, **Processed** and **Error** folders. "
        "Missing ones are created (the subfolders on the run after a new AP Automation); processing starts "
        "on the next run.",
        col=0,
        row=2,
        grid=(
            ("Every minute", "Find AP folders", "Folder plan", "Folders ready?"),
            (None, None, "Missing folders", "Create folder"),
        ),
    ),
    Section(
        "Screen (core)",
        "The Inbox is the queue. The core answers per file: **process** now, **wait** (being processed, "
        "retry not due, or next run), or **error** (not a PDF or image, too large, given up: logged).",
        col=4,
        row=2,
        grid=(("List Inbox", "Files to screen", "Screen files (core)", SPLIT, "Route file"),),
    ),
    Section(
        "Process (core)",
        "Download, then extract, normalize, check, store: the same endpoint as email (source **drive:<file "
        "ID>**). The same file already received by email is an exact duplicate.",
        col=9,
        row=2,
        grid=(("Download file", "Process invoice (core)", "Route by status"),),
    ),
    Section(
        "Move, then post",
        "Any status (ready, needs review, rejected) → **Processed**; ready invoices start the posting workflow. "
        "A file the core refuses (too large, not really a PDF or image) → **Error**.",
        col=12,
        row=2,
        grid=(("Move to Processed", "Ready?", "Post to QuickBooks"), ("Move to Error", None, None)),
    ),
    Section(
        "Technical error",
        "Download or processing failed (e.g. OCR service down, core unreachable): logged and reported to the "
        "core. Temporary → the file stays in **Inbox** and is retried when due (15 min, 1 h, 4 h, 12 h). "
        "Permanent, or 5 attempts used → **given up**, moved to **Error**: a person looks.",
        col=9,
        row=5,
        grid=(("Error facts", "Log core error", "Report failure (core)", "Given up?"),),
        kind="failure",
    ),
]


def build(cred_ids: dict[str, str] | None) -> dict[str, Any]:
    nodes = _folder_nodes() + _screen_nodes() + _processing_nodes() + _error_nodes()
    return workflow("AP – Drive intake", lay_out(nodes, SECTIONS), CONNECTIONS, cred_ids)


if __name__ == "__main__":
    main(build)
