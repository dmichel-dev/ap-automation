"""Who acted, as recorded in the audit trail (review decisions, write-offs, supplier defaults, the log).

Two kinds of actor, never confused:
  person      signed in on the review page: "<display name> (review user <id>)", built from the session
  automation  a script or agent calling the core API with its bearer token: "<name>" or "<name> (<context>)",
              e.g. "claude-code (test run, authorised by David Michel)"

The bearer-token API acts for automation only, so it records a decision only under an automation label: a
caller can't write a person's name into the audit trail. A person's display name can't take an automation
name either, so the two can't be mistaken for each other in either direction.
"""

import re

AUTOMATION_NAMES = ("claude-code", "e2e-test")
_NAMES = "|".join(map(re.escape, AUTOMATION_NAMES))
_AUTOMATION = re.compile(rf"(?:{_NAMES})(?: \([^()\n]{{1,80}}\))?")
_PERSON = re.compile(r".+ \(review user \d+\)")


class NotAutomation(ValueError):
    """An automation call named an actor that isn't an automation label (for example a person's name)."""


def person_label(display_name: str, user_id: int) -> str:
    """How a signed-in person's decisions are recorded: the name, and the account that acted."""
    return f"{display_name} (review user {user_id})"


def is_automation(label: str) -> bool:
    return _AUTOMATION.fullmatch(label) is not None and _PERSON.fullmatch(label) is None


def require_automation(label: str) -> str:
    if not is_automation(label):
        raise NotAutomation(
            f"an automated call records its decisions under an automation label "
            f"({' or '.join(AUTOMATION_NAMES)}, optionally followed by ' (context)'), not {label!r}"
        )
    return label


def takes_automation_name(display_name: str) -> bool:
    """True when a person's display name would read as an automation label."""
    first = display_name.strip().split(" ", 1)[0].casefold()
    return first in AUTOMATION_NAMES
