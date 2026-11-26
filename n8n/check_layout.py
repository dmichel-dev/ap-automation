"""Checks that each workflow canvas is readable: every node sits inside exactly one section note,
no two nodes overlap, and no two notes overlap. Run by scripts/check_workflows.sh (pre-commit, CI).

Usage: python3 n8n/check_layout.py   (exit code 1 and the problems listed if a canvas is wrong)
"""

import importlib
import sys
from itertools import combinations
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
from nodes import LABEL, NODE

BUILDERS = (
    "build_email_intake",
    "build_drive_intake",
    "build_post_qbo",
    "build_bank_sync",
    "build_sync_reference",
    "build_run_now",
    "build_error_log",
    "build_daily_summary",
    "build_e2e_test",
    "build_e2e_cleanup",
    "build_demo_processing",
)
Box = tuple[int, int, int, int]  # x, y, width, height
GAP = 20  # minimum free space between two nodes


def _note_box(n: dict[str, Any]) -> Box:
    p = n["parameters"]
    return n["position"][0], n["position"][1], p["width"], p["height"]


def _node_box(n: dict[str, Any]) -> Box:
    return n["position"][0], n["position"][1], NODE, NODE + LABEL


def _inside(inner: Box, outer: Box) -> bool:
    return (
        outer[0] <= inner[0]
        and outer[1] <= inner[1]
        and inner[0] + inner[2] <= outer[0] + outer[2]
        and inner[1] + inner[3] <= outer[1] + outer[3]
    )


def _overlap(a: Box, b: Box, gap: int = 0) -> bool:
    return not (
        a[0] + a[2] + gap <= b[0] or b[0] + b[2] + gap <= a[0] or a[1] + a[3] + gap <= b[1] or b[1] + b[3] + gap <= a[1]
    )


def problems(wf: dict[str, Any]) -> list[str]:
    notes = [n for n in wf["nodes"] if n["type"] == "n8n-nodes-base.stickyNote"]
    nodes = [n for n in wf["nodes"] if n["type"] != "n8n-nodes-base.stickyNote"]
    found = []
    for n in nodes:
        holders = [s["name"] for s in notes if _inside(_node_box(n), _note_box(s))]
        if len(holders) != 1:
            found.append(f"node '{n['name']}' is inside {len(holders)} notes {holders} (expected 1)")
    for a, b in combinations(nodes, 2):
        if _overlap(_node_box(a), _node_box(b), GAP):
            found.append(f"nodes '{a['name']}' and '{b['name']}' overlap")
    for a, b in combinations(notes, 2):
        if _overlap(_note_box(a), _note_box(b)):
            found.append(f"notes '{a['name']}' and '{b['name']}' overlap")
    return found


def main() -> int:
    status = 0
    for name in BUILDERS:
        wf = importlib.import_module(name).build(None)
        for p in problems(wf):
            print(f"{wf['name']}: {p}")
            status = 1
    return status


if __name__ == "__main__":
    sys.exit(main())
