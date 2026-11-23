"""Builds the 'AP – Post to QuickBooks' n8n workflow JSON.

Usage:
  python3 n8n/build_post_qbo.py > out.json                       # no credential IDs (committable)
  python3 n8n/build_post_qbo.py --credentials ids.json > out.json  # with local credential IDs, for import

What the workflow does, per invoice claimed from the core service (one at a time):
  vendor (linked → reuse; else find by name: same currency → link, other currency → review, none → create)
  → exchange rate (QuickBooks' own rate on the invoice date, foreign currency only)
  → bill / vendor credit (looked up first by number + vendor, then created with a fixed request ID)
  → original file attached → result reported to the core (which deletes its copy of the file)

The core decides the content (lines, number, names, review rules); this workflow only makes the calls.
Every QuickBooks call is checked; failures are classified (retryable or not) in 'Describe failure'.
QuickBooks calls use HTTP Request nodes with the QuickBooks credential: the native QuickBooks node
can't set a vendor's currency (needed: one vendor per currency) nor upload attachments.
"""

import json
import sys

WORKFLOW_ID = "apPostQbo0000001"
CORE = "http://core:8000"
LOOP = "$('One at a time').first().json"  # the posting being worked on (batch size 1)
BASE = "$('Claim postings (core)').first().json.qbo_base"
MINOR = "$('Claim postings (core)').first().json.minorversion"


# --- node helpers -----------------------------------------------------------------------
def condition(expression: str) -> dict:
    return {
        "options": {"caseSensitive": True, "leftValue": "", "typeValidation": "loose", "version": 2},
        "conditions": [
            {
                "id": "c1",
                "leftValue": "={{ " + expression + " }}",
                "rightValue": "",
                "operator": {"type": "boolean", "operation": "true", "singleValue": True},
            }
        ],
        "combinator": "and",
    }


def if_node(name: str, pos: list[int], expression: str) -> dict:
    return {
        "name": name,
        "type": "n8n-nodes-base.if",
        "typeVersion": 2.2,
        "position": pos,
        "parameters": {"conditions": condition(expression), "options": {}},
    }


def switch_node(name: str, pos: list[int], field: str, outputs: list[str]) -> dict:
    rules = []
    for key in outputs:
        rules.append({"conditions": condition(f"{field} === '{key}'"), "renameOutput": True, "outputKey": key})
    return {
        "name": name,
        "type": "n8n-nodes-base.switch",
        "typeVersion": 3.2,
        "position": pos,
        "parameters": {"rules": {"values": rules}, "options": {}},
    }


def code_node(name: str, pos: list[int], js: str) -> dict:
    return {
        "name": name,
        "type": "n8n-nodes-base.code",
        "typeVersion": 2,
        "position": pos,
        "parameters": {"mode": "runOnceForAllItems", "language": "javaScript", "jsCode": js.strip()},
    }


def set_node(name: str, pos: list[int], fields: dict[str, str]) -> dict:
    return {
        "name": name,
        "type": "n8n-nodes-base.set",
        "typeVersion": 3.4,
        "position": pos,
        "parameters": {
            "mode": "manual",
            "includeOtherFields": False,
            "options": {},
            "assignments": {
                "assignments": [
                    {"id": k, "name": k, "value": "={{ " + v + " }}", "type": "string"} for k, v in fields.items()
                ]
            },
        },
    }


RESPONSE = {"response": {"response": {"fullResponse": True, "neverError": True}}}
# Network errors (DNS, reset, timeout) are retried in place; HTTP error responses are not
# (neverError), they go to the checks. A retried create is safe: same requestid → 425, never a duplicate.
RETRY = {"retryOnFail": True, "maxTries": 3, "waitBetweenTries": 2000}


def qbo(name: str, pos: list[int], method: str, path: str, query: dict | None = None, body: str | None = None) -> dict:
    """QuickBooks call: one item at a time, full response, never throws (the next node checks it)."""
    q = {"minorversion": "={{ " + MINOR + " }}", **(query or {})}
    params = {
        "method": method,
        "url": "={{ " + BASE + " }}" + path,
        "authentication": "predefinedCredentialType",
        "nodeCredentialType": "quickBooksOAuth2Api",
        "sendQuery": True,
        "queryParameters": {"parameters": [{"name": k, "value": v} for k, v in q.items()]},
        "sendHeaders": True,
        "headerParameters": {"parameters": [{"name": "Accept", "value": "application/json"}]},
        "options": {"batching": {"batch": {"batchSize": 1, "batchInterval": 300}}, **RESPONSE},
    }
    if body is not None:
        params.update({"sendBody": True, "specifyBody": "json", "jsonBody": "={{ " + body + " }}"})
    # continueRegularOutput: a network error (DNS, timeout) becomes an item without statusCode,
    # which the next check treats as a failure (status 0 → retryable) instead of stopping the run
    return {
        "name": name,
        "type": "n8n-nodes-base.httpRequest",
        "typeVersion": 4.2,
        "position": pos,
        "onError": "continueRegularOutput",
        **RETRY,
        "parameters": params,
        "credentials": {"quickBooksOAuth2Api": "QBO"},
    }


def core(name: str, pos: list[int], method: str, path: str, body: str | None = None, **extra) -> dict:
    params = {
        "method": method,
        "url": "=" + CORE + path,
        "authentication": "genericCredentialType",
        "genericAuthType": "httpHeaderAuth",
        "options": {**RESPONSE, **extra.pop("options", {})},
    }
    if body is not None:
        params.update({"sendBody": True, "specifyBody": "json", "jsonBody": "={{ " + body + " }}"})
    node = {
        "name": name,
        "type": "n8n-nodes-base.httpRequest",
        "typeVersion": 4.2,
        "position": pos,
        "onError": "continueRegularOutput",
        **RETRY,
        "parameters": params,
        "credentials": {"httpHeaderAuth": "CORE"},
    }
    node.update(extra)
    return node


# --- Code nodes ---------------------------------------------------------------------------
VENDOR_LOOKUP_JS = r"""
// Decide what to do with the vendor search result. Only IDs and currencies go into messages.
const r = $json, item = $('One at a time').first().json;
if (r.statusCode !== 200) return [{ json: { decision: 'error', statusCode: r.statusCode, body: r.body } }];
const found = r.body?.QueryResponse?.Vendor ?? [];
if (found.length === 0) return [{ json: { decision: 'create' } }];
const v = found[0];
const currency = v.CurrencyRef?.value;
if (currency !== item.currency) {
  return [{ json: { decision: 'review', error_code: 'vendor_currency_conflict',
    error_message: `vendor ${v.Id} with this name uses ${currency}; the invoice is in ${item.currency}` } }];
}
if (v.Active === false) {
  return [{ json: { decision: 'review', error_code: 'vendor_inactive', error_message: `vendor ${v.Id} is inactive` } }];
}
return [{ json: { decision: 'use', vendor_id: v.Id } }];
"""

EXISTING_JS = r"""
// Was this document already created (an earlier run that crashed after the call)? Number + vendor.
const r = $json, vendorId = $('Rate').first().json.vendor_id, entity = $('One at a time').first().json.entity;
if (r.statusCode !== 200) return [{ json: { decision: 'error', statusCode: r.statusCode, body: r.body } }];
const docs = (r.body?.QueryResponse?.[entity] ?? []).filter(d => d.VendorRef?.value === vendorId);
if (docs.length > 1) {
  return [{ json: { decision: 'review_dup', error_code: 'duplicate_in_accounting',
    error_message: `${docs.length} ${entity} records with this number for vendor ${vendorId}` } }];
}
return [{ json: docs.length ? { decision: 'exists', external_id: docs[0].Id } : { decision: 'create' } }];
"""

PREPARE_UPLOAD_JS = r"""
// The metadata part must be sent as application/json (as text/plain QuickBooks fails with HTTP 200 + Fault),
// so it is added as a second binary field typed application/json.
const item = $('One at a time').first().json;
let externalId = item.external_id;
if (item.action === 'create') externalId = $('Document ready').first().json.external_id;
const meta = { AttachableRef: [{ EntityRef: { type: item.entity, value: externalId } }],
               FileName: item.file.file_name, ContentType: item.file.mime_type };
const binary = { ...$input.first().binary };
binary.meta = { data: Buffer.from(JSON.stringify(meta)).toString('base64'), mimeType: 'application/json',
                fileName: 'metadata.json', fileExtension: 'json' };
return [{ json: { external_id: externalId }, binary }];
"""

UPLOAD_RESULT_JS = r"""
// Keep only the attachment ID: the response also holds a temporary signed download URL (never stored).
const r = $json;
const att = r.body?.AttachableResponse?.[0];
const id = r.statusCode === 200 && !r.body?.Fault && !att?.Fault ? att?.Attachable?.Id ?? null : null;
const fault = r.body?.Fault?.Error?.[0] ?? att?.Fault?.Error?.[0];
return [{ json: { attachment_id: id, upload_error: id ? null : (fault ? `qbo_${fault.code}` : `http_${r.statusCode}`) } }];
"""

BUILD_RESULT_JS = r"""
// Outcome for the core service. The bill/credit exists at this point; the attachment may not.
const item = $('One at a time').first().json;
const node = $prevNode.name;
const att = node === 'Upload result' ? $json : { attachment_id: null, upload_error: node === 'File OK?' ? 'file_fetch_failed' : null };
let body;
if (item.action === 'create') {
  const rate = $('Rate').first().json;
  body = { outcome: 'posted', external_id: $('Document ready').first().json.external_id,
           vendor_external_id: rate.vendor_id,
           exchange_rate: item.needs_exchange_rate ? rate.exchange_rate : null,
           exchange_rate_date: item.needs_exchange_rate ? item.exchange_rate_date : null,
           attachment_external_id: att.attachment_id };
} else if (att.attachment_id) {
  body = { outcome: 'attached', attachment_external_id: att.attachment_id };
} else {
  body = { outcome: 'failed', error_code: att.upload_error ?? 'attachment_failed', error_message: 'attachment not uploaded',
           retryable: true };
}
return [{ json: { invoice_id: item.invoice_id, body: { ...body, execution_id: $execution.id } } }];
"""

DESCRIBE_FAILURE_JS = r"""
// Turns any failed step into a result for the core: error code, short message, retryable or not.
// Retryable: network errors, 401 (token refresh), 425 (request already sent: the next run finds it),
// 429 and 5xx. Validation errors (other 4xx) are permanent: retrying the same data can't help.
const item = $('One at a time').first().json;
const step = $prevNode.name;
const r = $json;
let code, message, retryable;
if (r.error_code) {                                  // decided upstream (vendor conflict, duplicates)
  code = r.error_code; message = r.error_message; retryable = false;
} else if (step === 'Rate OK?' && r.statusCode === 200) {
  code = 'exchange_rate_unavailable'; message = `no ${item.currency} rate for ${item.exchange_rate_date}`; retryable = true;
} else {
  const status = r.statusCode ?? 0;
  const fault = r.body?.Fault?.Error?.[0];
  code = fault?.code ? `qbo_${fault.code}` : `http_${status}`;
  message = fault ? `${fault.Message ?? ''} ${fault.Detail ?? ''}`.trim() : (r.body?.message ?? r.error?.message ?? '');
  retryable = status === 0 || status === 401 || status === 425 || status === 429 || status >= 500;
}
return [{ json: { invoice_id: item.invoice_id, body: {
  outcome: 'failed', error_code: code, error_message: `${step}: ${message}`.slice(0, 1000), retryable,
  execution_id: $execution.id } } }];
"""


def build(cred_ids: dict | None) -> dict:
    nodes = [
        # --- triggers + claim -----------------------------------------------------------
        {
            "name": "Every 15 minutes",
            "type": "n8n-nodes-base.scheduleTrigger",
            "typeVersion": 1.2,
            "position": [0, 200],
            "parameters": {"rule": {"interval": [{"field": "minutes", "minutesInterval": 15}]}},
        },
        {
            "name": "Called by intake",
            "type": "n8n-nodes-base.executeWorkflowTrigger",
            "typeVersion": 1.1,
            "position": [0, 400],
            "parameters": {"inputSource": "passthrough"},
        },
        {
            "name": "Manual run",
            "type": "n8n-nodes-base.manualTrigger",
            "typeVersion": 1,
            "position": [0, 600],
            "parameters": {},
        },
        {
            "name": "Claim postings (core)",
            "type": "n8n-nodes-base.httpRequest",
            "typeVersion": 4.2,
            "position": [240, 400],
            "executeOnce": True,
            "parameters": {
                "method": "POST",
                "url": f"{CORE}/postings/claim",
                "authentication": "genericCredentialType",
                "genericAuthType": "httpHeaderAuth",
                "sendQuery": True,
                "queryParameters": {"parameters": [{"name": "limit", "value": "10"}]},
                "options": {},
            },
            "credentials": {"httpHeaderAuth": "CORE"},
        },
        {
            "name": "Split postings",
            "type": "n8n-nodes-base.splitOut",
            "typeVersion": 1,
            "position": [460, 400],
            "parameters": {"fieldToSplitOut": "items", "options": {}},
        },
        {
            "name": "One at a time",
            "type": "n8n-nodes-base.splitInBatches",
            "typeVersion": 3,
            "position": [680, 400],
            "parameters": {"batchSize": 1, "options": {}},
        },
        switch_node("Route action", [900, 420], "$json.action", ["create", "attach"]),
        # --- vendor ----------------------------------------------------------------------
        if_node("Vendor linked?", [1120, 200], "!!$json.vendor.external_id"),
        qbo(
            "Find vendor (QBO)",
            [1340, 300],
            "GET",
            "/query",
            query={
                "query": '={{ "select Id, DisplayName, CurrencyRef, Active from Vendor where DisplayName = \'" + '
                + LOOP
                + '.vendor.display_name_q + "\' and Active in (true, false)" }}'
            },
        ),
        code_node("Vendor lookup", [1560, 300], VENDOR_LOOKUP_JS),
        switch_node("Route vendor", [1780, 300], "$json.decision", ["use", "create", "review", "error"]),
        qbo(
            "Create vendor (QBO)",
            [2000, 320],
            "POST",
            "/vendor",
            query={"requestid": "={{ 'ap-vendor-' + " + LOOP + ".posting_id }}"},
            body="JSON.stringify({ DisplayName: "
            + LOOP
            + ".vendor.display_name, CompanyName: "
            + LOOP
            + ".vendor.company_name, CurrencyRef: { value: "
            + LOOP
            + ".currency } })",
        ),
        if_node("Vendor created?", [2220, 320], "$json.statusCode === 200 && !!$json.body?.Vendor?.Id"),
        core(
            "Link vendor (core)",
            [2440, 220],
            "PUT",
            "/vendor-links",
            body="JSON.stringify({ supplier_key: "
            + LOOP
            + ".vendor.supplier_key, currency: "
            + LOOP
            + ".currency, external_id: String($json.vendor_id ?? $json.body.Vendor.Id), display_name: "
            + LOOP
            + ".vendor.display_name })",
        ),
        if_node("Linked?", [2660, 220], "$json.statusCode === 200"),
        set_node(
            "Vendor ready",
            [2880, 120],
            {"vendor_id": "String($json.body?.external_id ?? " + LOOP + ".vendor.external_id)"},
        ),
        # --- exchange rate ---------------------------------------------------------------
        if_node("Needs exchange rate?", [3100, 120], LOOP + ".needs_exchange_rate"),
        qbo(
            "Get exchange rate (QBO)",
            [3320, 20],
            "GET",
            "/exchangerate",
            query={
                "sourcecurrencycode": "={{ " + LOOP + ".currency }}",
                "asofdate": "={{ " + LOOP + ".exchange_rate_date }}",
            },
        ),
        if_node("Rate OK?", [3540, 20], "$json.statusCode === 200 && Number($json.body?.ExchangeRate?.Rate) > 0"),
        set_node(
            "Rate",
            [3760, 120],
            {
                "vendor_id": "$('Vendor ready').first().json.vendor_id",
                "exchange_rate": "$json.body?.ExchangeRate?.Rate ?? ''",
            },
        ),
        # --- bill / vendor credit ------------------------------------------------------
        qbo(
            "Find existing document (QBO)",
            [3980, 120],
            "GET",
            "/query",
            query={
                "query": "={{ 'select Id, VendorRef, TotalAmt from ' + "
                + LOOP
                + '.entity + " where DocNumber = \'" + '
                + LOOP
                + '.doc_number_q + "\'" }}'
            },
        ),
        code_node("Existing?", [4200, 120], EXISTING_JS),
        switch_node("Route document", [4420, 120], "$json.decision", ["create", "exists", "review_dup", "error"]),
        qbo(
            "Create document (QBO)",
            [4640, 40],
            "POST",
            "",  # URL set below (entity endpoint)
            query={"requestid": "={{ " + LOOP + ".request_id }}"},
            body="JSON.stringify(Object.assign({}, "
            + LOOP
            + ".document, { VendorRef: { value: $('Rate').first().json.vendor_id } }, "
            + LOOP
            + ".needs_exchange_rate ? { ExchangeRate: Number($('Rate').first().json.exchange_rate) } : {}))",
        ),
        if_node("Created?", [4860, 40], "$json.statusCode === 200 && !!$json.body?.[" + LOOP + ".entity]?.Id"),
        set_node(
            "Document ready",
            [5080, 120],
            {"external_id": "String($json.external_id ?? $json.body[" + LOOP + ".entity].Id)"},
        ),
        # --- attachment (create path and attach-only path) ------------------------------
        if_node("File available?", [5300, 120], LOOP + ".file.available"),
        core(
            "Get file (core)",
            [5520, 40],
            "GET",
            "",  # URL set below (file path)
            options={
                "response": {
                    "response": {
                        "fullResponse": True,
                        "neverError": True,
                        "responseFormat": "file",
                        "outputPropertyName": "data",
                    }
                }
            },
        ),
        if_node("File OK?", [5740, 40], "$json.statusCode === 200"),
        code_node("Prepare upload", [5960, -40], PREPARE_UPLOAD_JS),
        {
            "name": "Upload attachment (QBO)",
            "type": "n8n-nodes-base.httpRequest",
            "typeVersion": 4.2,
            "position": [6180, -40],
            "parameters": {
                "method": "POST",
                "url": "={{ " + BASE + " }}/upload",
                "authentication": "predefinedCredentialType",
                "nodeCredentialType": "quickBooksOAuth2Api",
                "sendQuery": True,
                "queryParameters": {"parameters": [{"name": "minorversion", "value": "={{ " + MINOR + " }}"}]},
                "sendHeaders": True,
                "headerParameters": {"parameters": [{"name": "Accept", "value": "application/json"}]},
                "sendBody": True,
                "contentType": "multipart-form-data",
                "bodyParameters": {
                    "parameters": [
                        {"parameterType": "formBinaryData", "name": "file_metadata_0", "inputDataFieldName": "meta"},
                        {"parameterType": "formBinaryData", "name": "file_content_0", "inputDataFieldName": "data"},
                    ]
                },
                "options": RESPONSE,
            },
            "onError": "continueRegularOutput",
            **RETRY,
            "credentials": {"quickBooksOAuth2Api": "QBO"},
        },
        code_node("Upload result", [6400, -40], UPLOAD_RESULT_JS),
        code_node("Build result", [6620, 120], BUILD_RESULT_JS),
        # --- failures + report ---------------------------------------------------------
        code_node("Describe failure", [5960, 560], DESCRIBE_FAILURE_JS),
        core(
            "Report result (core)",
            [6840, 400],
            "POST",
            "/postings/{{ $json.invoice_id }}/result",
            body="JSON.stringify($json.body)",
        ),
    ]
    # URLs that depend on the work item
    for n in nodes:
        if n["name"] == "Create document (QBO)":
            n["parameters"]["url"] = "={{ " + BASE + " + '/' + " + LOOP + ".endpoint }}"
        if n["name"] == "Get file (core)":
            n["parameters"]["url"] = "=" + CORE + "{{ " + LOOP + ".file.path }}"

    connections = {
        "Every 15 minutes": [[("Claim postings (core)", 0)]],
        "Called by intake": [[("Claim postings (core)", 0)]],
        "Manual run": [[("Claim postings (core)", 0)]],
        "Claim postings (core)": [[("Split postings", 0)]],
        "Split postings": [[("One at a time", 0)]],
        "One at a time": [[], [("Route action", 0)]],  # 0 = done, 1 = next item
        "Route action": [[("Vendor linked?", 0)], [("File available?", 0)]],
        "Vendor linked?": [[("Vendor ready", 0)], [("Find vendor (QBO)", 0)]],
        "Find vendor (QBO)": [[("Vendor lookup", 0)]],
        "Vendor lookup": [[("Route vendor", 0)]],
        "Route vendor": [
            [("Link vendor (core)", 0)],
            [("Create vendor (QBO)", 0)],
            [("Describe failure", 0)],
            [("Describe failure", 0)],
        ],
        "Create vendor (QBO)": [[("Vendor created?", 0)]],
        "Vendor created?": [[("Link vendor (core)", 0)], [("Describe failure", 0)]],
        "Link vendor (core)": [[("Linked?", 0)]],
        "Linked?": [[("Vendor ready", 0)], [("Describe failure", 0)]],
        "Vendor ready": [[("Needs exchange rate?", 0)]],
        "Needs exchange rate?": [[("Get exchange rate (QBO)", 0)], [("Rate", 0)]],
        "Get exchange rate (QBO)": [[("Rate OK?", 0)]],
        "Rate OK?": [[("Rate", 0)], [("Describe failure", 0)]],
        "Rate": [[("Find existing document (QBO)", 0)]],
        "Find existing document (QBO)": [[("Existing?", 0)]],
        "Existing?": [[("Route document", 0)]],
        "Route document": [
            [("Create document (QBO)", 0)],
            [("Document ready", 0)],
            [("Describe failure", 0)],
            [("Describe failure", 0)],
        ],
        "Create document (QBO)": [[("Created?", 0)]],
        "Created?": [[("Document ready", 0)], [("Describe failure", 0)]],
        "Document ready": [[("File available?", 0)]],
        "File available?": [[("Get file (core)", 0)], [("Build result", 0)]],
        "Get file (core)": [[("File OK?", 0)]],
        "File OK?": [[("Prepare upload", 0)], [("Build result", 0)]],
        "Prepare upload": [[("Upload attachment (QBO)", 0)]],
        "Upload attachment (QBO)": [[("Upload result", 0)]],
        "Upload result": [[("Build result", 0)]],
        "Build result": [[("Report result (core)", 0)]],
        "Describe failure": [[("Report result (core)", 0)]],
        "Report result (core)": [[("One at a time", 0)]],
    }

    names = {"QBO": "QuickBooks – Sandbox US", "CORE": "AP core token"}
    for node in nodes:
        node["id"] = node["name"].lower().replace(" ", "-").replace("(", "").replace(")", "").replace("?", "")
        for ctype, key in node.get("credentials", {}).items():
            ref = {"name": names[key]}
            if cred_ids:
                ref["id"] = cred_ids[key]
            node["credentials"][ctype] = ref
    return {
        "name": "AP – Post to QuickBooks",
        "nodes": nodes,
        "connections": {
            src: {"main": [[{"node": n, "type": "main", "index": i} for n, i in out] for out in outs]}
            for src, outs in connections.items()
        },
        "settings": {
            "executionOrder": "v1",
            "saveDataErrorExecution": "all",
            "saveDataSuccessExecution": "all",
            "saveManualExecutions": True,
            "timezone": "Europe/Paris",
        },
        "pinData": {},
        "active": False,
    }


if __name__ == "__main__":
    ids = None
    if len(sys.argv) == 3 and sys.argv[1] == "--credentials":
        ids = json.load(open(sys.argv[2]))
    print(json.dumps(build(ids), indent=2, ensure_ascii=False, sort_keys=True))
