"""The agent mesh — every agent-initiated interaction flows through here.

This module is the concrete realisation of §3.2.2. The argument the paper
makes is that safety-critical constraints (permissions, HITL, audit,
resource bounds) must be enforced by infrastructure that operates
independently of agent behaviour, because agents cannot be trusted to
self-enforce. The mesh is that infrastructure.

Agents never call tools, delegate, request grants, spawn instances, or
instantiate templates directly — they call `Mesh.invoke(...)` and the
mesh does one of:

  * rejects the interaction (policy violation, budget exhausted, HITL
    rejected) and returns an error the agent's LLM loop will see;
  * fires one or more HITL checkpoints, then continues with approved args;
  * forwards a regular tool call through MCP to the corresponding server;
  * routes a delegate / spawn_parallel / instantiate_template call to the
    orchestrator;
  * grants a tool or delegation target to the live invocation context
    (R2/R3 graph reshape);
  * accepts request_human_review as a pure signal to the mesh.

Every path emits at least one audit record so the JSONL log tells the
full story of the run.
"""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from rich.console import Console

from . import hitl, policies
from .audit import AuditLog
from .mcp_client import MCPPool
from .spec import (
    AcquireCapabilityChange,
    BoundaryPolicy,
    InstantiateTemplateChange,
    SpawnInstanceChange,
    Spec,
    DataSource,
    Tool,
)

if TYPE_CHECKING:
    from .orchestrator import Orchestrator

console = Console(stderr=True)

# Pseudo-tools whose dispatch awaits a nested sub-agent run. For these the
# mesh emits a `tool_call_start` record *before* the await so the audit log
# (and the visualizer's trace) reads in causal order rather than reverse —
# without this, every child event lands in the log before the parent's
# delegate / spawn / instantiate row that caused it.
_SPAWNING_TOOLS: frozenset[str] = frozenset(
    {"delegate", "spawn_parallel", "instantiate_template"}
)


# ---------------------------------------------------------------------------
# Per-invocation state
# ---------------------------------------------------------------------------


@dataclass
class Grant:
    """A capability granted at runtime to a single live invocation (R2/R3)."""

    kind: Literal["tool", "delegation_target"]
    name: str
    reason: str = ""
    tool_schema: dict[str, Any] | None = None  # populated for kind="tool"


@dataclass
class InvocationContext:
    """Per-invocation context the mesh uses for scoping, audit, and grants.

    Every spawn produces a fresh InvocationContext. The fields fall into two
    groups: identity (caller, instance_id, parent, depth) and authority
    (allowed_tools, allowed_delegation_targets, granted_capabilities).

    Permissions are checked against this context, never against the spec
    directly. That decoupling is what lets template-instantiated agents
    work uniformly with declared agents — the orchestrator populates the
    authority fields from whichever blueprint the instance came from.
    """

    caller: str
    instance_id: str = ""
    delegation_depth: int = 0
    root_task: str = ""
    parent: str | None = None
    parent_instance_id: str | None = None
    allowed_tools: set[str] = field(default_factory=set)
    allowed_delegation_targets: set[str] = field(default_factory=set)
    granted_capabilities: list[Grant] = field(default_factory=list)
    accessed_data_sources: set[str] = field(default_factory=set)
    accessed_data_tags: set[str] = field(default_factory=set)
    max_data_sensitivity: str = "public"

    @staticmethod
    def make_instance_id(caller: str) -> str:
        return f"{caller}#{uuid.uuid4().hex[:6]}"


# ---------------------------------------------------------------------------
# Mesh
# ---------------------------------------------------------------------------


@dataclass
class Mesh:
    spec: Spec
    mcp_pool: MCPPool
    audit: AuditLog
    # Tool schemas keyed by spec tool name — used to build effective tool
    # lists when a grant adds a tool to a live invocation.
    tool_schemas: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Wired by the compiler after the orchestrator is constructed.
    orchestrator: "Orchestrator | None" = None
    # Prompter used for every HITL checkpoint. Defaults to the stdin
    # backend; the compiler swaps in UIPrompter when ANR_HITL=ui so the
    # operator can decide via the visualizer.
    prompter: hitl.Prompter = field(default_factory=hitl.StdinPrompter)
    # Live run state
    totals: policies.ResourceTotals = field(default_factory=policies.ResourceTotals)
    _started_at: float = field(default_factory=time.monotonic)
    # Time spent suspended on HITL prompts. Subtracted from elapsed_sec so
    # the global total_runtime_sec budget measures *computation* time, not
    # how long the human took to click. Without this, a spec with a tight
    # runtime budget becomes unusable in UI HITL mode.
    _hitl_wait_sec: float = 0.0
    # Per-instance tool-call counter, keyed by instance_id. Per-instance
    # rather than per-blueprint so parallel R1 spawns and R4 template
    # instantiations each get their own max_tool_calls budget — what
    # spec authors intuitively expect from `autonomy[X].max_tool_calls`.
    _per_instance_tool_calls: dict[str, int] = field(default_factory=dict)
    _tool_by_name: dict[str, Tool] = field(default_factory=dict)
    _data_source_by_name: dict[str, DataSource] = field(default_factory=dict)
    # Reshape bookkeeping for envelope budgets.
    _template_instantiations: dict[str, int] = field(default_factory=dict)
    # Platform-initiated checkpoints fire at most once per run. Without
    # this guard a threshold-crossed trigger re-prompts on every
    # subsequent invoke, which is just noise — the state of the run has
    # already been surfaced to the human.
    _fired_platform: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        self._tool_by_name = {t.name: t for t in self.spec.tools}
        self._data_source_by_name = {d.name: d for d in self.spec.data_sources}

    # ---- public API --------------------------------------------------------

    async def invoke(
        self,
        *,
        tool: str,
        arguments: dict[str, Any],
        ctx: InvocationContext,
    ) -> dict[str, Any]:
        """Run a single agent-initiated interaction end to end.

        Returns a plain dict the caller can embed in a tool result. Success
        returns {"ok": True, "value": ...}; a refused or failed call returns
        {"ok": False, "error": "..."}.
        """
        arguments = dict(arguments or {})
        self._refresh_elapsed()

        # Permission gate (ctx-driven; no spec lookup here).
        try:
            policies.check_resource_limits(self.spec, self.totals)
            policies.check_tool_permission(ctx, tool)
            policies.check_agent_tool_budget(
                self.spec,
                ctx.caller,
                ctx.instance_id,
                self._per_instance_tool_calls,
            )
        except policies.PolicyError as e:
            self._record(
                kind="policy_refusal",
                ctx=ctx,
                tool=tool,
                args=arguments,
                outcome={"ok": False, "error": str(e)},
            )
            return {"ok": False, "error": str(e)}

        boundary = await self._apply_boundary_policies(
            tool, arguments, ctx, direction="egress"
        )
        if boundary is not None:
            self._record_boundary_decision(boundary, ctx=ctx, tool=tool)
            if boundary.outcome == "block":
                return {"ok": False, "error": boundary.message}
            arguments = boundary.arguments

        if self.orchestrator is not None and self.orchestrator.requires_boundary_review(
            ctx.caller, tool
        ):
            decision = await self._prompt(
                checkpoint_id=f"orchestrator:boundary_review:{ctx.caller}:{tool}",
                pattern="orchestrator_initiated",
                prompt_text=(
                    "The orchestrator has tightened review for this boundary "
                    "interaction after repeated prior interventions. Approve "
                    "this exchange?"
                ),
                caller=ctx.caller,
                tool=tool,
                args=arguments,
                extra="control_plane_decision=tighten_boundary_review",
            )
            self._record(
                kind="hitl_checkpoint",
                ctx=ctx,
                tool=tool,
                args=arguments,
                checkpoint_id=f"orchestrator:boundary_review:{ctx.caller}:{tool}",
                pattern="orchestrator_initiated",
                decision=decision.action,
                note=decision.note,
                enforcement_plane="orchestrator",
            )
            if not decision.approved:
                return {
                    "ok": False,
                    "error": "orchestrator-initiated human review rejected the exchange",
                }
            if decision.action == "modify" and decision.modified_args is not None:
                arguments = decision.modified_args

        # HITL: deterministic / conditional / agent_initiated.
        matched = policies.match_hitl(
            self.spec, caller=ctx.caller, tool=tool, args=arguments
        )
        # R4 mandatory HITL: instantiate_template ALWAYS prompts, even if
        # no spec checkpoint matches it. The risk surface (LLM-controlled
        # prompt construction) makes this a non-negotiable safety floor.
        if tool == "instantiate_template" and not any(
            cp.id == "_r4_mandatory" for cp in matched
        ):
            matched = list(matched) + [_R4_MANDATORY_CHECKPOINT]

        for cp in matched:
            extra = arguments.get("reason", "") if tool in {"request_human_review", "request_grant"} else ""
            decision = await self._prompt(
                checkpoint_id=cp.id,
                pattern=cp.pattern,
                prompt_text=cp.prompt,
                caller=ctx.caller,
                tool=tool,
                args=arguments,
                extra=extra,
            )
            self._record(
                kind="hitl_checkpoint",
                ctx=ctx,
                tool=tool,
                args=arguments,
                checkpoint_id=cp.id,
                pattern=cp.pattern,
                decision=decision.action,
                note=decision.note,
            )
            if not decision.approved:
                return {
                    "ok": False,
                    "error": f"human reviewer rejected the call at checkpoint {cp.id!r}"
                    + (f": {decision.note}" if decision.note else ""),
                }
            if decision.action == "modify" and decision.modified_args is not None:
                arguments = decision.modified_args

        for cp in policies.match_platform_triggers(self.spec, self.totals):
            if cp.id in self._fired_platform:
                continue
            self._fired_platform.add(cp.id)
            decision = await self._prompt(
                checkpoint_id=cp.id,
                pattern=cp.pattern,
                prompt_text=cp.prompt,
                caller=ctx.caller,
                tool=tool,
                args=arguments,
                extra=f"totals={self._totals_snapshot()}",
            )
            self._record(
                kind="hitl_checkpoint",
                ctx=ctx,
                tool=tool,
                args=arguments,
                checkpoint_id=cp.id,
                pattern=cp.pattern,
                decision=decision.action,
                note=decision.note,
            )
            if not decision.approved:
                return {
                    "ok": False,
                    "error": f"human reviewer halted execution at checkpoint {cp.id!r}",
                }

        # Dispatch.
        # For spawning pseudo-tools the dispatch awaits the *entire* sub-agent
        # run, so the child's audit events would be written before the
        # parent's tool_call record. Emit a tool_call_start *before* the await
        # so the trace reads in causal order; pair via call_id so the viz can
        # collapse the start+completion into a single row.
        call_id: str | None = None
        if tool in _SPAWNING_TOOLS:
            call_id = uuid.uuid4().hex[:12]
            self._record(
                kind="tool_call_start",
                ctx=ctx,
                tool=tool,
                args=arguments,
                call_id=call_id,
            )
        try:
            if tool == "delegate":
                value = await self._dispatch_delegate(arguments, ctx)
            elif tool == "spawn_parallel":
                value = await self._dispatch_spawn_parallel(arguments, ctx)
            elif tool == "request_grant":
                value = await self._dispatch_request_grant(arguments, ctx)
            elif tool == "instantiate_template":
                value = await self._dispatch_instantiate_template(arguments, ctx)
            elif tool == "request_human_review":
                value = {"acknowledged": True}
            else:
                value = await self._dispatch_tool(tool, arguments, ctx)
                ingress_payload = _payload_for_ingress(value)
                ingress = await self._apply_boundary_policies(
                    tool, ingress_payload, ctx, direction="ingress"
                )
                if ingress is not None:
                    self._record_boundary_decision(ingress, ctx=ctx, tool=tool)
                    if ingress.outcome == "block":
                        self._record(
                            kind="tool_call",
                            ctx=ctx,
                            tool=tool,
                            args=arguments,
                            outcome={
                                "ok": False,
                                "value_preview": ingress.message,
                                "blocked_by_boundary": True,
                            },
                        )
                        return {"ok": False, "error": ingress.message}
                    value = _restore_ingress_value(value, ingress.arguments)
        except policies.PolicyError as e:
            self._record(
                kind="policy_refusal",
                ctx=ctx,
                tool=tool,
                args=arguments,
                outcome={"ok": False, "error": str(e)},
                **({"call_id": call_id} if call_id else {}),
            )
            return {"ok": False, "error": str(e)}
        except Exception as e:  # surface to the LLM as a tool error
            self._record(
                kind="tool_error",
                ctx=ctx,
                tool=tool,
                args=arguments,
                outcome={"ok": False, "error": str(e)},
                **({"call_id": call_id} if call_id else {}),
            )
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

        self._record(
            kind="tool_call",
            ctx=ctx,
            tool=tool,
            args=arguments,
            outcome={"ok": True, "value_preview": _preview(value)},
            **({"call_id": call_id} if call_id else {}),
        )
        if tool == "instantiate_template":
            self._record(
                kind="reshape",
                ctx=ctx,
                tool="instantiate_template",
                args=arguments,
                change="instantiate_template",
                template=arguments.get("template"),
                parameters=arguments.get("parameters") or {},
                call_id=call_id,
            )
        return {"ok": True, "value": value}

    # ---- dispatch helpers --------------------------------------------------

    async def _dispatch_tool(
        self, tool: str, arguments: dict[str, Any], ctx: InvocationContext
    ) -> Any:
        decl = self._tool_by_name.get(tool)
        if decl is None:
            raise RuntimeError(f"tool {tool!r} is not declared in the spec")
        self._note_data_access(decl, ctx)
        self._per_instance_tool_calls[ctx.instance_id] = (
            self._per_instance_tool_calls.get(ctx.instance_id, 0) + 1
        )
        self.totals.tool_calls += 1
        self.totals.tool_calls_by_name[tool] = (
            self.totals.tool_calls_by_name.get(tool, 0) + 1
        )
        if decl.kind == "mcp":
            assert decl.server is not None
            return await self.mcp_pool.call(
                decl.server, decl.remote_name or decl.name, arguments
            )
        if decl.kind == "http":
            return await _call_http_tool(decl, arguments)
        raise RuntimeError(f"tool {tool!r} has unsupported kind {decl.kind!r}")

    async def _dispatch_delegate(
        self, arguments: dict[str, Any], ctx: InvocationContext
    ) -> Any:
        target = arguments.get("target_agent")
        task = arguments.get("task")
        if not target or not task:
            raise RuntimeError("delegate(target_agent, task) requires both arguments")

        policies.check_delegation_permission(ctx, target)
        self._enforce_delegation_depth(ctx)
        policies.check_spawn_budget(self.spec, ctx.caller, self.totals)

        self.totals.sub_agents_spawned[ctx.caller] = (
            self.totals.sub_agents_spawned.get(ctx.caller, 0) + 1
        )
        assert self.orchestrator is not None
        result = await self.orchestrator.spawn_declared(
            target=target, task=str(task), parent_ctx=ctx
        )
        return result

    async def _dispatch_spawn_parallel(
        self, arguments: dict[str, Any], ctx: InvocationContext
    ) -> Any:
        self._ensure_graph_reshape_allowed()
        of_agent = arguments.get("of_agent")
        tasks = arguments.get("tasks") or []
        if not of_agent or not isinstance(tasks, list) or not tasks:
            raise RuntimeError(
                "spawn_parallel(of_agent, tasks: [str, ...]) requires both arguments"
            )

        rule = self._find_spawn_rule(of_agent, ctx.caller)
        if rule is None:
            raise policies.PolicyError(
                f"caller {ctx.caller!r} is not permitted to spawn instances of {of_agent!r}"
            )
        if len(tasks) > rule.max_concurrent:
            raise policies.PolicyError(
                f"spawn_parallel requested {len(tasks)} instances but max_concurrent={rule.max_concurrent}"
            )

        # Spawn budget on the parent (each parallel child still counts).
        parent_autonomy = self.spec.autonomy_for(ctx.caller)
        if (
            self.totals.sub_agents_spawned.get(ctx.caller, 0) + len(tasks)
            > parent_autonomy.max_sub_agents
        ):
            raise policies.PolicyError(
                f"spawn_parallel would exceed parent agent's max_sub_agents budget"
            )

        self._enforce_delegation_depth(ctx)
        self.totals.sub_agents_spawned[ctx.caller] = (
            self.totals.sub_agents_spawned.get(ctx.caller, 0) + len(tasks)
        )

        assert self.orchestrator is not None
        coros = [
            self.orchestrator.spawn_declared(
                target=of_agent, task=str(t), parent_ctx=ctx
            )
            for t in tasks
        ]
        results = await asyncio.gather(*coros, return_exceptions=True)
        # Any exception becomes a per-instance error string in the result list.
        return [
            (r if not isinstance(r, BaseException) else f"error: {type(r).__name__}: {r}")
            for r in results
        ]

    async def _dispatch_request_grant(
        self, arguments: dict[str, Any], ctx: InvocationContext
    ) -> Any:
        self._ensure_graph_reshape_allowed()
        kind = arguments.get("kind")
        name = arguments.get("name")
        reason = arguments.get("reason", "")
        if kind not in {"tool", "delegation_target"} or not name:
            raise RuntimeError(
                "request_grant(kind=tool|delegation_target, name, reason) — bad arguments"
            )

        rule = self._find_acquire_rule(kind, name, ctx.caller)
        if rule is None:
            raise policies.PolicyError(
                f"no permitted_change allows {ctx.caller!r} to acquire "
                f"{kind} {name!r}"
            )
        if rule.reason_required and not reason.strip():
            raise policies.PolicyError(
                f"acquire_capability rule for {name!r} requires a non-empty reason"
            )
        if rule.requires_hitl:
            decision = await self._prompt(
                checkpoint_id=f"reshape:acquire:{kind}:{name}",
                pattern="reshape",
                prompt_text=f"Grant {kind} {name!r} to {ctx.caller}?",
                caller=ctx.caller,
                tool="request_grant",
                args=arguments,
                extra=reason,
            )
            self._record(
                kind="hitl_checkpoint",
                ctx=ctx,
                tool="request_grant",
                args=arguments,
                checkpoint_id=f"reshape:acquire:{kind}:{name}",
                pattern="reshape",
                decision=decision.action,
                note=decision.note,
            )
            if not decision.approved:
                raise policies.PolicyError(
                    f"human reviewer rejected the grant of {kind} {name!r}"
                )

        # Apply the grant in place on the running ctx.
        schema = self.tool_schemas.get(name) if kind == "tool" else None
        ctx.granted_capabilities.append(
            Grant(kind=kind, name=name, reason=reason, tool_schema=schema)
        )
        self._record(
            kind="reshape",
            ctx=ctx,
            tool="request_grant",
            args=arguments,
            change="acquire_capability",
            capability_kind=kind,
            capability_name=name,
        )
        return {"granted": True, "kind": kind, "name": name}

    async def _dispatch_instantiate_template(
        self, arguments: dict[str, Any], ctx: InvocationContext
    ) -> Any:
        self._ensure_graph_reshape_allowed()
        template_name = arguments.get("template")
        params = arguments.get("parameters") or {}
        task = arguments.get("task")
        if not template_name or not task or not isinstance(params, dict):
            raise RuntimeError(
                "instantiate_template(template, parameters, task) — bad arguments"
            )

        rule = self._find_template_rule(template_name, ctx.caller)
        if rule is None:
            raise policies.PolicyError(
                f"caller {ctx.caller!r} is not permitted to instantiate template {template_name!r}"
            )
        used = self._template_instantiations.get(template_name, 0)
        if used >= rule.max_total:
            raise policies.PolicyError(
                f"template {template_name!r} has reached max_total={rule.max_total}"
            )

        self._enforce_delegation_depth(ctx)
        self._template_instantiations[template_name] = used + 1
        self.totals.sub_agents_spawned[ctx.caller] = (
            self.totals.sub_agents_spawned.get(ctx.caller, 0) + 1
        )

        assert self.orchestrator is not None
        result = await self.orchestrator.spawn_template_instance(
            template=template_name,
            parameters=params,
            task=str(task),
            parent_ctx=ctx,
        )
        # The paired tool_call + reshape audit records are emitted by
        # `invoke()` after this returns, so they carry the same call_id as
        # the tool_call_start written before this dispatch.
        return result

    # ---- helpers -----------------------------------------------------------

    def _ensure_graph_reshape_allowed(self) -> None:
        if not self.spec.envelope.graph_reshape.allowed:
            raise policies.PolicyError(
                "graph reshape is disabled in this application's envelope"
            )

    def _enforce_delegation_depth(self, ctx: InvocationContext) -> None:
        parent_autonomy = self.spec.autonomy_for(ctx.caller)
        if ctx.delegation_depth + 1 > parent_autonomy.max_delegation_depth:
            raise policies.PolicyError(
                f"delegation depth {ctx.delegation_depth + 1} exceeds "
                f"max_delegation_depth={parent_autonomy.max_delegation_depth} "
                f"for agent {ctx.caller!r}"
            )

    def _find_spawn_rule(
        self, of_agent: str, caller: str
    ) -> SpawnInstanceChange | None:
        for ch in self.spec.envelope.graph_reshape.permitted_changes:
            if (
                isinstance(ch, SpawnInstanceChange)
                and ch.of_agent == of_agent
                and (not ch.for_callers or caller in ch.for_callers)
            ):
                return ch
        return None

    def _find_acquire_rule(
        self, kind: str, name: str, caller: str
    ) -> AcquireCapabilityChange | None:
        for ch in self.spec.envelope.graph_reshape.permitted_changes:
            if (
                isinstance(ch, AcquireCapabilityChange)
                and ch.capability_kind == kind
                and ch.capability_name == name
                and (not ch.for_callers or caller in ch.for_callers)
            ):
                return ch
        return None

    def _find_template_rule(
        self, template: str, caller: str
    ) -> InstantiateTemplateChange | None:
        for ch in self.spec.envelope.graph_reshape.permitted_changes:
            if (
                isinstance(ch, InstantiateTemplateChange)
                and ch.template == template
                and (not ch.for_callers or caller in ch.for_callers)
            ):
                return ch
        return None

    async def _apply_boundary_policies(
        self,
        tool: str,
        arguments: dict[str, Any],
        ctx: InvocationContext,
        *,
        direction: Literal["ingress", "egress"],
    ) -> "_BoundaryDecision | None":
        policies_for_tool = [
            p
            for p in self.spec.envelope.boundary_policies
            if p.tool == tool
            and p.direction == direction
            and (not p.for_callers or ctx.caller in p.for_callers)
        ]
        if not policies_for_tool:
            return None

        current_args = dict(arguments)
        allow_seen = False
        for policy in policies_for_tool:
            raw = current_args.get(policy.content_arg, "")
            content = raw if isinstance(raw, str) else str(raw)
            match_count = _boundary_match_count(policy, content, ctx)
            if match_count == 0:
                allow_seen = True
                continue
            if policy.action == "allow":
                allow_seen = True
                continue
            if policy.action == "block":
                redacted = dict(current_args)
                redacted[policy.content_arg] = "<blocked by boundary policy>"
                return _BoundaryDecision(
                    outcome="block",
                    policy_id=policy.id,
                    direction=policy.direction,
                    content_arg=policy.content_arg,
                    message=f"boundary policy {policy.id!r} blocked {tool!r}: {policy.message}",
                    arguments=redacted,
                    match_count=match_count,
                )
            if policy.action == "sanitize":
                sanitized = content
                for pattern in policy.match:
                    sanitized = re.sub(
                        pattern,
                        policy.replacement,
                        sanitized,
                        flags=re.IGNORECASE,
                    )
                current_args[policy.content_arg] = sanitized
                return _BoundaryDecision(
                    outcome="sanitize",
                    policy_id=policy.id,
                    direction=policy.direction,
                    content_arg=policy.content_arg,
                    message=f"boundary policy {policy.id!r} sanitized {tool!r}",
                    arguments=current_args,
                    match_count=match_count,
                )
            if policy.action == "escalate_to_human":
                decision = await self._prompt(
                    checkpoint_id=f"boundary:{policy.id}",
                    pattern="boundary",
                    prompt_text=policy.prompt,
                    caller=ctx.caller,
                    tool=tool,
                    args=current_args,
                    extra=policy.message,
                )
                self._record(
                    kind="hitl_checkpoint",
                    ctx=ctx,
                    tool=tool,
                    args=current_args,
                    checkpoint_id=f"boundary:{policy.id}",
                    pattern="boundary",
                    decision=decision.action,
                    note=decision.note,
                )
                if not decision.approved:
                    redacted = dict(current_args)
                    redacted[policy.content_arg] = "<rejected by boundary reviewer>"
                    return _BoundaryDecision(
                        outcome="block",
                        policy_id=policy.id,
                        direction=policy.direction,
                        content_arg=policy.content_arg,
                        message=f"human reviewer rejected boundary exchange {policy.id!r}",
                        arguments=redacted,
                        match_count=match_count,
                    )
                if decision.action == "modify" and decision.modified_args is not None:
                    current_args = decision.modified_args
                return _BoundaryDecision(
                    outcome="escalate_to_human",
                    policy_id=policy.id,
                    direction=policy.direction,
                    content_arg=policy.content_arg,
                    message=f"boundary policy {policy.id!r} escalated {tool!r}",
                    arguments=current_args,
                    match_count=match_count,
                )

        if allow_seen:
            first = policies_for_tool[0]
            return _BoundaryDecision(
                outcome="allow",
                policy_id=first.id,
                direction=first.direction,
                content_arg=first.content_arg,
                message=f"boundary policies allowed {tool!r}",
                arguments=current_args,
                match_count=0,
            )
        return None

    def _record_boundary_decision(
        self, decision: "_BoundaryDecision", *, ctx: InvocationContext, tool: str
    ) -> None:
        self._record(
            kind="boundary_decision",
            ctx=ctx,
            tool=tool,
            args=decision.arguments,
            outcome={
                "ok": decision.outcome != "block",
                "enforcement_outcome": decision.outcome,
                "value_preview": decision.message,
            },
            policy_id=decision.policy_id,
            direction=decision.direction,
            content_arg=decision.content_arg,
            match_count=decision.match_count,
            enforcement_plane="mesh",
        )

    def _note_data_access(self, decl: Tool, ctx: InvocationContext) -> None:
        if not decl.binds_to:
            return
        ds = self._data_source_by_name.get(decl.binds_to)
        if ds is None:
            return
        ctx.accessed_data_sources.add(ds.name)
        ctx.accessed_data_tags.add(f"sensitivity:{ds.sensitivity}")
        ctx.accessed_data_tags.update(ds.tags)
        if _sensitivity_rank(ds.sensitivity) > _sensitivity_rank(ctx.max_data_sensitivity):
            ctx.max_data_sensitivity = ds.sensitivity

    # ---- bookkeeping -------------------------------------------------------

    def record_llm_usage(self, *, cost_usd: float = 0.0) -> None:
        self.totals.llm_calls += 1
        self.totals.cost_usd += cost_usd

    def record_agent_turn(
        self,
        *,
        ctx: InvocationContext,
        turn: int,
        model: str,
        exposed_tools: list[str],
        requested_tools: list[str],
        response_preview: str,
        cost_usd: float = 0.0,
    ) -> None:
        """Record one LLM reasoning turn.

        Tool/action audit remains the mesh's main source of truth, but a
        lightweight turn event helps the visualizer explain why the next
        interaction happened without logging full prompts or chain-of-thought.
        """
        self._record(
            kind="agent_turn",
            ctx=ctx,
            tool="llm",
            args={
                "turn": turn,
                "model": model,
                "exposed_tools": exposed_tools,
                "requested_tools": requested_tools,
                "cost_usd": round(cost_usd, 6),
            },
            outcome={"ok": True, "value_preview": response_preview},
        )

    def _refresh_elapsed(self) -> None:
        # Subtract HITL wait time so the runtime budget measures
        # computation, not how long the human took to click.
        self.totals.elapsed_sec = (
            time.monotonic() - self._started_at - self._hitl_wait_sec
        )

    async def _prompt(self, **kwargs: Any) -> hitl.Decision:
        """Forward to the configured prompter, accumulating wait time so
        ``totals.elapsed_sec`` excludes time spent suspended on a human.
        """
        t0 = time.monotonic()
        try:
            return await self.prompter.prompt(**kwargs)
        finally:
            self._hitl_wait_sec += time.monotonic() - t0

    def _totals_snapshot(self) -> dict[str, Any]:
        return {
            "tool_calls": self.totals.tool_calls,
            "llm_calls": self.totals.llm_calls,
            "cost_usd": round(self.totals.cost_usd, 4),
            "elapsed_sec": round(self.totals.elapsed_sec, 2),
        }

    def _record(
        self,
        *,
        kind: str,
        ctx: InvocationContext,
        tool: str,
        args: dict[str, Any],
        **extra: Any,
    ) -> None:
        record = self.audit.write(
            kind=kind,
            caller=ctx.caller,
            instance_id=ctx.instance_id,
            parent=ctx.parent,
            parent_instance_id=ctx.parent_instance_id,
            delegation_depth=ctx.delegation_depth,
            tool=tool,
            args=args,
            totals=self._totals_snapshot(),
            mesh_context={
                "accessed_data_sources": sorted(ctx.accessed_data_sources),
                "accessed_data_tags": sorted(ctx.accessed_data_tags),
                "max_data_sensitivity": ctx.max_data_sensitivity,
            },
            **extra,
        )
        if self.orchestrator is not None:
            self.orchestrator.observe_mesh_event(record)


# ---------------------------------------------------------------------------
# R4 mandatory HITL — synthesised on the fly so the spec author cannot
# disable it. Has the shape of an HITL checkpoint but is not part of the
# spec's checkpoint list.
# ---------------------------------------------------------------------------


class _SyntheticCheckpoint:
    id = "_r4_mandatory"
    pattern = "predefined"
    prompt = (
        "MANDATORY R4 review: an agent is asking to instantiate a new "
        "templated agent at runtime. Inspect the template, parameters, "
        "and task carefully before approving."
    )


_R4_MANDATORY_CHECKPOINT = _SyntheticCheckpoint()


@dataclass
class _BoundaryDecision:
    outcome: Literal["allow", "block", "sanitize", "escalate_to_human"]
    policy_id: str
    direction: Literal["ingress", "egress"]
    content_arg: str
    message: str
    arguments: dict[str, Any]
    match_count: int = 0


_SENSITIVITY_RANK = {
    "public": 0,
    "internal": 1,
    "confidential": 2,
    "restricted": 3,
}


def _sensitivity_rank(value: str) -> int:
    return _SENSITIVITY_RANK.get(value, 0)


def _boundary_match_count(
    policy: BoundaryPolicy, content: str, ctx: InvocationContext
) -> int:
    count = 0
    for pattern in policy.match:
        count += sum(1 for _ in re.finditer(pattern, content, flags=re.IGNORECASE))
    if policy.data_sensitivity_at_least is not None and _sensitivity_rank(
        ctx.max_data_sensitivity
    ) >= _sensitivity_rank(policy.data_sensitivity_at_least):
        count += 1
    if policy.accessed_data_tags_any and any(
        tag in ctx.accessed_data_tags for tag in policy.accessed_data_tags_any
    ):
        count += 1
    return count


def _payload_for_ingress(value: Any) -> dict[str, Any]:
    """Represent a tool result as an inspectable boundary payload.

    Dict results keep their own fields, so policies can name e.g.
    ``content_arg: content``. Scalar text is exposed as ``content``;
    everything else is exposed as ``result``.
    """
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        return {"content": value}
    return {"result": value}


def _restore_ingress_value(original: Any, payload: dict[str, Any]) -> Any:
    if isinstance(original, dict):
        return payload
    if isinstance(original, str):
        return str(payload.get("content", ""))
    return payload.get("result", original)


def _preview(value: Any, limit: int = 2000) -> str:
    s = repr(value)
    return s if len(s) <= limit else s[:limit] + "...<truncated>"


async def _call_http_tool(decl: Tool, arguments: dict[str, Any]) -> Any:
    """Invoke an `kind: http` tool via urllib, off the event loop.

    GET requests encode args as query parameters; any other method sends
    the args as a JSON body. The response is JSON-parsed when the
    server advertises a JSON content-type, else returned as text.
    """
    import json as _json
    import urllib.parse
    import urllib.request

    assert decl.url is not None

    def _do() -> Any:
        method = decl.method.upper()
        if method == "GET":
            qs = urllib.parse.urlencode(arguments, doseq=True)
            url = decl.url + (("?" + qs) if qs else "")
            req = urllib.request.Request(url, method="GET")
        else:
            body = _json.dumps(arguments).encode("utf-8")
            req = urllib.request.Request(
                decl.url,
                data=body,
                method=method,
                headers={"Content-Type": "application/json"},
            )
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            ctype = resp.headers.get("content-type", "")
        if "json" in ctype:
            try:
                return _json.loads(raw)
            except ValueError:
                return raw
        return raw

    return await asyncio.to_thread(_do)
