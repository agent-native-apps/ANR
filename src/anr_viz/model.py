"""Structural graph model derived from the declarative spec.

The model is purely static: it reflects what the spec declares, not what
is currently running. The LiveState layer (anr_viz/state.py) decorates
this model with dynamic activity drawn from the audit log.

Node kinds rendered:

  * agent   — declared LLM-backed or script-backed worker (spec.agents)
  * template — declared blueprint for R4 runtime instantiation
  * tool    — declared tool (MCP or HTTP)
  * data    — declared data source
  * hitl    — declared HITL checkpoint (appears as a small marker / gate)

Edges rendered:

  * tool_binding       — from agent to each tool in its `tools` list
  * delegation_edge    — from agent to each target in `may_delegate_to`
  * data_binding       — from tool to its `binds_to` data source
  * hitl_gate          — from a HITL checkpoint to the tool it guards
  * reshape_potential  — from a caller agent to the entity it MAY spawn /
                         acquire / instantiate, per envelope permissions
                         (dashed, shown only to indicate possibility)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from anr.spec import (
    AcquireCapabilityChange,
    InstantiateTemplateChange,
    SpawnInstanceChange,
    Spec,
)

NodeKind = Literal["agent", "template", "tool", "data", "hitl"]
EdgeKind = Literal[
    "tool_binding",
    "delegation_edge",
    "data_binding",
    "hitl_gate",
    "reshape_spawn",
    "reshape_acquire",
    "reshape_template",
]


@dataclass
class Node:
    id: str
    kind: NodeKind
    label: str
    sublabel: str = ""
    # Tags that drive styling (e.g. "agent:native", "agent:script", "tool:http").
    tags: list[str] = field(default_factory=list)


@dataclass
class Edge:
    id: str
    src: str
    dst: str
    kind: EdgeKind
    label: str = ""


@dataclass
class GraphModel:
    nodes: list[Node]
    edges: list[Edge]

    def node(self, node_id: str) -> Node | None:
        for n in self.nodes:
            if n.id == node_id:
                return n
        return None


# ---------------------------------------------------------------------------


def build_model(spec: Spec) -> GraphModel:
    nodes: list[Node] = []
    edges: list[Edge] = []

    for ds in spec.data_sources:
        nodes.append(
            Node(
                id=f"data:{ds.name}",
                kind="data",
                label=ds.name,
                sublabel=ds.kind,
                tags=[f"data:{ds.kind}"],
            )
        )

    for tool in spec.tools:
        nodes.append(
            Node(
                id=f"tool:{tool.name}",
                kind="tool",
                label=tool.name,
                sublabel=tool.kind,
                tags=[f"tool:{tool.kind}"],
            )
        )
        if tool.binds_to:
            edges.append(
                Edge(
                    id=f"bind:{tool.name}->{tool.binds_to}",
                    src=f"tool:{tool.name}",
                    dst=f"data:{tool.binds_to}",
                    kind="data_binding",
                )
            )

    for agent in spec.agents:
        entry = agent.role == "entry_point"
        tags = [f"agent:{agent.kind}"]
        if entry:
            tags.append("entry")
        nodes.append(
            Node(
                id=f"agent:{agent.name}",
                kind="agent",
                label=agent.name,
                sublabel=(agent.model or agent.kind),
                tags=tags,
            )
        )
        for t in agent.tools:
            edges.append(
                Edge(
                    id=f"tool_binding:{agent.name}->{t}",
                    src=f"agent:{agent.name}",
                    dst=f"tool:{t}",
                    kind="tool_binding",
                )
            )
        for target in agent.may_delegate_to:
            target_prefix = (
                "template:"
                if any(tpl.name == target for tpl in spec.agent_templates)
                else "agent:"
            )
            edges.append(
                Edge(
                    id=f"deleg:{agent.name}->{target}",
                    src=f"agent:{agent.name}",
                    dst=f"{target_prefix}{target}",
                    kind="delegation_edge",
                )
            )

    for tpl in spec.agent_templates:
        nodes.append(
            Node(
                id=f"template:{tpl.name}",
                kind="template",
                label=tpl.name,
                sublabel="agent blueprint",
                tags=["template"],
            )
        )
        for t in tpl.tools:
            edges.append(
                Edge(
                    id=f"tool_binding:tpl:{tpl.name}->{t}",
                    src=f"template:{tpl.name}",
                    dst=f"tool:{t}",
                    kind="tool_binding",
                )
            )

    # Each HITL checkpoint becomes a small marker node, attached via a
    # reshape-style dotted edge to the tool (or agent) it guards.
    for cp in spec.envelope.hitl_checkpoints:
        nodes.append(
            Node(
                id=f"hitl:{cp.id}",
                kind="hitl",
                label=cp.id,
                sublabel=cp.pattern,
                tags=[f"hitl:{cp.pattern}"],
            )
        )
        target = cp.when.tool if (cp.when and cp.when.tool) else None
        if target:
            edges.append(
                Edge(
                    id=f"hitl_bind:{cp.id}",
                    src=f"hitl:{cp.id}",
                    dst=f"tool:{target}",
                    kind="hitl_gate",
                )
            )

    # Reshape-potential edges (dashed in the renderer): show what each
    # caller MAY do under the envelope. These are not active edges — they
    # light up when a reshape event fires at runtime.
    for ch in spec.envelope.graph_reshape.permitted_changes:
        callers = ch.for_callers or [a.name for a in spec.agents]
        for caller in callers:
            if isinstance(ch, SpawnInstanceChange):
                edges.append(
                    Edge(
                        id=f"reshape_spawn:{caller}->{ch.of_agent}",
                        src=f"agent:{caller}",
                        dst=f"agent:{ch.of_agent}",
                        kind="reshape_spawn",
                        label=f"×{ch.max_concurrent}",
                    )
                )
            elif isinstance(ch, AcquireCapabilityChange):
                if ch.capability_kind == "tool":
                    edges.append(
                        Edge(
                            id=f"reshape_acq:{caller}->{ch.capability_name}",
                            src=f"agent:{caller}",
                            dst=f"tool:{ch.capability_name}",
                            kind="reshape_acquire",
                            label="grant",
                        )
                    )
                else:
                    edges.append(
                        Edge(
                            id=f"reshape_acq_d:{caller}->{ch.capability_name}",
                            src=f"agent:{caller}",
                            dst=f"agent:{ch.capability_name}",
                            kind="reshape_acquire",
                            label="grant→delegate",
                        )
                    )
            elif isinstance(ch, InstantiateTemplateChange):
                edges.append(
                    Edge(
                        id=f"reshape_tpl:{caller}->{ch.template}",
                        src=f"agent:{caller}",
                        dst=f"template:{ch.template}",
                        kind="reshape_template",
                        label=f"×{ch.max_total}",
                    )
                )

    return GraphModel(nodes=nodes, edges=edges)
