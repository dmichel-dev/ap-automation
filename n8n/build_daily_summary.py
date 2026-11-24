"""Builds the 'AP – Daily summary' n8n workflow JSON.

Usage:
  python3 n8n/build_daily_summary.py > out.json                         # no credential IDs (committable)
  python3 n8n/build_daily_summary.py --credentials ids.json > out.json  # with local credential IDs, for import

What the workflow does (every day at 08:00 Paris time, or by hand):
  1. Asks the core for the summary of the last 24 hours. The core computes every figure and writes the
     email (recipients, subject, text): invoices, touchless rate, open review items, fraud alerts, bank
     details seen for the first time, email intake problems.
  2. Sends it with Gmail and logs that it was sent (step daily_summary).
A failed core call ends the run as failed, so the error workflow logs it.
"""

from typing import Any

from nodes import (
    AUTO,
    CORE,
    Connections,
    Node,
    Section,
    core_call,
    gmail,
    if_node,
    lay_out,
    main,
    postgres_query,
    stop,
    workflow,
)

SUMMARY = "$('Daily summary (core)').first().json.body"
LOG_SQL = (
    "INSERT INTO ap.processing_log (channel, n8n_execution_id, step, level, message, details)\n"
    "VALUES ('system', $1, 'daily_summary', 'info', 'daily summary sent', $2::jsonb)"
)


def _nodes() -> list[Node]:
    return [
        {
            "name": "Every day at 08:00",
            "type": "n8n-nodes-base.scheduleTrigger",
            "typeVersion": 1.2,
            "position": AUTO,
            "parameters": {"rule": {"interval": [{"field": "days", "daysInterval": 1, "triggerAtHour": 8}]}},
        },
        {
            "name": "Manual run",
            "type": "n8n-nodes-base.manualTrigger",
            "typeVersion": 1,
            "position": AUTO,
            "parameters": {},
        },
        core_call("Daily summary (core)", AUTO, "GET", f"{CORE}/summary/daily"),
        if_node("Summary ready?", AUTO, "$json.statusCode === 200"),
        gmail(
            "Send summary email",
            {
                "resource": "message",
                "operation": "send",
                "sendTo": "={{ " + SUMMARY + ".to }}",
                "subject": "={{ " + SUMMARY + ".subject }}",
                "emailType": "html",
                "message": "={{ " + SUMMARY + ".html }}",
                "options": {"appendAttribution": False},
            },
        ),
        postgres_query(
            "Log summary sent",
            AUTO,
            LOG_SQL,
            ["$execution.id", "JSON.stringify({ subject: " + SUMMARY + ".subject })"],
        ),
        stop(
            "Stop: no summary",
            AUTO,
            "'daily summary not built: HTTP ' + ($json.statusCode ?? 0) + ' ' + ($json.body?.detail ?? '')",
        ),
    ]


CONNECTIONS: Connections = {
    "Every day at 08:00": [[("Daily summary (core)", 0)]],
    "Manual run": [[("Daily summary (core)", 0)]],
    "Daily summary (core)": [[("Summary ready?", 0)]],
    "Summary ready?": [[("Send summary email", 0)], [("Stop: no summary", 0)]],
    "Send summary email": [[("Log summary sent", 0)]],
}

SECTIONS = [
    Section(
        "Daily summary",
        "One email every morning about the **last 24 hours**: invoices received and posted, touchless rate, "
        "payments and write-offs, open review items (oldest first), fraud alerts, bank details seen for the "
        "first time, email intake problems.\n\n"
        "The **core** computes every figure and writes the email; this workflow only starts it and sends it. "
        "Live data only; bank details masked.",
        col=0,
        row=0,
        kind="overview",
        min_cols=6,
    ),
    Section(
        "Build",
        "Every day at 08:00 (Paris time), or by hand. The core answers with the recipients, subject and text.",
        col=0,
        row=2,
        grid=(("Every day at 08:00", "Daily summary (core)", "Summary ready?"), ("Manual run", None, None)),
    ),
    Section(
        "Send",
        "Gmail sends it to the configured recipients (DAILY_SUMMARY_TO), then the AP log records it.",
        col=3,
        row=2,
        grid=(("Send summary email", "Log summary sent"),),
    ),
    Section(
        "Not built",
        "The core did not answer: the run fails, so the error workflow logs it.",
        col=3,
        row=4,
        grid=(("Stop: no summary",),),
        kind="failure",
    ),
]


def build(cred_ids: dict[str, str] | None) -> dict[str, Any]:
    return workflow("AP – Daily summary", lay_out(_nodes(), SECTIONS), CONNECTIONS, cred_ids)


if __name__ == "__main__":
    main(build)
