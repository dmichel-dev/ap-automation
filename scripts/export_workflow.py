"""Cleans an n8n workflow export so it can be committed, then scans it for secrets.

Usage (normally via scripts/export_workflow.sh):
  python3 scripts/export_workflow.py RAW_EXPORT.json OUTPUT.json

What is removed: credential IDs (names are kept so an import can re-link them), instance
fields (versionId, meta, timestamps, owner/shared, tags IDs, triggerCount), pinned data
and static data (the Gmail trigger's last poll time). The workflow is saved inactive.

The secret scan refuses to write the file if it finds anything that looks like a token,
key, password or an email address other than the project's own domain.
"""

import json
import re
import sys
from pathlib import Path

KEEP_TOP = ("name", "nodes", "connections", "settings")
KEEP_NODE = (
    "parameters",
    "id",
    "name",
    "type",
    "typeVersion",
    "position",
    "credentials",
    "alwaysOutputData",
    "executeOnce",
    "onError",
    "retryOnFail",
    "maxTries",
    "waitBetweenTries",
    "notes",
    "notesInFlow",
    "disabled",
)
ALLOWED_EMAIL_DOMAINS = {"davidmichel.dev", "example.com"}
SECRET_PATTERNS = {
    "bearer token": re.compile(r"Bearer\s+[A-Za-z0-9._\-]{16,}"),
    "api key": re.compile(r"\b(sk|pk|rk)[-_][A-Za-z0-9]{16,}"),
    "google oauth token": re.compile(r"\bya29\.[A-Za-z0-9._\-]+|\b1//[A-Za-z0-9._\-]{20,}"),
    "google client secret": re.compile(r"GOCSPX-[A-Za-z0-9_\-]+"),
    "private key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "secret-looking field": re.compile(
        r'"(password|accessToken|refreshToken|clientSecret|apiKey|token|secret)"\s*:\s*"[^"={]', re.I
    ),
    "connection string with password": re.compile(r"postgres(?:ql)?://[^:\s/]+:[^@\s]+@"),
}
EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@([A-Za-z0-9.\-]+\.[A-Za-z]{2,})")


def clean(raw) -> dict:
    wf = raw[0] if isinstance(raw, list) else raw
    out = {k: wf[k] for k in KEEP_TOP if k in wf}
    nodes = []
    for node in wf["nodes"]:
        n = {k: node[k] for k in KEEP_NODE if k in node}
        if "credentials" in n:
            n["credentials"] = {kind: {"name": c["name"]} for kind, c in n["credentials"].items()}
        nodes.append(n)
    out["nodes"] = nodes
    out["pinData"] = {}
    out["active"] = False
    return out


def scan(text: str) -> list[str]:
    problems = [f"{label}: {m.group(0)[:40]}…" for label, rx in SECRET_PATTERNS.items() for m in rx.finditer(text)]
    problems += [
        f"email outside the project domain: {m.group(0)}"
        for m in EMAIL.finditer(text)
        if m.group(1).lower() not in ALLOWED_EMAIL_DOMAINS
    ]
    if re.search(r'"credentials"\s*:\s*\{[^}]*"id"', text):
        problems.append("credential id still present")
    return problems


def main() -> int:
    src, dst = Path(sys.argv[1]), Path(sys.argv[2])
    text = json.dumps(clean(json.loads(src.read_text())), indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    problems = scan(text)
    if problems:
        print("REFUSED: possible secrets in the export:", *problems, sep="\n  - ", file=sys.stderr)
        return 2
    dst.write_text(text)
    print(f"wrote {dst} ({len(text)} bytes), secret scan clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
