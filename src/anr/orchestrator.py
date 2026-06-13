"""Hybrid orchestrator (§3.2.1) with bounded graph reshape authority.

The orchestrator plays three roles at runtime:

  * Entry-point routing. Given an incoming task, the orchestrator decides
    which agent receives it. Deterministic regex routes take precedence,
    modelling the well-understood-workflow end of the §3.2.1 spectrum;
    anything that matches no route falls through to the entry agent,
    modelling the adaptive end.

  * Declared-agent spawning. When the mesh processes `delegate(...)` or
    `spawn_parallel(...)`, it calls the orchestrator to instantiate a
    fresh invocation of a declared agent.

  * Template instantiation (R4). When the mesh processes
    `instantiate_template(...)`, the orchestrator binds the template's
    parameters, builds an ephemeral NativeAgent on the fly with the
    template's tool scope, and runs it on the supplied task. The new
    agent is fully isolated; it dies when its `.run` returns.

In all cases, the orchestrator constructs the child InvocationContext
itself, populating `allowed_tools` and `allowed_delegation_targets` from
the appropriate blueprint. The mesh then enforces every subsequent
interaction against that ctx — never against the spec directly. This is
what lets template-instantiated agents share the mesh's enforcement
machinery uniformly with declared agents.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from rich.console import Console

from .agent import NativeAgent
from .agent_base import AgentNode
from .mesh import InvocationContext, Mesh
from .spec import AgentTemplate, Spec

console = Console(stderr=True)


@dataclass
class Orchestrator:
    spec: Spec
    mesh: Mesh
    # Concrete declared nodes, instantiated once at compile time.
    # Any AgentNode implementation is acceptable (native LLM loop,
    # script-backed, third-party-framework-backed, etc).
    agents: dict[str, AgentNode]
    # Compile-time per-agent + per-template authority sets, used to
    # populate every spawned ctx.
    agent_authority: dict[str, "AgentAuthority"] = field(default_factory=dict)
    template_authority: dict[str, "AgentAuthority"] = field(default_factory=dict)
    # Live counter of currently-running instances, per blueprint name.
    _live_instances: dict[str, int] = field(default_factory=dict)
    # Compact control-plane view of the mesh event stream. The visualizer
    # still reads the durable audit log; this is the orchestrator's
    # in-process memory for longer-horizon decisions in the prototype.
    _recent_mesh_events: list[dict[str, Any]] = field(default_factory=list)
    # Deterministic prototype control-plane policy: repeated boundary
    # interventions on the same caller/tool pair tighten that interaction
    # so future attempts require orchestrator-initiated human review.
    _boundary_interventions: dict[tuple[str, str], int] = field(default_factory=dict)
    _tightened_boundary_review: set[tuple[str, str]] = field(default_factory=set)

    async def handle(self, task: str) -> str:
        entry = self._choose_entry(task)
        console.log(f"[bold]orchestrator[/bold]: routing task to {entry!r}")
        ctx = self._build_ctx_for(
            blueprint=entry,
            task=task,
            parent_ctx=None,
            authority=self.agent_authority[entry],
        )
        return await self._run_with_lifecycle(entry, ctx, task)

    def observe_mesh_event(self, record: dict[str, Any]) -> None:
        self._recent_mesh_events.append(record)
        if len(self._recent_mesh_events) > 200:
            del self._recent_mesh_events[: len(self._recent_mesh_events) - 200]
        self._maybe_tighten_boundary(record)

    def requires_boundary_review(self, caller: str, tool: str) -> bool:
        return (caller, tool) in self._tightened_boundary_review

    # ---- spawn methods invoked by the mesh ---------------------------------

    async def spawn_declared(
        self, *, target: str, task: str, parent_ctx: InvocationContext
    ) -> Any:
        if target not in self.agents or target not in self.agent_authority:
            raise RuntimeError(
                f"orchestrator refused to spawn {target!r}: not a declared agent"
            )
        ctx = self._build_ctx_for(
            blueprint=target,
            task=task,
            parent_ctx=parent_ctx,
            authority=self.agent_authority[target],
        )
        console.log(
            f"[bold]orchestrator[/bold]: spawning {ctx.instance_id!r} "
            f"(parent={parent_ctx.instance_id}, depth={ctx.delegation_depth})"
        )
        return await self._run_with_lifecycle(target, ctx, task)

    async def spawn_template_instance(
        self,
        *,
        template: str,
        parameters: dict[str, Any],
        task: str,
        parent_ctx: InvocationContext,
    ) -> Any:
        if not self.spec.envelope.graph_reshape.allowed:
            raise RuntimeError("graph reshape disabled in this application's envelope")
        try:
            tpl = self.spec.template(template)
        except KeyError:
            raise RuntimeError(
                f"orchestrator refused to instantiate {template!r}: not a declared template"
            )

        # Validate parameters against the template's closed parameter set.
        missing = [p for p in tpl.parameters if p not in parameters]
        extra = [k for k in parameters if k not in tpl.parameters]
        if missing or extra:
            raise RuntimeError(
                f"template {template!r} parameter mismatch: "
                f"missing={missing} extra={extra}"
            )

        # Bind the prompt. We use str.format which only substitutes
        # declared parameter names — no escapes, no expressions. Any
        # rogue `{...}` in the template that doesn't match a parameter
        # would raise KeyError, surfaced as a tool error to the caller.
        try:
            bound_prompt = tpl.system_prompt_template.format(**parameters)
        except KeyError as e:
            raise RuntimeError(
                f"template {template!r}: prompt references unknown placeholder {e}"
            )

        # Build an ephemeral NativeAgent for this one task. Its tool
        # schemas come from the template's declared scope; the compiler
        # has already built the schema lookup we use here.
        instance_agent = NativeAgent(
            spec=self.spec,
            declaration=_TemplateDecl(name=template, model=tpl.model),
            mesh=self.mesh,
            tool_schemas=self._compose_template_tool_schemas(tpl),
            system_prompt=bound_prompt,
        )

        authority = self.template_authority[template]
        ctx = self._build_ctx_for(
            blueprint=template,
            task=task,
            parent_ctx=parent_ctx,
            authority=authority,
        )
        console.log(
            f"[bold]orchestrator[/bold]: instantiated template {template!r} "
            f"as {ctx.instance_id!r} (params={parameters})"
        )

        # Lifecycle: register, run, deregister.
        self._live_instances[template] = self._live_instances.get(template, 0) + 1
        try:
            return await instance_agent.run(task, ctx)
        finally:
            self._live_instances[template] = self._live_instances.get(template, 1) - 1

    # ---- helpers -----------------------------------------------------------

    async def _run_with_lifecycle(
        self, blueprint_name: str, ctx: InvocationContext, task: str
    ) -> Any:
        self._live_instances[blueprint_name] = (
            self._live_instances.get(blueprint_name, 0) + 1
        )
        try:
            return await self.agents[blueprint_name].run(task, ctx)
        finally:
            self._live_instances[blueprint_name] = (
                self._live_instances.get(blueprint_name, 1) - 1
            )

    def _build_ctx_for(
        self,
        *,
        blueprint: str,
        task: str,
        parent_ctx: InvocationContext | None,
        authority: "AgentAuthority",
    ) -> InvocationContext:
        return InvocationContext(
            caller=blueprint,
            instance_id=InvocationContext.make_instance_id(blueprint),
            delegation_depth=(parent_ctx.delegation_depth + 1) if parent_ctx else 0,
            root_task=parent_ctx.root_task if parent_ctx else task,
            parent=parent_ctx.caller if parent_ctx else None,
            parent_instance_id=parent_ctx.instance_id if parent_ctx else None,
            allowed_tools=set(authority.tools),
            allowed_delegation_targets=set(authority.delegation_targets),
        )

    def _compose_template_tool_schemas(
        self, tpl: AgentTemplate
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for tname in tpl.tools:
            schema = self.mesh.tool_schemas.get(tname)
            if schema is not None:
                out.append(schema)
        # Templates can also call request_human_review if the spec admits it
        # generally; the compiler decides what to expose for declared agents
        # but for templates we keep it minimal — only the declared tools and
        # request_human_review.
        out.append(_HUMAN_REVIEW_SCHEMA)
        return out

    def _choose_entry(self, task: str) -> str:
        for route in self.spec.orchestrator.deterministic_routes:
            if re.search(route.input_regex, task):
                return route.route_to
        return self.spec.orchestrator.default_entry

    def _maybe_tighten_boundary(self, record: dict[str, Any]) -> None:
        if record.get("kind") != "boundary_decision":
            return
        outcome = (record.get("outcome") or {}).get("enforcement_outcome")
        if outcome not in {"block", "sanitize", "escalate_to_human"}:
            return
        caller = str(record.get("caller") or "")
        tool = str(record.get("tool") or "")
        if not caller or not tool:
            return
        key = (caller, tool)
        count = self._boundary_interventions.get(key, 0) + 1
        self._boundary_interventions[key] = count
        if count < 2 or key in self._tightened_boundary_review:
            return

        self._tightened_boundary_review.add(key)
        self.mesh.audit.write(
            kind="orchestrator_decision",
            caller=caller,
            instance_id=record.get("instance_id"),
            parent=record.get("parent"),
            parent_instance_id=record.get("parent_instance_id"),
            delegation_depth=record.get("delegation_depth"),
            tool=tool,
            args={},
            outcome={
                "ok": True,
                "value_preview": (
                    "tightened boundary review after repeated mesh interventions"
                ),
            },
            decision="tighten_boundary_review",
            reason=(
                f"{count} boundary interventions observed for "
                f"{caller}.{tool}"
            ),
            trigger_event={
                "policy_id": record.get("policy_id"),
                "direction": record.get("direction"),
                "enforcement_outcome": outcome,
            },
            totals=record.get("totals") or {},
            mesh_context=record.get("mesh_context") or {},
            control_plane_context={
                "boundary_intervention_count": count,
                "future_effect": "require_human_review",
            },
        )


# ---------------------------------------------------------------------------
# Authority records and a synthetic "declaration" stub for templates
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AgentAuthority:
    """The compile-time view of what an agent or template is allowed to do.

    Used by the orchestrator to populate every spawned InvocationContext's
    `allowed_tools` / `allowed_delegation_targets` sets, which the mesh
    then enforces.
    """

    tools: tuple[str, ...]
    delegation_targets: tuple[str, ...]


@dataclass
class _TemplateDecl:
    """Minimal stand-in for spec.Agent so NativeAgent works for templates too."""

    name: str
    model: str


_HUMAN_REVIEW_SCHEMA = {
    "type": "function",
    "function": {
        "name": "request_human_review",
        "description": (
            "Pause execution and request a human reviewer's judgement. Use "
            "sparingly — only when you genuinely cannot proceed without "
            "human input."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "reason": {"type": "string", "description": "Why you need a human."},
            },
            "required": ["reason"],
        },
    },
}
