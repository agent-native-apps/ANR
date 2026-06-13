"""A minimal MCP server for the inbox-triage demo.

Exposes four tools over MCP stdio:

  * list_emails()                              — enumerate inbox metadata
  * read_email(id)                             — full body of one email
  * mark_urgent(id, subject, reason)           — append to an urgent-flags file
  * save_draft(to, subject, body, in_reply_to) — write a markdown draft

The server enforces path sandboxing for filesystem operations (ANR_INBOX_DIR,
ANR_DRAFTS_DIR, ANR_STATE_DIR). Policy-as-code enforcement (HITL checkpoints,
per-agent tool whitelists, rate-threshold triggers) is layered above this
server by the anr mesh.

Run with:  python -m mcp_servers.inbox_server
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

logging.getLogger("mcp.server").setLevel(logging.WARNING)
logging.getLogger("mcp.server.lowlevel.server").setLevel(logging.WARNING)

INBOX_DIR = Path(os.environ.get("ANR_INBOX_DIR", "./data/inbox")).resolve()
DRAFTS_DIR = Path(os.environ.get("ANR_DRAFTS_DIR", "./output/drafts")).resolve()
STATE_DIR = Path(os.environ.get("ANR_STATE_DIR", "./output/state")).resolve()
URGENT_FLAGS = STATE_DIR / "urgent_flags.jsonl"

# A permissive-but-sane filename rule for email ids and draft filenames.
_SAFE_ID = re.compile(r"^[A-Za-z0-9_\-.]{1,64}$")

app = FastMCP("anr-inbox")


def _resolve_under(root: Path, candidate: str) -> Path:
    root = root.resolve()
    target = (root / candidate).resolve() if not Path(candidate).is_absolute() else Path(candidate).resolve()
    try:
        target.relative_to(root)
    except ValueError as e:
        raise ValueError(f"path {candidate!r} escapes sandbox {root}") from e
    return target


def _load_email(email_id: str) -> dict[str, Any]:
    if not _SAFE_ID.match(email_id):
        raise ValueError(f"invalid email id {email_id!r}")
    target = _resolve_under(INBOX_DIR, f"{email_id}.json")
    if not target.is_file():
        raise FileNotFoundError(f"no email with id {email_id!r}")
    return json.loads(target.read_text(encoding="utf-8"))


@app.tool()
def list_emails() -> list[dict[str, Any]]:
    """Return metadata (id, from, to, date, subject) for every email in the inbox."""
    if not INBOX_DIR.exists():
        return []
    entries: list[dict[str, Any]] = []
    for p in sorted(INBOX_DIR.glob("*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        entries.append(
            {
                "id": data.get("id", p.stem),
                "from": data.get("from", ""),
                "to": data.get("to", ""),
                "date": data.get("date", ""),
                "subject": data.get("subject", ""),
            }
        )
    return entries


@app.tool()
def read_email(id: str) -> dict[str, Any]:
    """Return the full content of one email by id (e.g. 'e001')."""
    return _load_email(id)


@app.tool()
def mark_urgent(id: str, subject: str, reason: str) -> dict[str, Any]:
    """Flag an email as urgent. Appends {id, subject, reason, ts} to the flags log.

    `subject` and `reason` are required (empty strings are rejected) so the
    audit trail is meaningful — the mesh's conditional HITL checks also key
    off the subject field.
    """
    # Validate the email id resolves to a real email.
    _load_email(id)
    if not subject.strip():
        raise ValueError("subject must not be empty")
    if not reason.strip():
        raise ValueError("reason must not be empty")

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    record = {
        "id": id,
        "subject": subject,
        "reason": reason,
        "ts": time.time(),
    }
    with URGENT_FLAGS.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")
    return {"flagged": id, "flags_file": str(URGENT_FLAGS)}


@app.tool()
def save_draft(to: str, subject: str, body: str, in_reply_to: str) -> dict[str, Any]:
    """Write a markdown draft reply to DRAFTS_DIR/<in_reply_to>.md.

    The draft body is wrapped in a small markdown envelope (`To:`, `Subject:`,
    `In-Reply-To:` headers above a horizontal rule) so the on-disk artifact
    reads like an email and captures routing metadata.
    """
    if not _SAFE_ID.match(in_reply_to):
        raise ValueError(f"invalid in_reply_to id {in_reply_to!r}")
    if not to.strip() or not subject.strip() or not body.strip():
        raise ValueError("to, subject, and body must all be non-empty")

    target = _resolve_under(DRAFTS_DIR, f"{in_reply_to}.md")
    target.parent.mkdir(parents=True, exist_ok=True)
    content = (
        f"To: {to}\n"
        f"Subject: {subject}\n"
        f"In-Reply-To: {in_reply_to}\n"
        f"\n---\n\n"
        f"{body}\n"
    )
    target.write_text(content, encoding="utf-8")
    return {"path": str(target), "bytes": len(content.encode("utf-8"))}


def main() -> None:
    app.run()


if __name__ == "__main__":
    main()
