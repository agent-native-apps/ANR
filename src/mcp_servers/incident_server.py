"""A minimal MCP server for the emergency-response demo.

Exposes seven tools over MCP stdio, modelling a tiny incident-command
toolkit. Telemetry, dispatches, evacuation orders, and inter-agency
consultations all land as audit-friendly JSONL records under
ANR_STATE_DIR; situation reports are markdown under ANR_OUTPUT_DIR.

  * list_incident_reports()                       — what's open in the field
  * read_incident_report(id)                      — full text of one report
  * read_sensor_telemetry(site, kind)             — structural / chemical / thermal
  * dispatch_resources(site, resource_kind, count)
  * issue_evacuation_order(site, perimeter_blocks, justification)
  * consult_external_agency(agency, query)        — stand-in for A2A
  * write_situation_report(filename, content)     — markdown SITREP

The server enforces path sandboxing for filesystem operations
(ANR_INCIDENTS_DIR for reads, ANR_SITREPS_DIR for SITREP writes).
Policy-as-code enforcement (HITL on evacuation orders, conditional
approval on big dispatches, allow-list of consultable agencies, and
egress inspection on external consultations) is layered above this
server by the anr mesh.

Run with:  python -m mcp_servers.incident_server
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

INCIDENTS_DIR = Path(os.environ.get("ANR_INCIDENTS_DIR", "./data/incidents")).resolve()
# Dedicated env var (not ANR_OUTPUT_DIR — the compiler injects that for
# tools_server's write_note default; sharing it would collide and route
# SITREPs into output/notes/).
OUTPUT_DIR = Path(os.environ.get("ANR_SITREPS_DIR", "./output/sitreps")).resolve()
STATE_DIR = Path(os.environ.get("ANR_INCIDENT_STATE_DIR", "./output/incident_state")).resolve()

DISPATCH_LOG = STATE_DIR / "dispatches.jsonl"
EVAC_LOG = STATE_DIR / "evacuations.jsonl"
CONSULT_LOG = STATE_DIR / "consultations.jsonl"

# Sites referenced in the fixture incident reports. Used to validate
# dispatch / evacuation / telemetry calls so agents cannot invent locations.
KNOWN_SITES: frozenset[str] = frozenset(
    {"riverside_tower", "north_warehouse", "metro_overpass", "harbor_terminal"}
)

# Telemetry kinds the fixture supports.
KNOWN_TELEMETRY: frozenset[str] = frozenset({"structural", "chemical", "thermal"})

# Resources the lead agency stocks. Anything else is rejected.
KNOWN_RESOURCES: frozenset[str] = frozenset(
    {"medic_unit", "fire_engine", "rescue_team", "hazmat_team", "drone", "ambulance"}
)

# Allow-listed neighbouring agencies for cross-agency consultation.
# An off-list agency would fail at the MCP layer; the mesh additionally
# requires HITL on every consult call regardless of the agency.
KNOWN_AGENCIES: frozenset[str] = frozenset(
    {"county_hazmat_unit", "regional_fire_command", "state_emergency_ops"}
)

_SAFE_ID = re.compile(r"^[A-Za-z0-9_\-.]{1,64}$")
_SAFE_FILENAME = re.compile(r"^[A-Za-z0-9_\-.]{1,80}\.md$")

app = FastMCP("anr-incident")


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
def list_incident_reports() -> list[dict[str, Any]]:
    """Return the id, site, and one-line headline of every open incident report."""
    if not INCIDENTS_DIR.exists():
        return []
    entries: list[dict[str, Any]] = []
    for p in sorted(INCIDENTS_DIR.glob("*.md")):
        first_lines = p.read_text(encoding="utf-8").splitlines()[:6]
        meta: dict[str, Any] = {"id": p.stem}
        for ln in first_lines:
            if ln.lower().startswith("site:"):
                meta["site"] = ln.split(":", 1)[1].strip()
            elif ln.lower().startswith("severity:"):
                meta["severity"] = ln.split(":", 1)[1].strip()
            elif ln.lower().startswith("headline:"):
                meta["headline"] = ln.split(":", 1)[1].strip()
        entries.append(meta)
    return entries


@app.tool()
def read_incident_report(id: str) -> dict[str, Any]:
    """Read the full markdown body of one incident report by id (e.g. 'inc-002')."""
    if not _SAFE_ID.match(id):
        raise ValueError(f"invalid incident id {id!r}")
    target = _resolve_under(INCIDENTS_DIR, f"{id}.md")
    if not target.is_file():
        raise FileNotFoundError(f"no incident with id {id!r}")
    return {"id": id, "content": target.read_text(encoding="utf-8")}


@app.tool()
def read_sensor_telemetry(site: str, kind: str) -> dict[str, Any]:
    """Read the latest telemetry record for `site` and `kind`.

    `kind` must be one of: structural, chemical, thermal. The sandboxed
    fixture lives under data/incidents/telemetry/<site>.<kind>.json.
    """
    if site not in KNOWN_SITES:
        raise ValueError(f"unknown site {site!r}; known: {sorted(KNOWN_SITES)}")
    if kind not in KNOWN_TELEMETRY:
        raise ValueError(f"unknown telemetry kind {kind!r}; known: {sorted(KNOWN_TELEMETRY)}")
    target = _resolve_under(INCIDENTS_DIR / "telemetry", f"{site}.{kind}.json")
    if not target.is_file():
        return {"site": site, "kind": kind, "available": False, "note": "no recent telemetry"}
    return {"site": site, "kind": kind, "available": True, "data": json.loads(target.read_text("utf-8"))}


# ----- Action tools --------------------------------------------------------


@app.tool()
def dispatch_resources(site: str, resource_kind: str, count: int) -> dict[str, Any]:
    """Dispatch `count` units of `resource_kind` to `site`. Records to the dispatch log."""
    if site not in KNOWN_SITES:
        raise ValueError(f"unknown site {site!r}")
    if resource_kind not in KNOWN_RESOURCES:
        raise ValueError(f"unknown resource_kind {resource_kind!r}; known: {sorted(KNOWN_RESOURCES)}")
    if not 1 <= count <= 50:
        raise ValueError(f"count must be 1..50, got {count}")
    record = {"site": site, "resource_kind": resource_kind, "count": count}
    _append_state(DISPATCH_LOG, record)
    return {"dispatched": record, "log": str(DISPATCH_LOG)}


@app.tool()
def issue_evacuation_order(site: str, perimeter_blocks: int, justification: str) -> dict[str, Any]:
    """Issue an evacuation order around `site` covering `perimeter_blocks` city blocks.

    `justification` must be non-empty — it is preserved on the audit
    trail as the recorded basis for the order.
    """
    if site not in KNOWN_SITES:
        raise ValueError(f"unknown site {site!r}")
    if not 1 <= perimeter_blocks <= 20:
        raise ValueError(f"perimeter_blocks must be 1..20, got {perimeter_blocks}")
    if not justification.strip():
        raise ValueError("justification must not be empty")
    record = {
        "site": site,
        "perimeter_blocks": perimeter_blocks,
        "justification": justification,
    }
    _append_state(EVAC_LOG, record)
    return {"issued": record, "log": str(EVAC_LOG)}


@app.tool()
def consult_external_agency(agency: str, query: str) -> dict[str, Any]:
    """Stand-in for cross-agency A2A consultation.

    Real ANR doesn't ship an A2A transport (the paper notes this is a
    deliberate scope limit); the call returns a deterministic canned
    response keyed off the agency, so the agent can complete its
    reasoning chain. The mesh additionally requires HITL on every call.
    """
    if agency not in KNOWN_AGENCIES:
        raise ValueError(f"unknown agency {agency!r}; known: {sorted(KNOWN_AGENCIES)}")
    if not query.strip():
        raise ValueError("query must not be empty")
    canned: dict[str, str] = {
        "county_hazmat_unit": (
            "Probable agent: chlorine dioxide aerosol release. "
            "Recommended PPE: Level B with positive-pressure SCBA. "
            "Recommended evacuation perimeter: 4 blocks downwind, "
            "2 blocks upwind. Decontamination corridor required."
        ),
        "regional_fire_command": (
            "Two additional engine companies and one ladder company can "
            "stage within 18 minutes. Mutual-aid form 4-B will follow."
        ),
        "state_emergency_ops": (
            "State coordinator acknowledges the request. Public-alert "
            "drafting and shelter activation are being prepared in parallel."
        ),
    }
    record = {"agency": agency, "query": query, "response": canned[agency]}
    _append_state(CONSULT_LOG, record)
    return record


@app.tool()
def write_situation_report(filename: str, content: str) -> dict[str, Any]:
    """Write a markdown situation report (SITREP) to OUTPUT_DIR/<filename>."""
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
