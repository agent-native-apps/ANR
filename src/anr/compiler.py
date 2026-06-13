"""Compile a Spec into a running Graph of mesh, orchestrator, and agents.

This is the "compiler from declarative specification to running
application" that §3.1.2 names as a gap. The compiler:

  1. opens stdio sessions to every MCP server referenced in the spec,
  2. discovers each server's tool schemas so the LLM sees accurate
     signatures without the developer re-declaring them in the spec,
  3. builds, for every declared agent, the exact subset of tools and
     delegation targets it is permitted to use (including which mesh
     pseudo-tools the envelope makes available — `delegate`,
     `request_human_review`, `spawn_parallel`, `request_grant`,
     `instantiate_template`),
  4. instantiates one AgentNode per declared agent by dispatching to
     the builder registered for its `kind` (see AGENT_BUILDERS) and an
     `AgentAuthority` record per declared agent / template, which the
     orchestrator uses to populate every spawned InvocationContext,
  5. wires a single Mesh as the chokepoint and an Orchestrator as the
     entry-point router and agent spawner; the cycle is resolved by
     constructing the mesh first, then the orchestrator, then setting
     `mesh.orchestrator = orchestrator`,
  6. returns a Graph that can run any number of tasks against the
     compiled application.
"""

from __future__ import annotations

import os
import sys
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from rich.console import Console

from .agent import NativeAgent, load_system_prompt
from .agent_base import AgentNode
from .audit import AuditLog
from .hitl import make_prompter
from .mcp_client import MCPPool
from .mesh import Mesh
from .orchestrator import AgentAuthority, Orchestrator
from .script_agent import ScriptAgent, resolve_script_entry
from .spec import (
    AcquireCapabilityChange,
    Agent as AgentDecl,
    AgentTemplate,
    InstantiateTemplateChange,
    SpawnInstanceChange,
    Spec,
)

console = Console(stderr=True)


# ---- Pseudo-tool schemas -------------------------------------------------

_DELEGATE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "delegate",
        "description": (
            "Hand off a focused sub-task to another agent. The target agent "
            "runs with its own declared tool scope — it does NOT inherit "
            "yours. Use this when the sub-task requires capabilities or "
            "focus you do not have."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "target_agent": {"type": "string", "description": "Name of the agent to delegate to."},
                "task": {"type": "string", "description": "A complete, self-contained task description."},
            },
            "required": ["target_agent", "task"],
        },
    },
}

_HUMAN_REVIEW_SCHEMA = {
    "type": "function",
    "function": {
        "name": "request_human_review",
        "description": (
            "Pause execution and request a human reviewer's judgement on a "
            "high-stakes or ambiguous decision. Use sparingly — only when "
            "you genuinely cannot proceed without human input."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "reason": {"type": "string", "description": "Why a human is needed."},
            },
            "required": ["reason"],
        },
    },
}

_SPAWN_PARALLEL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "spawn_parallel",
        "description": (
            "Spawn multiple concurrent instances of a declared agent type, "
            "each working on its own sub-task. Returns a list of results in "
            "the same order. Use when several focused investigations can "
            "proceed independently. The envelope caps `max_concurrent` per "
            "agent type."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "of_agent": {
                    "type": "string",
                    "description": "Declared agent type to spawn (e.g. 'researcher').",
                },
                "tasks": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "One self-contained task per instance.",
                },
            },
            "required": ["of_agent", "tasks"],
        },
    },
}

_REQUEST_GRANT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "request_grant",
        "description": (
            "Request runtime acquisition of a tool or delegation target you "
            "do not currently possess. The mesh validates against the "
            "envelope's permitted_changes and may require human approval. "
            "On success, the new capability becomes available to YOU only, "
            "for the rest of THIS task."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": ["tool", "delegation_target"],
                    "description": "What kind of capability to acquire.",
                },
                "name": {
                    "type": "string",
                    "description": "Name of the tool or delegation target.",
                },
                "reason": {
                    "type": "string",
                    "description": "Why you need this capability for the current task.",
                },
            },
            "required": ["kind", "name", "reason"],
        },
    },
}

_INSTANTIATE_TEMPLATE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "instantiate_template",
        "description": (
            "Instantiate a brand-new agent at runtime from a declared "
            "template, parameterised for this specific task. The new agent "
            "exists for one task only, then dies. EVERY call to this tool "
            "requires explicit human approval — do not call casually."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "template": {
                    "type": "string",
                    "description": "Name of a declared agent_template.",
                },
                "parameters": {
                    "type": "object",
                    "description": "Bindings for the template's declared parameters (closed set).",
                    "additionalProperties": {"type": "string"},
                },
                "task": {
                    "type": "string",
                    "description": "The task the new agent should execute.",
                },
            },
            "required": ["template", "parameters", "task"],
        },
    },
}


@dataclass
class Graph:
    spec: Spec
    mesh: Mesh
    orchestrator: Orchestrator
    audit_path: Path

    async def run(self, task: str) -> str:
        return await self.orchestrator.handle(task)


class Compiler:
    """Builds a live Graph inside an async context. Use with `async with`."""

    def __init__(
        self,
        spec: Spec,
        *,
        spec_dir: Path,
        output_dir: Path,
    ) -> None:
        self.spec = spec
        self.spec_dir = spec_dir
        self.output_dir = output_dir
        self._stack = AsyncExitStack()

    async def __aenter__(self) -> Graph:
        await self._stack.__aenter__()

        audit_path = self.output_dir / "audit.jsonl"
        audit = AuditLog(audit_path)
        hitl_dir = self.output_dir / "hitl"
        hitl_dir.mkdir(parents=True, exist_ok=True)
        # Sweep leftovers from a previous run so the fresh session starts
        # with no phantom pending prompts and no pre-canned decisions.
        for stale in hitl_dir.glob("req-*.json"):
            stale.unlink(missing_ok=True)
        for stale in hitl_dir.glob("res-*.json"):
            stale.unlink(missing_ok=True)
        prompter = make_prompter(hitl_dir=hitl_dir)

        pool = await self._stack.enter_async_context(MCPPool())

        # Inject runtime-resolved paths into every MCP server's environment.
        docs_dir: str | None = None
        for ds in self.spec.data_sources:
            if ds.kind == "local_directory" and ds.path:
                docs_dir = str(self._resolve_existing_path(ds.path))
                break
        runtime_env: dict[str, str] = {
            "ANR_OUTPUT_DIR": str((self.output_dir / "notes").resolve()),
        }
        if docs_dir is not None:
            runtime_env["ANR_DOCS_DIR"] = docs_dir
        for tool in self.spec.tools:
            if tool.kind == "mcp" and tool.server is not None:
                tool.server.env = {**tool.server.env, **runtime_env}

        # Discover tool schemas. MCP tools are resolved by live
        # `list_tools()` against each server; HTTP tools declare their
        # schema inline in the spec.
        schema_by_tool: dict[str, dict[str, Any]] = {}
        for tool in self.spec.tools:
            if tool.kind == "mcp" and tool.server is not None:
                remote_tools = await pool.list_tools(tool.server)
                by_name = {rt.name: rt for rt in remote_tools}
                remote_name = tool.remote_name or tool.name
                if remote_name not in by_name:
                    raise RuntimeError(
                        f"MCP server for tool {tool.name!r} does not advertise "
                        f"{remote_name!r}. Available: {sorted(by_name)}"
                    )
                rt = by_name[remote_name]
                schema_by_tool[tool.name] = {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description or (rt.description or ""),
                        "parameters": rt.inputSchema or {"type": "object", "properties": {}},
                    },
                }
            elif tool.kind == "http":
                schema_by_tool[tool.name] = {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.input_schema or {"type": "object", "properties": {}},
                    },
                }

        # Build the mesh first; orchestrator gets wired in below.
        mesh = Mesh(
            spec=self.spec,
            mcp_pool=pool,
            audit=audit,
            tool_schemas=schema_by_tool,
            prompter=prompter,
        )

        # Make the spec directory importable so `kind: script` agents
        # can reference local modules via `module:callable` without
        # needing a packaged install.
        spec_dir_str = str(self.spec_dir.resolve())
        if spec_dir_str not in sys.path:
            sys.path.insert(0, spec_dir_str)

        # Per-agent and per-template authority + per-agent node instance.
        # Any AgentNode implementation is acceptable — the mesh only
        # talks to nodes through their `.run(task, ctx)` method.
        agent_authority: dict[str, AgentAuthority] = {}
        template_authority: dict[str, AgentAuthority] = {}
        agents: dict[str, AgentNode] = {}

        # Fault-injection mode (ANR_EXPOSE=all): hand every agent the
        # schemas of ALL declared tools, simulating a miscalibrated or
        # drifted node whose local configuration no longer matches its
        # authority. Mesh-side permissions (AgentAuthority, built from
        # decl.tools below) are deliberately untouched, so out-of-scope
        # calls are proposed by the agent and refused by the mesh.
        expose_all = os.environ.get("ANR_EXPOSE", "").strip().lower() == "all"
        if expose_all:
            console.print(
                "[yellow]compiler:[/yellow] ANR_EXPOSE=all — node-local tool "
                "scoping disabled (fault injection); mesh permissions unchanged"
            )
        all_tool_names = [t.name for t in self.spec.tools]

        for decl in self.spec.agents:
            pseudo_tools = self._pseudo_tools_for_caller(decl.name)
            exposed = all_tool_names if expose_all else decl.tools
            tool_schemas = self._build_tool_schemas(exposed, schema_by_tool, pseudo_tools)
            authority = AgentAuthority(
                tools=tuple(decl.tools) + tuple(pseudo_tools),
                delegation_targets=tuple(decl.may_delegate_to),
            )
            agent_authority[decl.name] = authority

            builder = AGENT_BUILDERS.get(decl.kind)
            if builder is None:
                raise RuntimeError(
                    f"no node builder registered for agent kind {decl.kind!r}; "
                    f"known kinds: {sorted(AGENT_BUILDERS)}"
                )
            agents[decl.name] = builder(
                decl,
                spec=self.spec,
                mesh=mesh,
                tool_schemas=tool_schemas,
                resolve_prompt_path=self._resolve_prompt_path,
            )

        for tpl in self.spec.agent_templates:
            # Templates only get their declared tools + request_human_review.
            # They cannot themselves spawn / instantiate / acquire — that
            # would require pre-declaring their authority which we keep
            # constrained for the POC.
            template_authority[tpl.name] = AgentAuthority(
                tools=tuple(tpl.tools) + ("request_human_review",),
                delegation_targets=tuple(tpl.may_delegate_to),
            )

        orchestrator = Orchestrator(
            spec=self.spec,
            mesh=mesh,
            agents=agents,
            agent_authority=agent_authority,
            template_authority=template_authority,
        )
        mesh.orchestrator = orchestrator

        return Graph(
            spec=self.spec,
            mesh=mesh,
            orchestrator=orchestrator,
            audit_path=audit_path,
        )

    async def __aexit__(self, *exc: object) -> None:
        await self._stack.__aexit__(*exc)

    # ---- helpers -----------------------------------------------------------

    def _resolve_prompt_path(self, ref: str) -> Path:
        candidate = Path(ref)
        if candidate.is_absolute():
            return candidate
        for base in (self.spec_dir, Path.cwd()):
            p = (base / candidate).resolve()
            if p.is_file():
                return p
        return (Path.cwd() / candidate).resolve()

    def _resolve_existing_path(self, ref: str) -> Path:
        candidate = Path(ref)
        if candidate.is_absolute():
            return candidate
        for base in (self.spec_dir, Path.cwd()):
            p = (base / candidate).resolve()
            if p.exists():
                return p
        return (Path.cwd() / candidate).resolve()

    def _build_tool_schemas(
        self,
        tool_names: list[str],
        schema_by_tool: dict[str, dict[str, Any]],
        pseudo_tools: list[str],
    ) -> list[dict[str, Any]]:
        schemas: list[dict[str, Any]] = []
        for name in tool_names:
            if name in schema_by_tool:
                schemas.append(schema_by_tool[name])
        for name in pseudo_tools:
            schema = _PSEUDO_TOOL_SCHEMAS.get(name)
            if schema is not None:
                schemas.append(schema)
        return schemas

    def _pseudo_tools_for_caller(self, caller_name: str) -> list[str]:
        """Decide which mesh pseudo-tools this caller can see in its tool list."""
        out: list[str] = []
        decl = self.spec.agent(caller_name)

        if decl.may_delegate_to:
            out.append("delegate")

        if any(
            cp.pattern == "agent_initiated"
            and (not cp.allowed_for or caller_name in cp.allowed_for)
            for cp in self.spec.envelope.hitl_checkpoints
        ):
            out.append("request_human_review")

        if not self.spec.envelope.graph_reshape.allowed:
            return out

        # Reshape pseudo-tools: gated by envelope.permitted_changes for_callers.
        for ch in self.spec.envelope.graph_reshape.permitted_changes:
            for_callers = ch.for_callers
            if for_callers and caller_name not in for_callers:
                continue
            if isinstance(ch, SpawnInstanceChange) and "spawn_parallel" not in out:
                out.append("spawn_parallel")
            elif isinstance(ch, AcquireCapabilityChange) and "request_grant" not in out:
                out.append("request_grant")
            elif (
                isinstance(ch, InstantiateTemplateChange)
                and "instantiate_template" not in out
            ):
                out.append("instantiate_template")

        return out


_PSEUDO_TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "delegate": _DELEGATE_SCHEMA,
    "request_human_review": _HUMAN_REVIEW_SCHEMA,
    "spawn_parallel": _SPAWN_PARALLEL_SCHEMA,
    "request_grant": _REQUEST_GRANT_SCHEMA,
    "instantiate_template": _INSTANTIATE_TEMPLATE_SCHEMA,
}


# ---------------------------------------------------------------------------
# Node-kind builder registry
# ---------------------------------------------------------------------------
#
# The compiler looks up a builder by `agents[].kind` and delegates the
# actual node construction. Each builder takes the declaration plus a
# handful of keyword-only resources (the spec, the mesh, the agent's
# effective tool-schema list, a prompt-path resolver) and returns any
# object satisfying the AgentNode protocol.
#
# Adding a new node kind — say, one that wraps a LangGraph subgraph or
# an A2A remote endpoint — is a matter of registering a new builder here
# and extending the `kind` literal in spec.py. The mesh, envelope,
# orchestrator, and audit machinery all remain untouched.

AgentBuilder = Callable[..., AgentNode]


def _build_native_agent(
    decl: AgentDecl,
    *,
    spec: Spec,
    mesh: Mesh,
    tool_schemas: list[dict[str, Any]],
    resolve_prompt_path: Callable[[str], Path],
) -> AgentNode:
    assert decl.system_prompt_file is not None  # enforced by spec validator
    prompt_path = resolve_prompt_path(decl.system_prompt_file)
    return NativeAgent(
        spec=spec,
        declaration=decl,
        mesh=mesh,
        tool_schemas=tool_schemas,
        system_prompt=load_system_prompt(prompt_path),
    )


def _build_script_agent(
    decl: AgentDecl,
    *,
    spec: Spec,
    mesh: Mesh,
    tool_schemas: list[dict[str, Any]],
    resolve_prompt_path: Callable[[str], Path],
) -> AgentNode:
    assert decl.script_entry is not None  # enforced by spec validator
    entry = resolve_script_entry(decl.script_entry)
    return ScriptAgent(declaration=decl, mesh=mesh, entry=entry)


AGENT_BUILDERS: dict[str, AgentBuilder] = {
    "native": _build_native_agent,
    "script": _build_script_agent,
}


def register_agent_builder(kind: str, builder: AgentBuilder) -> None:
    """Register an out-of-tree node-kind builder.

    Callers would typically do this in an import-time hook before
    entering the Compiler context, e.g. to plug in a LangGraph-backed
    or A2A-remote node kind.
    """
    AGENT_BUILDERS[kind] = builder
