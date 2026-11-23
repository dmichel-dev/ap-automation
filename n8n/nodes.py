"""Shared building blocks for the n8n workflow builders (build_*.py).

The builders generate the workflow JSON from code, so wiring and layout stay reviewable and
identical between the repo and the running instance. Credentials are referenced by name only;
local IDs are added at import time with --credentials (never committed).
"""

import json
import sys
from collections.abc import Callable
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
