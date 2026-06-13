"""A minimal MCP server for the cross-organizational supply-chain demo.

Exposes eight tools over MCP stdio, modelling a tiny procurement
toolkit. Reads supplier offers and inventory snapshots from sandboxed
fixture directories; writes outbound supplier messages, purchase
commitments, and finalized contracts as audit-friendly JSONL records.

  * list_suppliers()                                — discover suppliers
  * read_supplier_offer(supplier_id)                — full offer text
  * read_inventory(component)                       — current stock + threshold
  * read_market_signal(component)                   — analyst snapshot
  * send_supplier_message(supplier_id, message)     — outbound A2A stand-in
  * commit_purchase(supplier_id, component, qty, value_usd)
  * finalize_contract(supplier_id, summary, value_usd, term_months)
  * write_award_recommendation(filename, content)   — markdown deliverable

The server enforces sandboxing on filesystem reads/writes. Policy
enforcement (HITL on contract finalization, conditional approval on
high-value commits, and egress sovereignty inspection) lives in the
mesh above this server. The comms_guardrail script agent is only a
preflight helper for reformulation.

Run with:  python -m mcp_servers.procurement_server
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

SUPPLIERS_DIR = Path(os.environ.get("ANR_SUPPLIERS_DIR", "./data/suppliers")).resolve()
INVENTORY_DIR = Path(os.environ.get("ANR_INVENTORY_DIR", "./data/inventory")).resolve()
OUTPUT_DIR = Path(os.environ.get("ANR_AWARDS_DIR", "./output/awards")).resolve()
STATE_DIR = Path(os.environ.get("ANR_PROCUREMENT_STATE_DIR", "./output/procurement_state")).resolve()

OUTBOUND_LOG = STATE_DIR / "outbound_messages.jsonl"
COMMITS_LOG = STATE_DIR / "commits.jsonl"
CONTRACTS_LOG = STATE_DIR / "contracts.jsonl"

# Components the procurement system tracks. Anything else is rejected.
KNOWN_COMPONENTS: frozenset[str] = frozenset(
    {"controller_board_v3", "lithium_cell_18650", "harness_assembly_a", "thermal_paste_xt"}
)

_SAFE_ID = re.compile(r"^[A-Za-z0-9_\-.]{1,64}$")
_SAFE_FILENAME = re.compile(r"^[A-Za-z0-9_\-.]{1,80}\.md$")

app = FastMCP("anr-procurement")


def _resolve_under(root: Path, candidate: str) -> Path:
    root = root.resolve()
    target = (
        (root / candidate).resolve()
        if not Path(candidate).is_absolute()
        else Path(candidate).resolve()
    )
    try:
        target.relative_to(root)
    except ValueError as e:
        raise ValueError(f"path {candidate!r} escapes sandbox {root}") from e
    return target


def _append_state(path: Path, record: dict[str, Any]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    record = {"ts": time.time(), **record}
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")


# ----- Read tools ----------------------------------------------------------


@app.tool()
def list_suppliers() -> list[dict[str, Any]]:
    """Return id + headline (and component coverage) for every known supplier."""
    if not SUPPLIERS_DIR.exists():
        return []
    entries: list[dict[str, Any]] = []
    for p in sorted(SUPPLIERS_DIR.glob("*.md")):
        first_lines = p.read_text(encoding="utf-8").splitlines()[:8]
        meta: dict[str, Any] = {"id": p.stem}
        for ln in first_lines:
            if ln.lower().startswith("supplier:"):
                meta["supplier"] = ln.split(":", 1)[1].strip()
            elif ln.lower().startswith("component:"):
                meta["component"] = ln.split(":", 1)[1].strip()
            elif ln.lower().startswith("headline:"):
                meta["headline"] = ln.split(":", 1)[1].strip()
        entries.append(meta)
    return entries


@app.tool()
def read_supplier_offer(supplier_id: str) -> dict[str, Any]:
    """Read the full markdown body of one supplier's standing offer."""
    if not _SAFE_ID.match(supplier_id):
        raise ValueError(f"invalid supplier id {supplier_id!r}")
    target = _resolve_under(SUPPLIERS_DIR, f"{supplier_id}.md")
    if not target.is_file():
        raise FileNotFoundError(f"no supplier with id {supplier_id!r}")
    return {"supplier_id": supplier_id, "content": target.read_text(encoding="utf-8")}


@app.tool()
def read_inventory(component: str) -> dict[str, Any]:
    """Read current inventory + reorder threshold for one component."""
    if component not in KNOWN_COMPONENTS:
        raise ValueError(
            f"unknown component {component!r}; known: {sorted(KNOWN_COMPONENTS)}"
        )
    target = _resolve_under(INVENTORY_DIR, "snapshot.json")
    if not target.is_file():
        raise FileNotFoundError("no inventory snapshot")
    snapshot = json.loads(target.read_text("utf-8"))
    record = snapshot.get(component)
    if record is None:
        return {"component": component, "available": False}
    return {"component": component, "available": True, **record}


@app.tool()
def read_market_signal(component: str) -> dict[str, Any]:
    """Read the analyst market snapshot for one component (price trend, supply risk)."""
    if component not in KNOWN_COMPONENTS:
        raise ValueError(f"unknown component {component!r}")
    target = _resolve_under(INVENTORY_DIR, "market_signals.json")
    if not target.is_file():
        return {"component": component, "available": False}
    signals = json.loads(target.read_text("utf-8"))
    record = signals.get(component)
    if record is None:
        return {"component": component, "available": False}
    return {"component": component, "available": True, **record}


# ----- Action tools --------------------------------------------------------


@app.tool()
def send_supplier_message(supplier_id: str, message: str) -> dict[str, Any]:
    """Record an outbound message to a supplier (stand-in for cross-org A2A).

    Real ANR doesn't ship A2A; this tool just appends the message to the
    outbound log so the audit trail captures what crossed the boundary.
    The mesh inspects this egress payload before the call reaches the
    server, and may block, sanitize, or escalate it. A prompt-level
    comms_guardrail preflight may also have helped the agent reformulate.
    """
    if not _SAFE_ID.match(supplier_id):
        raise ValueError(f"invalid supplier id {supplier_id!r}")
    if not message.strip():
        raise ValueError("message must not be empty")
    record = {"supplier_id": supplier_id, "message": message}
    _append_state(OUTBOUND_LOG, record)
    return {"sent": True, "log": str(OUTBOUND_LOG)}


@app.tool()
def commit_purchase(
    supplier_id: str, component: str, qty: int, value_usd: float
) -> dict[str, Any]:
    """Commit to a purchase. The mesh's conditional HITL fires above $50k."""
    if not _SAFE_ID.match(supplier_id):
        raise ValueError(f"invalid supplier id {supplier_id!r}")
    if component not in KNOWN_COMPONENTS:
        raise ValueError(f"unknown component {component!r}")
    if not 1 <= qty <= 1_000_000:
        raise ValueError(f"qty out of range: {qty}")
    if not 0 < value_usd <= 5_000_000:
        raise ValueError(f"value_usd out of range: {value_usd}")
    record = {
        "supplier_id": supplier_id,
        "component": component,
        "qty": qty,
        "value_usd": value_usd,
    }
    _append_state(COMMITS_LOG, record)
    return {"committed": record, "log": str(COMMITS_LOG)}


@app.tool()
def finalize_contract(
    supplier_id: str, summary: str, value_usd: float, term_months: int
) -> dict[str, Any]:
    """Finalize a long-term contract. Always HITL-gated by the mesh."""
    if not _SAFE_ID.match(supplier_id):
        raise ValueError(f"invalid supplier id {supplier_id!r}")
    if not summary.strip():
        raise ValueError("summary must not be empty")
    if not 0 < value_usd <= 25_000_000:
        raise ValueError(f"value_usd out of range: {value_usd}")
    if not 1 <= term_months <= 60:
        raise ValueError(f"term_months out of range: {term_months}")
    record = {
        "supplier_id": supplier_id,
        "summary": summary,
        "value_usd": value_usd,
        "term_months": term_months,
    }
    _append_state(CONTRACTS_LOG, record)
    return {"finalized": record, "log": str(CONTRACTS_LOG)}


@app.tool()
def write_award_recommendation(filename: str, content: str) -> dict[str, Any]:
    """Write a markdown award recommendation to OUTPUT_DIR/<filename>."""
    if not _SAFE_FILENAME.match(filename):
        raise ValueError(f"filename must match {_SAFE_FILENAME.pattern}, got {filename!r}")
    if not content.strip():
        raise ValueError("content must not be empty")
    target = _resolve_under(OUTPUT_DIR, filename)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return {"path": str(target), "bytes": len(content.encode("utf-8"))}


def main() -> None:
    app.run()


if __name__ == "__main__":
    main()
