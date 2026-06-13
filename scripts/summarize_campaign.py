#!/usr/bin/env python3
"""Summarize ANR campaign audit logs.

The paper reports descriptive audit-derived counts from unattended ANR
campaigns. This helper recomputes the same style of quantities from a campaign
root, defaulting to the committed logs under ``artifacts/campaign``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ROOT = REPO_ROOT / "artifacts" / "campaign"
REPORTED_SCENARIOS = ("research_assistant", "emergency_response", "supply_chain")


@dataclass
class RunSummary:
    events: int = 0
    tool_calls: int = 0
    hitl_checkpoints: int = 0
    reshapes: int = 0
    policy_refusals: int = 0
    boundary: Counter[str] = field(default_factory=Counter)
    trace_hash: str = ""


@dataclass
class CampaignSummary:
    name: str
    condition: str
    runs: list[RunSummary] = field(default_factory=list)
    manifest_rows: int = 0


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_no}: invalid JSON") from exc
    return records


def boundary_outcome(record: dict[str, Any]) -> str:
    outcome = record.get("outcome")
    if isinstance(outcome, dict):
        raw = outcome.get("enforcement_outcome")
        if isinstance(raw, str) and raw:
            return raw
    return "unknown"


def trace_signature(record: dict[str, Any]) -> tuple[Any, ...]:
    """Return a stable, content-light event signature for distinct-trace counts."""

    outcome = record.get("outcome") if isinstance(record.get("outcome"), dict) else {}
    args = record.get("args") if isinstance(record.get("args"), dict) else {}
    return (
        record.get("kind"),
        record.get("caller"),
        record.get("tool"),
        record.get("checkpoint_id"),
        record.get("policy_id"),
        outcome.get("enforcement_outcome"),
        outcome.get("ok"),
        args.get("target_agent"),
        args.get("template") or args.get("template_name"),
    )


def summarize_run(audit_path: Path) -> RunSummary:
    records = load_jsonl(audit_path)
    summary = RunSummary(events=len(records))
    signature_parts: list[str] = []

    for record in records:
        kind = record.get("kind")
        signature_parts.append(json.dumps(trace_signature(record), sort_keys=True))
        if kind == "tool_call":
            summary.tool_calls += 1
        elif kind == "hitl_checkpoint":
            summary.hitl_checkpoints += 1
        elif kind == "reshape":
            summary.reshapes += 1
        elif kind == "policy_refusal":
            summary.policy_refusals += 1
        elif kind == "boundary_decision":
            summary.boundary[boundary_outcome(record)] += 1

    digest = hashlib.sha256("\n".join(signature_parts).encode("utf-8")).hexdigest()
    summary.trace_hash = digest[:12]
    return summary


def count_manifest_rows(path: Path) -> int:
    if not path.is_file():
        return 0
    return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())


def load_campaign(root: Path, name: str, condition: str) -> CampaignSummary:
    campaign_dir = root / name
    summary = CampaignSummary(
        name=name.removesuffix("_expose_all"),
        condition=condition,
        manifest_rows=count_manifest_rows(campaign_dir / "manifest.jsonl"),
    )
    for audit_path in sorted(campaign_dir.glob("run-*/audit.jsonl")):
        summary.runs.append(summarize_run(audit_path))
    return summary


def avg(values: list[int]) -> float:
    return sum(values) / len(values) if values else 0.0


def fmt(value: float) -> str:
    return f"{value:.1f}"


def total_boundary(runs: list[RunSummary]) -> Counter[str]:
    counts: Counter[str] = Counter()
    for run in runs:
        counts.update(run.boundary)
    return counts


def print_campaign_table(campaigns: list[CampaignSummary]) -> None:
    print("# ANR campaign summary\n")
    print("| Scenario | Condition | Runs | Manifest rows | Tool calls/run | HITL/run | Reshapes/run | Refusals/run | Refusals total | Distinct traces | Boundary totals |")
    print("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|")
    for campaign in campaigns:
        runs = campaign.runs
        boundary = total_boundary(runs)
        boundary_text = ", ".join(
            f"{key}={boundary[key]}" for key in sorted(boundary)
        ) or "--"
        distinct_traces = len({run.trace_hash for run in runs})
        total_refusals = sum(run.policy_refusals for run in runs)
        print(
            "| "
            f"{campaign.name} | "
            f"{campaign.condition} | "
            f"{len(runs)} | "
            f"{campaign.manifest_rows} | "
            f"{fmt(avg([r.tool_calls for r in runs]))} | "
            f"{fmt(avg([r.hitl_checkpoints for r in runs]))} | "
            f"{fmt(avg([r.reshapes for r in runs]))} | "
            f"{fmt(avg([r.policy_refusals for r in runs]))} | "
            f"{total_refusals} | "
            f"{distinct_traces} | "
            f"{boundary_text} |"
        )


def print_paper_mapping() -> None:
    print("\n## Event-kind mapping\n")
    print("| Paper-table quantity | Audit records counted |")
    print("|---|---|")
    print("| Mesh-mediated tool calls per run | `kind == \"tool_call\"` |")
    print("| HITL episodes per run | `kind == \"hitl_checkpoint\"` |")
    print("| Graph-reshape ops per run | `kind == \"reshape\"` |")
    print("| Boundary decisions | `kind == \"boundary_decision\"`, grouped by `outcome.enforcement_outcome`; the paper row reports allow / sanitize / block, while raw logs also include `escalate_to_human` |")
    print("| Agent actions refused | `kind == \"policy_refusal\"`; totals match the paper's `x -> 0` rows because refusals are returned before dispatch |")
    print("| Distinct traces | Stable hash of event kind, caller, tool, checkpoint, policy, and enforcement outcome sequence |")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "root",
        nargs="?",
        type=Path,
        default=DEFAULT_ROOT,
        help="Campaign root to summarize (default: artifacts/campaign)",
    )
    args = parser.parse_args()

    root = args.root.resolve()
    if not root.is_dir():
        parser.error(f"campaign root not found: {root}")

    campaigns: list[CampaignSummary] = []
    for scenario in REPORTED_SCENARIOS:
        calibrated = root / scenario
        exposed = root / f"{scenario}_expose_all"
        if calibrated.is_dir():
            campaigns.append(load_campaign(root, scenario, "calibrated"))
        if exposed.is_dir():
            campaigns.append(load_campaign(root, f"{scenario}_expose_all", "fault-injected"))

    print_campaign_table(campaigns)
    print_paper_mapping()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
