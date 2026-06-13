"""Build a click-to-inspect detail payload for any graph node.

Given a node id (``agent:NAME``, ``tool:NAME``, ``data:NAME``,
``template:NAME``, ``hitl:NAME``) and the loaded spec, returns a structured dict the
client renders into the inspector modal:

    {
      "title":     "coordinator",
      "subtitle":  "native · anthropic/claude-haiku-4-5 · entry point",
      "kind":      "agent",
      "sections":  [
        {"label": "Description",  "kind": "text",  "value": "..."},
        {"label": "Tools",        "kind": "list",  "value": ["grep_files", ...]},
        {"label": "System prompt","kind": "code",  "value": "..."},
        ...
      ],
    }
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from anr.spec import (
    AcquireCapabilityChange,
    Agent,
    AgentTemplate,
    DataSource,
    InstantiateTemplateChange,
    SpawnInstanceChange,
    Spec,
    Tool,
)


def build_node_info(
    spec: Spec, spec_dir: Path, node_id: str
) -> dict[str, Any] | None:
    """Resolve `node_id` into a JSON-friendly inspector payload."""
    if ":" not in node_id:
        return None
    kind, name = node_id.split(":", 1)
    if kind == "agent":
        agent = next((a for a in spec.agents if a.name == name), None)
        if agent is None:
            return None
        return _agent_info(agent, spec, spec_dir)
    if kind == "template":
        tpl = next((t for t in spec.agent_templates if t.name == name), None)
        if tpl is None:
            return None
        return _template_info(tpl, spec)
    if kind == "tool":
        tool = next((t for t in spec.tools if t.name == name), None)
        if tool is None:
            return None
        return _tool_info(tool, spec)
    if kind == "data":
        ds = next((d for d in spec.data_sources if d.name == name), None)
        if ds is None:
            return None
        return _data_info(ds, spec)
    if kind == "hitl":
        cp = next((c for c in spec.envelope.hitl_checkpoints if c.id == name), None)
        if cp is None:
            return None
        return _hitl_info(cp)
    return None


# ---------------------------------------------------------------------------


def _agent_info(agent: Agent, spec: Spec, spec_dir: Path) -> dict[str, Any]:
    bits = [agent.kind]
    if agent.model:
        bits.append(agent.model)
    if agent.role == "entry_point":
        bits.append("entry point")
    subtitle = " · ".join(bits)

    sections: list[dict[str, Any]] = []
    if agent.tools:
        sections.append({"label": "Tools", "kind": "list", "value": list(agent.tools)})
    if agent.may_delegate_to:
        sections.append(
            {
                "label": "May delegate to",
                "kind": "list",
                "value": list(agent.may_delegate_to),
            }
        )

    # HITL checkpoints affecting this agent: those whose tool the agent
    # can call, or that explicitly allow it via agent_initiated.
    hitl = _hitl_for_agent(agent, spec)
    if hitl:
        sections.append({"label": "HITL gates", "kind": "table", "value": hitl})

    # Reshape rules that name this agent as caller.
    reshape = _reshape_for_caller(agent.name, spec)
    if reshape:
        sections.append(
            {"label": "May reshape graph", "kind": "table", "value": reshape}
        )

    if agent.system_prompt_file:
        prompt = _read_prompt_file(agent.system_prompt_file, spec_dir)
        sections.append(
            {
                "label": f"System prompt  ({agent.system_prompt_file})",
                "kind": "code",
                "value": prompt,
            }
        )
    if agent.script_entry:
        sections.append(
            {"label": "Script entry", "kind": "text", "value": agent.script_entry}
        )

    return {
        "kind": "agent",
        "title": agent.name,
        "subtitle": subtitle,
        "sections": sections,
    }


def _template_info(tpl: AgentTemplate, spec: Spec) -> dict[str, Any]:
    subtitle = f"agent blueprint · {tpl.model}"
    sections: list[dict[str, Any]] = []
    if tpl.description:
        sections.append({"label": "Description", "kind": "text", "value": tpl.description})
    if tpl.parameters:
        sections.append({"label": "Parameters", "kind": "list", "value": list(tpl.parameters)})
    if tpl.tools:
        sections.append({"label": "Tools", "kind": "list", "value": list(tpl.tools)})
    if tpl.may_delegate_to:
        sections.append(
            {
                "label": "May delegate to",
                "kind": "list",
                "value": list(tpl.may_delegate_to),
            }
        )
    # Reshape rules that target this template (R4) or name it as caller.
    inst_rules = []
    for ch in spec.envelope.graph_reshape.permitted_changes:
        if isinstance(ch, InstantiateTemplateChange) and ch.template == tpl.name:
            callers = ch.for_callers or [a.name for a in spec.agents]
            inst_rules.append(
                {
                    "change": "instantiate_template",
                    "max_total": ch.max_total,
                    "for_callers": list(callers),
                    "requires_hitl": True,  # R4 always
                }
            )
    if inst_rules:
        sections.append(
            {"label": "R4 instantiation rules", "kind": "table", "value": inst_rules}
        )
    sections.append(
        {
            "label": "System prompt template",
            "kind": "code",
            "value": tpl.system_prompt_template,
        }
    )
    return {
        "kind": "template",
        "title": tpl.name,
        "subtitle": subtitle,
        "sections": sections,
    }


def _hitl_info(cp: Any) -> dict[str, Any]:
    sections: list[dict[str, Any]] = [
        {"label": "Pattern", "kind": "text", "value": cp.pattern},
        {"label": "Prompt", "kind": "text", "value": cp.prompt},
    ]
    if cp.when:
        sections.append(
            {
                "label": "Intercepts",
                "kind": "json",
                "value": cp.when.model_dump(exclude_none=True),
            }
        )
    if cp.condition:
        sections.append({"label": "Condition", "kind": "code", "value": cp.condition})
    if cp.trigger:
        sections.append({"label": "Trigger", "kind": "code", "value": cp.trigger})
    if cp.allowed_for:
        sections.append(
            {"label": "Allowed for", "kind": "list", "value": list(cp.allowed_for)}
        )
    return {
        "kind": "hitl",
        "title": cp.id,
        "subtitle": f"HITL gate · {cp.pattern}",
        "sections": sections,
    }


def _tool_info(tool: Tool, spec: Spec) -> dict[str, Any]:
    subtitle = tool.kind if tool.boundary == "internal" else f"{tool.kind} · {tool.boundary}"
    sections: list[dict[str, Any]] = []
    if tool.description:
        sections.append({"label": "Description", "kind": "text", "value": tool.description})
    if tool.boundary != "internal":
        sections.append({"label": "Boundary", "kind": "text", "value": tool.boundary})
    if tool.binds_to:
        sections.append(
            {"label": "Binds to data", "kind": "text", "value": tool.binds_to}
        )

    # MCP server / HTTP details (best effort — not every Tool field is
    # serialisable, so we lean on the pydantic dump).
    raw = tool.model_dump(exclude_none=True, exclude_defaults=True)
    raw.pop("name", None)
    raw.pop("kind", None)
    raw.pop("description", None)
    raw.pop("binds_to", None)
    raw.pop("remote_name", None)
    if raw:
        sections.append({"label": "Server config", "kind": "json", "value": raw})

    # HITL gates explicitly targeting this tool.
    hitl_rows: list[dict[str, Any]] = []
    for cp in spec.envelope.hitl_checkpoints:
        if cp.when and cp.when.tool == tool.name:
            hitl_rows.append(
                {
                    "checkpoint": cp.id,
                    "pattern": cp.pattern,
                    "prompt": cp.prompt,
                }
            )
    if hitl_rows:
        sections.append({"label": "HITL gates", "kind": "table", "value": hitl_rows})

    boundary_rows: list[dict[str, Any]] = []
    for policy in spec.envelope.boundary_policies:
        if policy.tool == tool.name:
            boundary_rows.append(
                {
                    "policy": policy.id,
                    "direction": policy.direction,
                    "action": policy.action,
                    "content_arg": policy.content_arg,
                    "patterns": len(policy.match),
                }
            )
    if boundary_rows:
        sections.append(
            {"label": "Boundary policies", "kind": "table", "value": boundary_rows}
        )

    # Agents and templates that have this tool in their tools list.
    callers = [a.name for a in spec.agents if tool.name in a.tools]
    template_callers = [t.name for t in spec.agent_templates if tool.name in t.tools]
    if callers or template_callers:
        sections.append(
            {
                "label": "Used by",
                "kind": "list",
                "value": callers + [f"{t} (template)" for t in template_callers],
            }
        )
    return {
        "kind": "tool",
        "title": tool.name,
        "subtitle": subtitle,
        "sections": sections,
    }


def _data_info(ds: DataSource, spec: Spec) -> dict[str, Any]:
    subtitle = f"{ds.kind} · {ds.sensitivity}"
    sections: list[dict[str, Any]] = []
    if ds.description:
        sections.append({"label": "Description", "kind": "text", "value": ds.description})
    if ds.path:
        sections.append({"label": "Path", "kind": "text", "value": ds.path})
    if ds.allowed_domains:
        sections.append(
            {"label": "Allowed domains", "kind": "list", "value": list(ds.allowed_domains)}
        )
    if ds.content_type:
        sections.append({"label": "Content type", "kind": "text", "value": ds.content_type})
    sections.append({"label": "Sensitivity", "kind": "text", "value": ds.sensitivity})
    if ds.tags:
        sections.append({"label": "Tags", "kind": "list", "value": list(ds.tags)})
    bound_by = [t.name for t in spec.tools if t.binds_to == ds.name]
    if bound_by:
        sections.append({"label": "Bound by tools", "kind": "list", "value": bound_by})
    return {
        "kind": "data",
        "title": ds.name,
        "subtitle": subtitle,
        "sections": sections,
    }


# ---------------------------------------------------------------------------


def _hitl_for_agent(agent: Agent, spec: Spec) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for cp in spec.envelope.hitl_checkpoints:
        applies = False
        if cp.pattern == "agent_initiated":
            allowed = getattr(cp, "allowed_for", None)
            if allowed and agent.name in allowed:
                applies = True
        elif cp.when and cp.when.tool and cp.when.tool in agent.tools:
            applies = True
        if applies:
            rows.append(
                {
                    "checkpoint": cp.id,
                    "pattern": cp.pattern,
                    "prompt": cp.prompt,
                }
            )
    return rows


def _reshape_for_caller(name: str, spec: Spec) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    all_agents = [a.name for a in spec.agents]
    for ch in spec.envelope.graph_reshape.permitted_changes:
        callers = ch.for_callers or all_agents
        if name not in callers:
            continue
        if isinstance(ch, SpawnInstanceChange):
            rows.append(
                {
                    "change": "spawn_instance",
                    "of_agent": ch.of_agent,
                    "max_concurrent": ch.max_concurrent,
                }
            )
        elif isinstance(ch, AcquireCapabilityChange):
            rows.append(
                {
                    "change": "acquire_capability",
                    "capability_kind": ch.capability_kind,
                    "capability_name": ch.capability_name,
                    "requires_hitl": ch.requires_hitl,
                }
            )
        elif isinstance(ch, InstantiateTemplateChange):
            rows.append(
                {
                    "change": "instantiate_template",
                    "template": ch.template,
                    "max_total": ch.max_total,
                }
            )
    return rows


def _read_prompt_file(ref: str, spec_dir: Path) -> str:
    candidate = Path(ref)
    if not candidate.is_absolute():
        for base in (spec_dir, Path.cwd()):
            p = (base / candidate).resolve()
            if p.is_file():
                candidate = p
                break
    try:
        return candidate.read_text(encoding="utf-8")
    except OSError:
        return f"(could not read {ref})"
