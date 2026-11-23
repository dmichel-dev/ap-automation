"""Shared building blocks for the n8n workflow builders (build_*.py).

The builders generate the workflow JSON from code, so wiring and layout stay reviewable and
identical between the repo and the running instance. Credentials are referenced by name only;
local IDs are added at import time with --credentials (never committed).
"""

import json
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

Node = dict[str, Any]
# source node → list of outputs → list of (target node, target input)
Connections = dict[str, list[list[tuple[str, int]]]]

CORE = "http://core:8000"
CREDENTIAL_NAMES = {
    "GMAIL": "Gmail – david@davidmichel.dev",
    "CORE": "AP core token",
    "POSTGRES": "AP database (ap_app)",
    "QBO": "QuickBooks – Sandbox US",
    "PLAID": "Plaid – Sandbox",
}
# Full response, never throw on an HTTP error status: the next node checks the status itself
FULL_RESPONSE = {"response": {"response": {"fullResponse": True, "neverError": True}}}
# Network errors (DNS, reset, timeout) are retried in place; HTTP error responses are not
# (see FULL_RESPONSE). A retried create is safe: same requestid → 425, never a duplicate.
NETWORK_RETRY = {"retryOnFail": True, "maxTries": 3, "waitBetweenTries": 2000}


def condition(expression: str, type_validation: str = "loose", condition_id: str = "c1") -> dict[str, Any]:
    """An If/Switch condition that is true when the boolean expression is true."""
    return {
        "options": {"caseSensitive": True, "leftValue": "", "typeValidation": type_validation, "version": 2},
        "conditions": [
            {
                "id": condition_id,
                "leftValue": "={{ " + expression + " }}",
                "rightValue": "",
                "operator": {"type": "boolean", "operation": "true", "singleValue": True},
            }
        ],
        "combinator": "and",
    }


def if_node(name: str, pos: list[int], expression: str) -> Node:
    return {
        "name": name,
        "type": "n8n-nodes-base.if",
        "typeVersion": 2.2,
        "position": pos,
        "parameters": {"conditions": condition(expression), "options": {}},
    }


def switch_node(name: str, pos: list[int], field: str, outputs: list[str], fallback: str | None = None) -> Node:
    """One output per value of `field`; with `fallback`, an extra output for anything else."""
    rules = [
        {"conditions": condition(f"{field} === '{key}'"), "renameOutput": True, "outputKey": key} for key in outputs
    ]
    options = {"fallbackOutput": "extra", "renameFallbackOutput": fallback} if fallback else {}
    return {
        "name": name,
        "type": "n8n-nodes-base.switch",
        "typeVersion": 3.2,
        "position": pos,
        "parameters": {"rules": {"values": rules}, "options": options},
    }


def code_node(name: str, pos: list[int], js: str) -> Node:
    return {
        "name": name,
        "type": "n8n-nodes-base.code",
        "typeVersion": 2,
        "position": pos,
        "parameters": {"mode": "runOnceForAllItems", "language": "javaScript", "jsCode": js.strip()},
    }


def set_node(name: str, pos: list[int], fields: dict[str, str]) -> Node:
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


def json_body(expression: str) -> dict[str, Any]:
    return {"sendBody": True, "specifyBody": "json", "jsonBody": "={{ " + expression + " }}"}


def core_call(
    name: str, pos: list[int], method: str, url: str, body: str | None = None, options: dict[str, Any] | None = None
) -> Node:
    """Call to the core service (bearer token). Full response, never throws; network errors retried."""
    params: dict[str, Any] = {
        "method": method,
        "url": url,
        "authentication": "genericCredentialType",
        "genericAuthType": "httpHeaderAuth",
        "options": {**FULL_RESPONSE, **(options or {})},
    }
    if body is not None:
        params.update(json_body(body))
    return {
        "name": name,
        "type": "n8n-nodes-base.httpRequest",
        "typeVersion": 4.2,
        "position": pos,
        "onError": "continueRegularOutput",
        **NETWORK_RETRY,
        "parameters": params,
        "credentials": {"httpHeaderAuth": "CORE"},
    }


def describe_failure_js(
    id_field: str, id_expr: str, named: dict[str, str] | None = None, extra: dict[str, str] | None = None
) -> str:
    """Code node: facts about the step that failed (HTTP status, QuickBooks Fault code, message), for
    the core to classify (retry later, stop, review). `named`: steps that answer 200 without the
    expected data, and the failure name to report for them. `extra`: constant fields added to the body."""
    js = r"""
// Facts about the step that failed. The core decides what they mean (retry later, stop, review).
const NAMED = __NAMED__;   // steps that answered 200 without the expected data
const step = $prevNode.name;
const r = $json;
const b = r.body ?? {};
let facts;
if (b.decision) {          // the core's answer to a lookup: review, or QuickBooks query error
  facts = { error_code: b.error_code, error_message: b.error_message, http_status: b.http_status,
            fault_code: b.fault_code };
} else {                   // a QuickBooks or core call that failed
  const fault = b.Fault?.Error?.[0];
  facts = { http_status: r.statusCode ?? 0, fault_code: fault?.code ?? null,
            error_message: fault ? `${fault.Message ?? ''} ${fault.Detail ?? ''}`.trim()
                                 : String(b.message ?? b.detail ?? r.error?.message ?? '') };
  if (NAMED[step] && r.statusCode === 200) facts.error_code = NAMED[step];
}
return [{ json: { __ID_FIELD__: __ID_EXPR__, body: {
  ...__EXTRA__, ...facts, error_message: `${step}: ${facts.error_message ?? ''}`.slice(0, 1000),
  execution_id: $execution.id } } }];
"""
    return (
        js.replace("__NAMED__", json.dumps(named or {}))
        .replace("__EXTRA__", json.dumps(extra or {}))
        .replace("__ID_FIELD__", id_field)
        .replace("__ID_EXPR__", id_expr)
    )


def qbo_call(
    name: str, pos: list[int], method: str, url: str, minor_expr: str, query: dict[str, str] | None = None
) -> Node:
    """QuickBooks call: one item at a time, full response, never throws (the next node checks it).
    Network errors are retried in place, then become an item without status code."""
    q = {"minorversion": "={{ " + minor_expr + " }}", **(query or {})}
    return {
        "name": name,
        "type": "n8n-nodes-base.httpRequest",
        "typeVersion": 4.2,
        "position": pos,
        "onError": "continueRegularOutput",
        **NETWORK_RETRY,
        "parameters": {
            "method": method,
            "url": url,
            "authentication": "predefinedCredentialType",
            "nodeCredentialType": "quickBooksOAuth2Api",
            "sendQuery": True,
            "queryParameters": {"parameters": [{"name": k, "value": v} for k, v in q.items()]},
            "sendHeaders": True,
            "headerParameters": {"parameters": [{"name": "Accept", "value": "application/json"}]},
            "options": {"batching": {"batch": {"batchSize": 1, "batchInterval": 300}}, **FULL_RESPONSE},
        },
        "credentials": {"quickBooksOAuth2Api": "QBO"},
    }


def qbo_send(node: Node, body: str) -> Node:
    node["parameters"].update(json_body(body))
    return node


def stop(name: str, pos: list[int], message: str) -> Node:
    """Ends the run as failed (visible in the executions list)."""
    return {
        "name": name,
        "type": "n8n-nodes-base.stopAndError",
        "typeVersion": 1,
        "position": pos,
        "parameters": {"errorMessage": "={{ " + message + " }}"},
    }


# =====================================================================================
# Canvas layout: nodes on a grid, one sticky note per section, sized around its nodes
# =====================================================================================
AUTO = [0, 0]  # placeholder position in the builders: lay_out() sets every node's position
COL, ROW = 240, 200  # grid step (px)
NODE, LABEL = 100, 60  # node box, and room for its name under it
PAD, HEADER = 40, 150  # sticky padding, and room for the note's title and text above the nodes
COLORS = {"overview": 5, "section": 7, "failure": 3}  # n8n sticky colours: blue, grey, red


@dataclass(frozen=True)
class Section:
    """A part of the workflow: its nodes on a grid (rows of names, None = empty cell), placed with
    its top-left node at grid cell (col, row), and a note explaining it."""

    title: str
    text: str
    col: int
    row: int
    grid: tuple[tuple[str | None, ...], ...] = ()
    kind: str = "section"  # section | failure | overview
    min_cols: int = 2  # notes narrower than this can't hold their text
    rows: int = 1  # height of a note without nodes (overview)

    def size(self) -> tuple[int, int]:
        cols = max(self.min_cols, *(len(r) for r in self.grid)) if self.grid else self.min_cols
        return cols, len(self.grid) or self.rows


def sticky(name: str, content: str, box: tuple[int, int, int, int], color: int) -> Node:
    x, y, width, height = box
    return {
        "name": name,
        "type": "n8n-nodes-base.stickyNote",
        "typeVersion": 1,
        "position": [x, y],
        "parameters": {"content": content, "width": width, "height": height, "color": color},
    }


def section_box(s: Section) -> tuple[int, int, int, int]:
    cols, rows = s.size()
    x, y = s.col * COL - PAD, s.row * ROW - HEADER
    return x, y, (cols - 1) * COL + NODE + 2 * PAD, HEADER + (rows - 1) * ROW + NODE + LABEL + PAD


def lay_out(nodes: list[Node], sections: list[Section]) -> list[Node]:
    """Positions every node from its section's grid and adds the sections' notes.
    Each node must be in exactly one section (a node left out would sit at a stale position)."""
    by_name = {n["name"]: n for n in nodes}
    placed = [name for s in sections for row in s.grid for name in row if name]
    if sorted(placed) != sorted(by_name):
        missing, twice = set(by_name) - set(placed), {n for n in placed if placed.count(n) > 1}
        raise ValueError(
            f"layout: not placed {sorted(missing)}, placed twice {sorted(twice)}, unknown "
            f"{sorted(set(placed) - set(by_name))}"
        )
    notes = []
    for s in sections:
        for r, row in enumerate(s.grid):
            for c, name in enumerate(row):
                if name:
                    by_name[name]["position"] = [(s.col + c) * COL, (s.row + r) * ROW]
        notes.append(sticky(f"Note: {s.title}", f"## {s.title}\n{s.text}", section_box(s), COLORS[s.kind]))
    return notes + nodes


def node_id(name: str) -> str:
    return name.lower().replace(" ", "-").translate(str.maketrans("", "", ":()?"))


def workflow(name: str, nodes: list[Node], connections: Connections, cred_ids: dict[str, str] | None) -> dict[str, Any]:
    """Final workflow JSON: node IDs from names, credential references, rendered connections."""
    for node in nodes:
        node["id"] = node_id(node["name"])
        for ctype, key in node.get("credentials", {}).items():
            ref = {"name": CREDENTIAL_NAMES[key]}
            if cred_ids:
                ref["id"] = cred_ids[key]
            node["credentials"][ctype] = ref
    return {
        "name": name,
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


def main(build: Callable[[dict[str, str] | None], dict[str, Any]]) -> None:
    """CLI: `build_x.py` (no credential IDs, committable) or `build_x.py --credentials ids.json` (for import)."""
    ids = None
    if len(sys.argv) == 3 and sys.argv[1] == "--credentials":
        with open(sys.argv[2]) as f:
            ids = json.load(f)
    print(json.dumps(build(ids), indent=2, ensure_ascii=False, sort_keys=True))
