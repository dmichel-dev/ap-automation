"""Builds the 'AP – Sync QuickBooks reference data' n8n workflow JSON.

Usage:
  python3 n8n/build_sync_reference.py > out.json                         # no credential IDs (committable)
  python3 n8n/build_sync_reference.py --credentials ids.json > out.json  # with local credential IDs, for import

What the workflow does (every hour, or by hand):
  1. Reads the chart of accounts from QuickBooks and passes the answer to the core, which stores
     its copy (the review page's account picker reads it).
  2. Same for the vendor list (an existing QuickBooks vendor counts as a known supplier).
The core checks each answer and refuses an unusable one (error, empty, more than one page), so the
stored copy is never replaced by a partial list. A refused or failed step ends the run as failed.

QuickBooks tokens stay in n8n: this is how the core gets the data without them.
"""

from typing import Any

from nodes import AUTO, CORE, Connections, Node, Section, core_call, if_node, lay_out, main, qbo_call, stop, workflow

TARGET = "$('QuickBooks target (core)').first().json.body"
PASS_THROUGH = "JSON.stringify({ status_code: $json.statusCode ?? 0, body: $json.body ?? null })"
QUERIES = {  # what is read, and where the core stores it
    "accounts": "select * from Account maxresults 1000",
    "vendors": "select * from Vendor maxresults 1000",
}


def _trigger_nodes() -> list[Node]:
    return [
        {
            "name": "Every hour",
            "type": "n8n-nodes-base.scheduleTrigger",
            "typeVersion": 1.2,
            "position": AUTO,
            "parameters": {"rule": {"interval": [{"field": "hours", "hoursInterval": 1}]}},
        },
        {
            "name": "Manual run",
            "type": "n8n-nodes-base.manualTrigger",
            "typeVersion": 1,
            "position": AUTO,
            "parameters": {},
        },
        {**core_call("QuickBooks target (core)", AUTO, "GET", f"{CORE}/accounting/target"), "executeOnce": True},
    ]


def _sync_nodes(entity: str) -> list[Node]:
    """Read from QuickBooks → store in the core → stop the run if the core refused it."""
    label = entity.capitalize()
    return [
        qbo_call(
            f"{label} (QBO)",
            AUTO,
            "GET",
            "={{ " + TARGET + ".qbo_base }}/query",
            TARGET + ".minorversion",
            {"query": QUERIES[entity]},
        ),
        core_call(f"Store {entity} (core)", AUTO, "POST", f"{CORE}/reference/{entity}", PASS_THROUGH),
        if_node(f"{label} stored?", AUTO, "$json.statusCode === 200"),
        stop(
            f"Stop: {entity} not synced",
            AUTO,
            f"'{entity} not synced: HTTP ' + ($json.statusCode ?? 0) + ' ' + ($json.body?.message ?? '')",
        ),
    ]


CONNECTIONS: Connections = {
    "Every hour": [[("QuickBooks target (core)", 0)]],
    "Manual run": [[("QuickBooks target (core)", 0)]],
    "QuickBooks target (core)": [[("Accounts (QBO)", 0)]],
    "Accounts (QBO)": [[("Store accounts (core)", 0)]],
    "Store accounts (core)": [[("Accounts stored?", 0)]],
    "Accounts stored?": [[("Vendors (QBO)", 0)], [("Stop: accounts not synced", 0)]],
    "Vendors (QBO)": [[("Store vendors (core)", 0)]],
    "Store vendors (core)": [[("Vendors stored?", 0)]],
    "Vendors stored?": [[], [("Stop: vendors not synced", 0)]],
}


SECTIONS = [
    Section(
        "Sync QuickBooks reference data",
        "Keeps a copy of the QuickBooks **chart of accounts** and **vendor list** in the AP database.\n\n"
        "- The review page's account picker reads the accounts.\n"
        "- The vendors tell the core which suppliers are already known.\n\n"
        "QuickBooks tokens stay in n8n. The core checks every answer and keeps its previous copy "
        "if an answer is unusable.",
        col=0,
        row=0,
        kind="overview",
        min_cols=6,
    ),
    Section(
        "Start",
        "Every hour, or by hand after changing accounts in QuickBooks. The core says which company to call.",
        col=0,
        row=2,
        grid=(("Every hour", "QuickBooks target (core)"), ("Manual run", None)),
    ),
    Section(
        "Chart of accounts",
        "Read all accounts, then store them in the core. A refused answer stops the run.",
        col=2,
        row=2,
        grid=(
            ("Accounts (QBO)", "Store accounts (core)", "Accounts stored?"),
            (None, None, "Stop: accounts not synced"),
        ),
    ),
    Section(
        "Vendor list",
        "Read all vendors, then store them in the core. A refused answer stops the run.",
        col=5,
        row=2,
        grid=(("Vendors (QBO)", "Store vendors (core)", "Vendors stored?"), (None, None, "Stop: vendors not synced")),
    ),
]


def build(cred_ids: dict[str, str] | None) -> dict[str, Any]:
    nodes = lay_out(_trigger_nodes() + _sync_nodes("accounts") + _sync_nodes("vendors"), SECTIONS)
    return workflow("AP – Sync QuickBooks reference data", nodes, CONNECTIONS, cred_ids)


if __name__ == "__main__":
    main(build)
