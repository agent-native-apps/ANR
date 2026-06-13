"""Script-agent bodies for specs/supply_chain.yaml.

The compiler inserts the spec directory into sys.path so this module is
importable as `supply_chain_agents`.

`comms_guardrail` plays the §6.2 sovereignty guardrail: a deterministic
outbound-message reviewer that flags drafts mentioning sovereign signals
(inventory state, urgency cues, sole-source disclosures) before they
cross the organizational boundary. The procurement agent delegates a
draft to this node and gets back either an APPROVED verdict or a
BLOCKED verdict with the offending phrases listed; on BLOCKED, the
procurement agent is expected to reformulate.
"""

from __future__ import annotations

import re
from typing import Any

# Pattern → human-readable rule. Order matters only for the displayed
# explanation; every pattern is checked.
_SOVEREIGNTY_RULES: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(
            r"\b(running low|low stock|stock(?:\s+(?:is|are))?\s+(?:low|critically|down)|"
            r"low\s+inventory|inventory(?:\s+(?:is|are))?\s+(?:low|critically|down|tight)|"
            r"only\s+\d+\s+(?:units|cells|boards)|below(?:\s+our)?\s+reorder)\b",
            re.IGNORECASE,
        ),
        "discloses inventory state",
    ),
    (
        re.compile(
            r"\b(deadline|must\s+have|need(?:ed)?(?:\s+\w+){0,2}\s+by|production\s+stop|"
            r"line\s+stoppage|will\s+halt|cannot\s+wait|urgent(?:ly)?|"
            r"by\s+(?:next\s+(?:week|month|quarter)|month[-\s]end|end\s+of|"
            r"(?:the\s+)?(?:end\s+of\s+)?(?:this|next)\s+(?:week|month|quarter)))\b",
            re.IGNORECASE,
        ),
        "discloses delivery urgency",
    ),
    (
        re.compile(
            r"\b(sole\s+source|only\s+(?:viable\s+)?(?:supplier|source)|no\s+alternatives?|"
            r"only\s+option|no\s+(?:other|backup)\s+suppliers?)\b",
            re.IGNORECASE,
        ),
        "discloses sole-source dependence",
    ),
    (
        re.compile(
            r"\b(weeks?\s+of\s+cover|burn\s+rate|consumption\s+rate)\b",
            re.IGNORECASE,
        ),
        "discloses internal consumption metrics",
    ),
    (
        re.compile(
            r"\b(target\s+price|maximum\s+price|cost\s+ceiling|budget\s+ceiling|"
            r"won['']?t\s+pay\s+more\s+than|cap(?:ped)?\s+at\s+\$)\b",
            re.IGNORECASE,
        ),
        "discloses internal pricing strategy",
    ),
]


async def comms_guardrail(task: str, ctx: Any, mesh: Any) -> str:
    """Review a proposed outbound supplier message for sovereignty leaks.

    The procurement agent passes the draft message text as the task
    string. The guardrail returns one of:

      * APPROVED — followed by the draft, ready to send
      * BLOCKED  — followed by a bulleted list of (rule, offending phrase)
                   pairs and an instruction to reformulate

    The mesh and the procurement prompt enforce that send_supplier_message
    can only be called after the guardrail returns APPROVED.
    """
    # Strip a "Draft:" prefix if the procurement agent added one, so the
    # guardrail sees just the text that would cross the boundary.
    draft = task.strip()
    for prefix in ("draft:", "Draft:", "DRAFT:", "message:", "Message:"):
        if draft.startswith(prefix):
            draft = draft[len(prefix):].strip()
            break

    if not draft:
        return "BLOCKED — empty draft; nothing to review."

    findings: list[tuple[str, str]] = []
    for pattern, rule in _SOVEREIGNTY_RULES:
        for match in pattern.finditer(draft):
            findings.append((rule, match.group(0)))

    if not findings:
        return f"APPROVED\n\n---\n{draft}"

    lines = ["BLOCKED — sovereignty guardrail flagged the following:"]
    seen: set[tuple[str, str]] = set()
    for rule, phrase in findings:
        key = (rule, phrase.lower())
        if key in seen:
            continue
        seen.add(key)
        lines.append(f"  - {rule}: {phrase!r}")
    lines.append("")
    lines.append(
        "Reformulate the message to preserve the negotiating position "
        "without revealing internal inventory, urgency, sole-source "
        "dependence, consumption rates, or pricing strategy. Resubmit "
        "the new draft for review before sending."
    )
    return "\n".join(lines)
