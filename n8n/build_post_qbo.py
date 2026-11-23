"""Builds the 'AP – Post to QuickBooks' n8n workflow JSON.

Usage:
  python3 n8n/build_post_qbo.py > out.json                         # no credential IDs (committable)
  python3 n8n/build_post_qbo.py --credentials ids.json > out.json  # with local credential IDs, for import

What the workflow does, per invoice claimed from the core service (one at a time):
  vendor (linked → reuse; else search by name → the core decides: use / create / review)
  → exchange rate (QuickBooks' own rate on the invoice date, foreign currency only)
  → bill / vendor credit (searched by number first → the core decides: reuse / create / review;
    then created with a fixed request ID)
  → original file attached → result reported to the core (which deletes its copy of the file)

The core decides everything: the content (lines, number, names), what QuickBooks' answers mean,
and what a failure means (retry, stop, review). This workflow makes the calls and passes the
answers through; its Code nodes only reshape data.
QuickBooks calls use HTTP Request nodes with the QuickBooks credential: the native QuickBooks node
can't set a vendor's currency (needed: one vendor per currency) nor upload attachments.
"""

from typing import Any

from nodes import (
    CORE,
    FULL_RESPONSE,
    Connections,
    Node,
    code_node,
    core_call,
    describe_failure_js,
    if_node,
    json_body,
    main,
    qbo_call,
    qbo_send,
    set_node,
    switch_node,
    workflow,
)

WORKFLOW_ID = "apPostQbo0000001"
LOOP = "$('One at a time').first().json"  # the posting being worked on (batch size 1)
BASE = "$('Claim postings (core)').first().json.qbo_base"
MINOR = "$('Claim postings (core)').first().json.minorversion"
POSTING_URL = "=" + CORE + "/postings/{{ " + LOOP + ".invoice_id }}"
# A response passed through to the core as is
PASS_THROUGH = "status_code: $json.statusCode ?? 0, body: $json.body ?? null"


def qbo(name: str, pos: list[int], method: str, url: str, query: dict[str, str] | None = None) -> Node:
    return qbo_call(name, pos, method, url, MINOR, query)


def qbo_url(path: str) -> str:
    return "={{ " + BASE + " }}" + path


# --- Code nodes (reshaping only) ------------------------------------------------------------
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
// Keep only the attachment ID and the failure facts: the response also holds a temporary signed
// download URL (never stored). QuickBooks can report a Fault with HTTP 200.
const r = $json;
const att = r.body?.AttachableResponse?.[0];
const fault = r.body?.Fault?.Error?.[0] ?? att?.Fault?.Error?.[0];
const id = r.statusCode === 200 && !fault ? att?.Attachable?.Id ?? null : null;
return [{ json: { attachment_id: id, http_status: r.statusCode ?? 0, fault_code: fault?.code ?? null } }];
"""

BUILD_RESULT_JS = r"""
// What happened, for the core. The bill/credit exists at this point; the attachment may not.
const item = $('One at a time').first().json;
const node = $prevNode.name;
const att = node === 'Upload result' ? $json : { attachment_id: null, http_status: null, fault_code: null };
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
  body = { outcome: 'failed', error_code: node === 'File OK?' ? 'file_fetch_failed' : 'attachment_failed',
           error_message: 'attachment not uploaded', http_status: att.http_status, fault_code: att.fault_code };
}
return [{ json: { invoice_id: item.invoice_id, body: { ...body, execution_id: $execution.id } } }];
"""

DESCRIBE_FAILURE_JS = describe_failure_js(
    "invoice_id", LOOP + ".invoice_id", {"Rate OK?": "exchange_rate_unavailable"}, {"outcome": "failed"}
)


def _trigger_nodes() -> list[Node]:
    return [
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
        {**core_call("QuickBooks target (core)", [240, 400], "GET", f"{CORE}/accounting/target"), "executeOnce": True},
        # The book close date: the core posts nothing on or before it (and nothing while it is unknown)
        qbo_call(
            "Preferences (QBO)",
            [460, 400],
            "GET",
            "={{ $json.body.qbo_base }}/preferences",
            "$('QuickBooks target (core)').first().json.body.minorversion",
        ),
        {
            "name": "Claim postings (core)",
            "type": "n8n-nodes-base.httpRequest",
            "typeVersion": 4.2,
            "position": [680, 400],
            "executeOnce": True,
            "parameters": {
                "method": "POST",
                "url": f"{CORE}/postings/claim",
                "authentication": "genericCredentialType",
                "genericAuthType": "httpHeaderAuth",
                "sendQuery": True,
                "queryParameters": {"parameters": [{"name": "limit", "value": "10"}]},
                **json_body(
                    "JSON.stringify({ preferences: { status_code: $json.statusCode ?? 0, body: $json.body ?? null } })"
                ),
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
    ]


def _vendor_nodes() -> list[Node]:
    return [
        if_node("Vendor linked?", [1120, 200], "!!$json.vendor.external_id"),
        qbo(
            "Find vendor (QBO)",
            [1340, 300],
            "GET",
            qbo_url("/query"),
            query={
                "query": '={{ "select Id, DisplayName, CurrencyRef, Active from Vendor where DisplayName = \'" + '
                + LOOP
                + '.vendor.display_name_q + "\' and Active in (true, false)" }}'
            },
        ),
        core_call(
            "Vendor decision (core)",
            [1560, 300],
            "POST",
            POSTING_URL + "/lookup/vendor",
            body=f"JSON.stringify({{ {PASS_THROUGH} }})",
        ),
        switch_node(
            "Route vendor", [1780, 300], "$json.body?.decision", ["use", "create", "review", "error"], "core error"
        ),
        qbo_send(
            qbo(
                "Create vendor (QBO)",
                [2000, 320],
                "POST",
                qbo_url("/vendor"),
                query={"requestid": "={{ 'ap-vendor-' + " + LOOP + ".posting_id }}"},
            ),
            f"JSON.stringify({{ DisplayName: {LOOP}.vendor.display_name, CompanyName: {LOOP}.vendor.company_name, "
            f"CurrencyRef: {{ value: {LOOP}.currency }} }})",
        ),
        if_node("Vendor created?", [2220, 320], "$json.statusCode === 200 && !!$json.body?.Vendor?.Id"),
        core_call(
            "Link vendor (core)",
            [2440, 220],
            "PUT",
            "=" + CORE + "/vendor-links",
            body=f"JSON.stringify({{ supplier_key: {LOOP}.vendor.supplier_key, currency: {LOOP}.currency, "
            f"external_id: String($json.body.external_id ?? $json.body.Vendor.Id), "
            f"display_name: {LOOP}.vendor.display_name }})",
        ),
        if_node("Linked?", [2660, 220], "$json.statusCode === 200"),
        set_node(
            "Vendor ready", [2880, 120], {"vendor_id": f"String($json.body?.external_id ?? {LOOP}.vendor.external_id)"}
        ),
    ]


def _rate_nodes() -> list[Node]:
    return [
        if_node("Needs exchange rate?", [3100, 120], LOOP + ".needs_exchange_rate"),
        qbo(
            "Get exchange rate (QBO)",
            [3320, 20],
            "GET",
            qbo_url("/exchangerate"),
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
    ]


def _document_nodes() -> list[Node]:
    return [
        qbo(
            "Find existing document (QBO)",
            [3980, 120],
            "GET",
            qbo_url("/query"),
            query={
                "query": "={{ 'select Id, VendorRef, TotalAmt from ' + "
                + LOOP
                + '.entity + " where DocNumber = \'" + '
                + LOOP
                + '.doc_number_q + "\'" }}'
            },
        ),
        core_call(
            "Document decision (core)",
            [4200, 120],
            "POST",
            POSTING_URL + "/lookup/document",
            body=f"JSON.stringify({{ {PASS_THROUGH}, vendor_id: $('Rate').first().json.vendor_id }})",
        ),
        switch_node(
            "Route document", [4420, 120], "$json.body?.decision", ["create", "exists", "review", "error"], "core error"
        ),
        qbo_send(
            qbo(
                "Create document (QBO)",
                [4640, 40],
                "POST",
                "={{ " + BASE + " + '/' + " + LOOP + ".endpoint }}",
                query={"requestid": "={{ " + LOOP + ".request_id }}"},
            ),
            f"JSON.stringify(Object.assign({{}}, {LOOP}.document, "
            "{ VendorRef: { value: $('Rate').first().json.vendor_id } }, "
            f"{LOOP}.needs_exchange_rate ? {{ ExchangeRate: Number($('Rate').first().json.exchange_rate) }} : {{}}))",
        ),
        if_node("Created?", [4860, 40], "$json.statusCode === 200 && !!$json.body?.[" + LOOP + ".entity]?.Id"),
        set_node(
            "Document ready",
            [5080, 120],
            {"external_id": "String($json.body.external_id ?? $json.body[" + LOOP + ".entity].Id)"},
        ),
    ]


def _attachment_nodes() -> list[Node]:
    file_response = {
        "response": {
            "response": {
                "fullResponse": True,
                "neverError": True,
                "responseFormat": "file",
                "outputPropertyName": "data",
            }
        }
    }
    upload = qbo("Upload attachment (QBO)", [6180, -40], "POST", qbo_url("/upload"))
    upload["parameters"]["options"] = FULL_RESPONSE  # one file per call already: no batching needed
    upload["parameters"].update(
        {
            "sendBody": True,
            "contentType": "multipart-form-data",
            "bodyParameters": {
                "parameters": [
                    {"parameterType": "formBinaryData", "name": "file_metadata_0", "inputDataFieldName": "meta"},
                    {"parameterType": "formBinaryData", "name": "file_content_0", "inputDataFieldName": "data"},
                ]
            },
        }
    )
    return [
        if_node("File available?", [5300, 120], LOOP + ".file.available"),
        core_call(
            "Get file (core)", [5520, 40], "GET", "=" + CORE + "{{ " + LOOP + ".file.path }}", options=file_response
        ),
        if_node("File OK?", [5740, 40], "$json.statusCode === 200"),
        code_node("Prepare upload", [5960, -40], PREPARE_UPLOAD_JS),
        upload,
        code_node("Upload result", [6400, -40], UPLOAD_RESULT_JS),
        code_node("Build result", [6620, 120], BUILD_RESULT_JS),
    ]


def _report_nodes() -> list[Node]:
    return [
        code_node("Describe failure", [5960, 560], DESCRIBE_FAILURE_JS),
        core_call(
            "Report result (core)",
            [6840, 400],
            "POST",
            "=" + CORE + "/postings/{{ $json.invoice_id }}/result",
            body="JSON.stringify($json.body)",
        ),
    ]


FAIL = [("Describe failure", 0)]
CONNECTIONS: Connections = {
    "Every 15 minutes": [[("QuickBooks target (core)", 0)]],
    "Called by intake": [[("QuickBooks target (core)", 0)]],
    "Manual run": [[("QuickBooks target (core)", 0)]],
    "QuickBooks target (core)": [[("Preferences (QBO)", 0)]],
    "Preferences (QBO)": [[("Claim postings (core)", 0)]],
    "Claim postings (core)": [[("Split postings", 0)]],
    "Split postings": [[("One at a time", 0)]],
    "One at a time": [[], [("Route action", 0)]],  # 0 = done, 1 = next item
    "Route action": [[("Vendor linked?", 0)], [("File available?", 0)]],
    "Vendor linked?": [[("Vendor ready", 0)], [("Find vendor (QBO)", 0)]],
    "Find vendor (QBO)": [[("Vendor decision (core)", 0)]],
    "Vendor decision (core)": [[("Route vendor", 0)]],
    "Route vendor": [[("Link vendor (core)", 0)], [("Create vendor (QBO)", 0)], FAIL, FAIL, FAIL],
    "Create vendor (QBO)": [[("Vendor created?", 0)]],
    "Vendor created?": [[("Link vendor (core)", 0)], FAIL],
    "Link vendor (core)": [[("Linked?", 0)]],
    "Linked?": [[("Vendor ready", 0)], FAIL],
    "Vendor ready": [[("Needs exchange rate?", 0)]],
    "Needs exchange rate?": [[("Get exchange rate (QBO)", 0)], [("Rate", 0)]],
    "Get exchange rate (QBO)": [[("Rate OK?", 0)]],
    "Rate OK?": [[("Rate", 0)], FAIL],
    "Rate": [[("Find existing document (QBO)", 0)]],
    "Find existing document (QBO)": [[("Document decision (core)", 0)]],
    "Document decision (core)": [[("Route document", 0)]],
    "Route document": [[("Create document (QBO)", 0)], [("Document ready", 0)], FAIL, FAIL, FAIL],
    "Create document (QBO)": [[("Created?", 0)]],
    "Created?": [[("Document ready", 0)], FAIL],
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


def build(cred_ids: dict[str, str] | None) -> dict[str, Any]:
    nodes = (
        _trigger_nodes() + _vendor_nodes() + _rate_nodes() + _document_nodes() + _attachment_nodes() + _report_nodes()
    )
    for node in nodes:  # room for the preferences nodes before the claim
        if node["position"][0] >= 460 and node["name"] not in ("Preferences (QBO)", "Claim postings (core)"):
            node["position"] = [node["position"][0] + 440, node["position"][1]]
    return workflow("AP – Post to QuickBooks", nodes, CONNECTIONS, cred_ids)


if __name__ == "__main__":
    main(build)
