"""Sends QuickBooks API requests through n8n's QuickBooks credential, so OAuth tokens never leave n8n.

A temporary workflow (manual trigger → requests → HTTP Request node with the credential) is imported,
run once with `n8n execute`, then deleted together with its execution (which held the responses).

Only reads and creates are allowed: a request may be a GET, or a POST whose body has no "Id" (QuickBooks
updates an entity only when the body carries its Id), so a script built on this can't change or
delete existing data.

Needs the environment from .env (QBO_REALM_ID, QBO_API_HOST, N8N_DB_USER) and the running stack.
"""

import json
import os
import secrets
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
TEMP_PREFIX = "apQboTmp"  # + 8 random hex characters: a fresh workflow per call (n8n IDs have 16)
CREDENTIAL_TYPE = "quickBooksOAuth2Api"
MINOR_VERSION = "75"


@dataclass(frozen=True)
class Request:
    method: str  # GET or POST
    path: str  # after /v3/company/{realm}, e.g. /query or /account
    query: dict[str, str] = field(default_factory=dict)
    body: dict[str, Any] | None = None


@dataclass(frozen=True)
class Response:
    status: int
    body: Any


def _check(r: Request) -> None:
    if r.method == "GET" and r.body is None:
        return
    if r.method == "POST" and r.body is not None and "Id" not in r.body:
        return
    raise ValueError(f"refused: only reads and creates are allowed ({r.method} {r.path})")


def _compose(*args: str, stdin: str | None = None) -> str:
    done = subprocess.run(["docker", "compose", *args], input=stdin, capture_output=True, text=True, cwd=ROOT)
    if done.returncode != 0:
        detail = (done.stderr.strip() or done.stdout.strip())[-800:]  # n8n execute reports on stdout
        raise RuntimeError(f"docker compose {' '.join(args[:6])} failed (exit {done.returncode}): {detail}")
    return done.stdout


def _n8n_sql(sql: str) -> str:
    return _compose("exec", "-T", "postgres", "psql", "-U", os.environ["N8N_DB_USER"], "-d", "n8n", "-Atc", sql)


def _credential() -> dict[str, str]:
    sql = f"SELECT id || '|' || name FROM credentials_entity WHERE type = '{CREDENTIAL_TYPE}'"
    rows = [r for r in _n8n_sql(sql).splitlines() if r]
    if len(rows) != 1:
        raise RuntimeError(f"expected exactly one QuickBooks credential in n8n, found {len(rows)}")
    cred_id, name = rows[0].split("|", 1)
    return {"id": cred_id, "name": name}


def _url(r: Request) -> str:
    base = f"{os.environ.get('QBO_API_HOST', 'https://sandbox-quickbooks.api.intuit.com')}/v3/company/"
    query = {**r.query, "minorversion": MINOR_VERSION}
    return base + os.environ["QBO_REALM_ID"] + r.path + "?" + "&".join(f"{k}={quote(v)}" for k, v in query.items())


def _workflow(workflow_id: str, requests: list[Request]) -> dict[str, Any]:
    items = [{"json": {"method": r.method, "url": _url(r), "body": json.dumps(r.body or {})}} for r in requests]
    http = {
        "id": "qbo",
        "name": "QuickBooks",
        "type": "n8n-nodes-base.httpRequest",
        "typeVersion": 4.2,
        "position": [440, 0],
        "credentials": {CREDENTIAL_TYPE: _credential()},
        "parameters": {
            "method": "={{ $json.method }}",
            "url": "={{ $json.url }}",
            "authentication": "predefinedCredentialType",
            "nodeCredentialType": CREDENTIAL_TYPE,
            "sendHeaders": True,
            "headerParameters": {"parameters": [{"name": "Accept", "value": "application/json"}]},
            "sendBody": True,
            "specifyBody": "json",
            "jsonBody": "={{ $json.body }}",
            "options": {
                "batching": {"batch": {"batchSize": 1, "batchInterval": 400}},
                "response": {"response": {"fullResponse": True, "neverError": True}},
            },
        },
    }
    return {
        "id": workflow_id,
        "name": "TEMP – QuickBooks admin task (deleted after the run)",
        "active": False,
        "settings": {},
        "nodes": [
            {
                "id": "start",
                "name": "Start",
                "type": "n8n-nodes-base.manualTrigger",
                "typeVersion": 1,
                "position": [0, 0],
                "parameters": {},
            },
            {
                "id": "reqs",
                "name": "Requests",
                "type": "n8n-nodes-base.code",
                "typeVersion": 2,
                "position": [220, 0],
                "parameters": {"jsCode": "return " + json.dumps(items) + ";"},
            },
            http,
        ],
        "connections": {
            "Start": {"main": [[{"node": "Requests", "type": "main", "index": 0}]]},
            "Requests": {"main": [[{"node": "QuickBooks", "type": "main", "index": 0}]]},
        },
    }


def delete_workflow(workflow_id: str) -> None:
    """Removes a temporary, never-published workflow (its executions go with it)."""
    _n8n_sql(f"DELETE FROM workflow_entity WHERE id = '{workflow_id}' AND NOT active")


def send(requests: list[Request]) -> list[Response]:
    """Runs the requests in order (one at a time) and returns one response each."""
    for r in requests:
        _check(r)
    # A new ID each time: re-importing an ID that was just deleted races with n8n's own bookkeeping
    workflow_id = TEMP_PREFIX + secrets.token_hex(4)
    wf = json.dumps(_workflow(workflow_id, requests))
    try:
        load = "cat > /tmp/qbo_task.json && n8n import:workflow --input=/tmp/qbo_task.json >/dev/null 2>&1"
        _compose("exec", "-T", "n8n", "sh", "-c", load + "; rm -f /tmp/qbo_task.json", stdin=wf)
        runner = ("exec", "-T", "-e", "N8N_RUNNERS_BROKER_PORT=5690", "n8n")
        out = _compose(*runner, "n8n", "execute", f"--id={workflow_id}", "--rawOutput")
    finally:
        delete_workflow(workflow_id)
    data, _ = json.JSONDecoder().raw_decode(out[out.index("{") :])
    run = data["data"]["resultData"]["runData"]["QuickBooks"][0]
    if run.get("error"):
        raise RuntimeError(f"QuickBooks node failed: {json.dumps(run['error'])[:500]}")
    items = run["data"]["main"][0]
    return [Response(i["json"].get("statusCode", 0), i["json"].get("body")) for i in items]
