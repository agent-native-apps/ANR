"""Typed models for the declarative application specification.

The spec mirrors the paper's §3.1.2 structure-vs-envelope split:

  - application structure: data sources, tools, agents, agent templates, and
                           their predefined edges
  - behavioral envelope:   autonomy bounds, resource limits, HITL checkpoints,
                           graph-reshape authority (the four R-kinds)
  - orchestrator:          hybrid routing configuration (§3.2.1)

A loaded Spec is the static authoring artifact. The compiler turns it into a
running Graph of mesh-wrapped runtime objects, and the orchestrator may
reshape that graph at runtime within the bounds the envelope permits.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, Field, model_validator

# Pseudo-tools the mesh exposes to agents directly. They are never declared
# in the spec; they appear here so cross-reference checks know to allow them
# in `agents[].tools` lists if a spec author lists them explicitly.
PSEUDO_TOOLS: frozenset[str] = frozenset(
    {
        "delegate",
        "request_human_review",
        "spawn_parallel",
        "request_grant",
        "instantiate_template",
    }
)


# ---------------------------------------------------------------------------
# Application structure
# ---------------------------------------------------------------------------


class DataSource(BaseModel):
    name: str
    kind: Literal["local_directory", "http"]
    description: str = ""
    path: str | None = None
    allowed_domains: list[str] = Field(default_factory=list)
    content_type: str | None = None
    sensitivity: Literal["public", "internal", "confidential", "restricted"] = "internal"
    tags: list[str] = Field(default_factory=list)


class MCPServerConfig(BaseModel):
    command: str
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)


class Tool(BaseModel):
    name: str
    kind: Literal["mcp", "http", "builtin"]
    description: str = ""
    boundary: Literal["internal", "ingress", "egress"] = "internal"
    # MCP-specific:
    server: MCPServerConfig | None = None
    remote_name: str | None = None
    binds_to: str | None = None
    # HTTP-specific:
    url: str | None = None
    method: Literal["GET", "POST", "PUT", "DELETE", "PATCH"] = "GET"
    input_schema: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _check_kind_fields(self) -> "Tool":
        if self.kind == "mcp" and self.server is None:
            raise ValueError(f"tool {self.name!r}: kind=mcp requires a 'server' block")
        if self.kind == "http" and not self.url:
            raise ValueError(f"tool {self.name!r}: kind=http requires a 'url'")
        if self.kind == "http" and self.input_schema is None:
            raise ValueError(
                f"tool {self.name!r}: kind=http requires an 'input_schema' "
                f"(agents need a parameter schema; no remote list_tools for http)"
            )
        return self


class Agent(BaseModel):
    name: str
    # Which node builder instantiates this agent. 'native' is the default
    # LLM-backed runtime; 'script' maps the node to a Python callable so
    # deterministic or third-party-framework-backed workers can sit inside
    # the same envelope and mesh as LLM-backed ones.
    kind: Literal["native", "script"] = "native"
    model: str | None = None
    role: Literal["entry_point", "worker"] = "worker"
    # Required for kind=native; unused for kind=script.
    system_prompt_file: str | None = None
    # Required for kind=script: 'module.path:callable_name'. The callable
    # receives (task, ctx, mesh) and returns a string result.
    script_entry: str | None = None
    tools: list[str] = Field(default_factory=list)
    may_delegate_to: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_kind_fields(self) -> "Agent":
        if self.kind == "native":
            if not self.model:
                raise ValueError(
                    f"agent {self.name!r}: kind=native requires a 'model'"
                )
            if not self.system_prompt_file:
                raise ValueError(
                    f"agent {self.name!r}: kind=native requires a 'system_prompt_file'"
                )
            if self.script_entry is not None:
                raise ValueError(
                    f"agent {self.name!r}: kind=native must not set 'script_entry'"
                )
        elif self.kind == "script":
            if not self.script_entry or ":" not in self.script_entry:
                raise ValueError(
                    f"agent {self.name!r}: kind=script requires 'script_entry' "
                    f"in 'module.path:callable' form"
                )
        return self


class AgentTemplate(BaseModel):
    """A blueprint for a runtime-instantiated agent (R4 graph reshape).

    Templates are pre-declared at spec time; their `system_prompt_template`
    contains named `{placeholder}` markers that are bound to caller-provided
    values at instantiation. The set of permitted parameter names is closed
    by the `parameters` list, so the caller cannot inject arbitrary keys.
    """

    name: str
    model: str
    description: str = ""
    system_prompt_template: str
    parameters: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    may_delegate_to: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Behavioral envelope
# ---------------------------------------------------------------------------


class AutonomyLimits(BaseModel):
    max_sub_agents: int = 0
    max_delegation_depth: int = 1
    max_tool_calls: int = 20


class ResourceLimits(BaseModel):
    total_cost_usd: float = 1.0
    total_runtime_sec: int = 120
    total_llm_calls: int = 40


HITLPattern = Literal[
    "predefined",
    "conditional",
    "agent_initiated",
    "platform_initiated",
]


class HITLWhen(BaseModel):
    tool: str | None = None
    caller: str | None = None


class HITLCheckpoint(BaseModel):
    """One HITL checkpoint. The four §3.2.2 patterns are distinguished by 'pattern'."""

    id: str
    pattern: HITLPattern
    prompt: str = "Approve this action?"
    when: HITLWhen | None = None
    condition: str | None = None
    trigger: str | None = None
    allowed_for: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_pattern_fields(self) -> "HITLCheckpoint":
        if self.pattern == "predefined" and self.when is None:
            raise ValueError(f"checkpoint {self.id!r}: predefined pattern requires 'when'")
        if self.pattern == "conditional" and (self.when is None or not self.condition):
            raise ValueError(
                f"checkpoint {self.id!r}: conditional pattern requires 'when' and 'condition'"
            )
        if self.pattern == "platform_initiated" and not self.trigger:
            raise ValueError(
                f"checkpoint {self.id!r}: platform_initiated pattern requires 'trigger'"
            )
        return self


class BoundaryPolicy(BaseModel):
    """Mesh policy for messages crossing the application boundary.

    This is a compact prototype of the paper's ingress/egress boundary
    enforcement role. A policy attaches to one tool and inspects one string
    argument and/or the caller's data-access context. Pattern or context
    matches trigger one of the mesh's four outcomes: allow, block, sanitize,
    or escalate_to_human.
    """

    id: str
    direction: Literal["ingress", "egress"]
    tool: str
    content_arg: str = "message"
    match: list[str] = Field(default_factory=list)
    data_sensitivity_at_least: Literal[
        "public", "internal", "confidential", "restricted"
    ] | None = None
    accessed_data_tags_any: list[str] = Field(default_factory=list)
    action: Literal["allow", "block", "sanitize", "escalate_to_human"] = "block"
    replacement: str = "[REDACTED]"
    prompt: str = "Boundary policy matched. Approve this exchange?"
    message: str = "boundary policy matched"
    for_callers: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_policy(self) -> "BoundaryPolicy":
        has_context_trigger = (
            self.data_sensitivity_at_least is not None
            or bool(self.accessed_data_tags_any)
        )
        if self.action != "allow" and not self.match and not has_context_trigger:
            raise ValueError(
                f"boundary policy {self.id!r}: action={self.action!r} requires "
                "at least one match pattern or data-context trigger"
            )
        if self.action == "sanitize" and not self.match:
            raise ValueError(
                f"boundary policy {self.id!r}: action='sanitize' requires "
                "at least one match pattern to redact"
            )
        return self


# ----- Graph-reshape rules (R1, R2/R3, R4) ---------------------------------


class SpawnInstanceChange(BaseModel):
    """R1: permission to spawn additional concurrent instances of a declared agent."""

    kind: Literal["spawn_instance"]
    of_agent: str
    max_concurrent: int = 3
    for_callers: list[str] = Field(default_factory=list)


class AcquireCapabilityChange(BaseModel):
    """R2/R3: permission for an agent to acquire a tool or delegation target at runtime."""

    kind: Literal["acquire_capability"]
    capability_kind: Literal["tool", "delegation_target"]
    capability_name: str
    for_callers: list[str] = Field(default_factory=list)
    requires_hitl: bool = True
    reason_required: bool = True


class InstantiateTemplateChange(BaseModel):
    """R4: permission to instantiate a templated agent at runtime.

    The mesh ALWAYS fires HITL on this kind regardless of the rule — the
    risk surface (LLM-controlled prompt construction within a closed
    parameter set) makes mandatory human approval the right default. This
    is intentionally not configurable in the YAML.
    """

    kind: Literal["instantiate_template"]
    template: str
    max_total: int = 5
    for_callers: list[str] = Field(default_factory=list)


PermittedChange = Annotated[
    Union[SpawnInstanceChange, AcquireCapabilityChange, InstantiateTemplateChange],
    Field(discriminator="kind"),
]


class GraphReshape(BaseModel):
    allowed: bool = True
    constraint: str = (
        "orchestrator may enact any change listed in permitted_changes, "
        "subject to per-rule HITL and per-rule limits"
    )
    permitted_changes: list[PermittedChange] = Field(default_factory=list)


class Envelope(BaseModel):
    autonomy: dict[str, AutonomyLimits] = Field(default_factory=dict)
    resource_limits: ResourceLimits = Field(default_factory=ResourceLimits)
    hitl_checkpoints: list[HITLCheckpoint] = Field(default_factory=list)
    boundary_policies: list[BoundaryPolicy] = Field(default_factory=list)
    graph_reshape: GraphReshape = Field(default_factory=GraphReshape)


# ---------------------------------------------------------------------------
# Orchestrator (§3.2.1 hybrid)
# ---------------------------------------------------------------------------


class DeterministicRoute(BaseModel):
    input_regex: str
    route_to: str


class Orchestrator(BaseModel):
    kind: Literal["hybrid", "deterministic", "agentic"] = "hybrid"
    deterministic_routes: list[DeterministicRoute] = Field(default_factory=list)
    default_entry: str


# ---------------------------------------------------------------------------
# Top-level spec
# ---------------------------------------------------------------------------


class ApplicationMeta(BaseModel):
    name: str
    version: str = "0.1"
    # Optional onboarding metadata. `description` is a one-line summary shown
    # by `anr list`; `example_task` is a ready-to-run task used when `anr run`
    # is invoked without one.
    description: str = ""
    example_task: str = ""


_SUPPORTED_SPEC_VERSIONS: frozenset[str] = frozenset({"0.1"})


class Spec(BaseModel):
    # The version of the agent-native application specification format this
    # document targets. A conforming runtime validates against the JSON
    # Schema for this version (emitted by `python -m anr.schema`). The schema
    # is the portable contract; anr's pydantic models are one implementation
    # of it.
    spec_version: str = "0.1"
    application: ApplicationMeta
    data_sources: list[DataSource] = Field(default_factory=list)
    tools: list[Tool] = Field(default_factory=list)
    agents: list[Agent]
    agent_templates: list[AgentTemplate] = Field(default_factory=list)
    envelope: Envelope = Field(default_factory=Envelope)
    orchestrator: Orchestrator

    @model_validator(mode="after")
    def _check_spec_version(self) -> "Spec":
        if self.spec_version not in _SUPPORTED_SPEC_VERSIONS:
            raise ValueError(
                f"spec_version {self.spec_version!r} is not supported by this runtime; "
                f"known versions: {sorted(_SUPPORTED_SPEC_VERSIONS)}"
            )
        return self

    @model_validator(mode="after")
    def _validate_references(self) -> "Spec":
        tool_names = {t.name for t in self.tools}
        agent_names = {a.name for a in self.agents}
        template_names = {t.name for t in self.agent_templates}
        all_callers = agent_names | template_names

        if agent_names & template_names:
            raise ValueError(
                f"agent and template names must be disjoint; overlap: {agent_names & template_names}"
            )

        for agent in self.agents:
            for tool in agent.tools:
                if tool not in tool_names and tool not in PSEUDO_TOOLS:
                    raise ValueError(
                        f"agent {agent.name!r} references unknown tool {tool!r}"
                    )
            for target in agent.may_delegate_to:
                if target not in agent_names and target not in template_names:
                    raise ValueError(
                        f"agent {agent.name!r} may_delegate_to references unknown agent/template {target!r}"
                    )

        data_source_names = {ds.name for ds in self.data_sources}
        for tool in self.tools:
            if tool.binds_to and tool.binds_to not in data_source_names:
                raise ValueError(
                    f"tool {tool.name!r} binds_to unknown data source {tool.binds_to!r}"
                )

        for tpl in self.agent_templates:
            for tool in tpl.tools:
                if tool not in tool_names and tool not in PSEUDO_TOOLS:
                    raise ValueError(
                        f"template {tpl.name!r} references unknown tool {tool!r}"
                    )
            # Make sure every parameter actually appears as {param} in the prompt.
            for p in tpl.parameters:
                if "{" + p + "}" not in tpl.system_prompt_template:
                    raise ValueError(
                        f"template {tpl.name!r}: parameter {p!r} declared but not referenced in system_prompt_template"
                    )

        if self.orchestrator.default_entry not in agent_names:
            raise ValueError(
                f"orchestrator.default_entry {self.orchestrator.default_entry!r} is not a declared agent"
            )

        for route in self.orchestrator.deterministic_routes:
            if route.route_to not in agent_names:
                raise ValueError(
                    f"orchestrator route targets unknown agent {route.route_to!r}"
                )

        for name in self.envelope.autonomy:
            if name not in agent_names and name not in template_names:
                raise ValueError(
                    f"envelope.autonomy references unknown agent/template {name!r}"
                )

        for policy in self.envelope.boundary_policies:
            if policy.tool not in tool_names:
                raise ValueError(
                    f"boundary policy {policy.id!r} references unknown tool {policy.tool!r}"
                )
            tool = self.tool(policy.tool)
            if tool.boundary != policy.direction:
                raise ValueError(
                    f"boundary policy {policy.id!r} direction={policy.direction!r} "
                    f"does not match tool {policy.tool!r} boundary={tool.boundary!r}"
                )
            for caller in policy.for_callers:
                if caller not in all_callers:
                    raise ValueError(
                        f"boundary policy {policy.id!r} references unknown caller {caller!r}"
                    )

        # Cross-check graph_reshape.permitted_changes
        for ch in self.envelope.graph_reshape.permitted_changes:
            for c in ch.for_callers:
                if c not in all_callers:
                    raise ValueError(
                        f"permitted_change references unknown caller {c!r}"
                    )
            if isinstance(ch, SpawnInstanceChange):
                if ch.of_agent not in agent_names:
                    raise ValueError(
                        f"spawn_instance.of_agent {ch.of_agent!r} is not a declared agent"
                    )
            elif isinstance(ch, AcquireCapabilityChange):
                if ch.capability_kind == "tool" and ch.capability_name not in tool_names:
                    raise ValueError(
                        f"acquire_capability.capability_name {ch.capability_name!r} is not a declared tool"
                    )
                if ch.capability_kind == "delegation_target" and ch.capability_name not in all_callers:
                    raise ValueError(
                        f"acquire_capability.capability_name {ch.capability_name!r} is not a declared agent or template"
                    )
            elif isinstance(ch, InstantiateTemplateChange):
                if ch.template not in template_names:
                    raise ValueError(
                        f"instantiate_template.template {ch.template!r} is not a declared agent template"
                    )

        return self

    def agent(self, name: str) -> Agent:
        for a in self.agents:
            if a.name == name:
                return a
        raise KeyError(name)

    def template(self, name: str) -> AgentTemplate:
        for t in self.agent_templates:
            if t.name == name:
                return t
        raise KeyError(name)

    def tool(self, name: str) -> Tool:
        for t in self.tools:
            if t.name == name:
                return t
        raise KeyError(name)

    def autonomy_for(self, agent_name: str) -> AutonomyLimits:
        return self.envelope.autonomy.get(agent_name, AutonomyLimits())
