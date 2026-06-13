"""Policy predicates evaluated by the mesh.

This module groups the decisions the mesh needs to make on every interaction:

  * is this (caller, tool) combination permitted by the spec?
  * does the interaction match a HITL checkpoint?
  * is a platform-initiated checkpoint triggered by the current totals?
  * would accepting this call exceed a declared resource limit?

The conditional-HITL and platform-initiated triggers use a *restricted*
expression evaluator — we parse an expression into a Python AST and walk it
with a small whitelist of allowed node types and names. This is intentional:
the spec contains expressions authored by the developer, but we still never
hand them to eval(). A compromised or malformed spec cannot execute
arbitrary code through this path.
"""

from __future__ import annotations

import ast
import operator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from .spec import HITLCheckpoint, Spec

if TYPE_CHECKING:
    from .mesh import InvocationContext


class PolicyError(Exception):
    """Raised when the mesh must refuse an interaction outright."""


@dataclass
class ResourceTotals:
    tool_calls: int = 0
    llm_calls: int = 0
    cost_usd: float = 0.0
    elapsed_sec: float = 0.0
    sub_agents_spawned: dict[str, int] = None  # type: ignore[assignment]
    # Per-tool-name completed-call counts. Populated by the mesh in
    # _dispatch_tool so platform-initiated triggers can key off a
    # specific tool's rate (e.g. "count_of('mark_urgent') >= 3").
    tool_calls_by_name: dict[str, int] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.sub_agents_spawned is None:
            self.sub_agents_spawned = {}
        if self.tool_calls_by_name is None:
            self.tool_calls_by_name = {}


# ---------------------------------------------------------------------------
# Permission enforcement (§3.1.2 + §5.1.3)
# ---------------------------------------------------------------------------


def check_tool_permission(ctx: "InvocationContext", tool: str) -> None:
    """Raise PolicyError if `ctx.caller` is not allowed to invoke `tool`.

    Permission is computed entirely from the live invocation context's
    `allowed_tools` set (populated at spawn from the agent's blueprint)
    plus any runtime grants that were attached via R2 capability
    acquisition. The spec is not consulted here — that is what makes
    template-instantiated agents work uniformly with declared agents.
    """
    if tool in ctx.allowed_tools:
        return
    for g in ctx.granted_capabilities:
        if g.kind == "tool" and g.name == tool:
            return
    raise PolicyError(
        f"agent {ctx.caller!r} is not permitted to call tool {tool!r}"
    )


def check_delegation_permission(ctx: "InvocationContext", target: str) -> None:
    if target in ctx.allowed_delegation_targets:
        return
    for g in ctx.granted_capabilities:
        if g.kind == "delegation_target" and g.name == target:
            return
    raise PolicyError(
        f"agent {ctx.caller!r} may not delegate to {target!r}"
    )


# ---------------------------------------------------------------------------
# HITL matching (§3.2.2 — four patterns)
# ---------------------------------------------------------------------------


def match_hitl(
    spec: Spec,
    *,
    caller: str,
    tool: str,
    args: dict[str, Any],
) -> list[HITLCheckpoint]:
    """Return every checkpoint the current interaction matches."""
    matches: list[HITLCheckpoint] = []
    for cp in spec.envelope.hitl_checkpoints:
        if cp.pattern == "predefined" and _when_matches(cp, caller, tool):
            matches.append(cp)
        elif cp.pattern == "conditional" and _when_matches(cp, caller, tool):
            if _eval_condition(cp.condition or "", spec=spec, args=args):
                matches.append(cp)
        elif cp.pattern == "agent_initiated" and tool == "request_human_review":
            if not cp.allowed_for or caller in cp.allowed_for:
                matches.append(cp)
        # platform_initiated is evaluated separately against totals, below.
    return matches


def match_platform_triggers(
    spec: Spec, totals: ResourceTotals
) -> list[HITLCheckpoint]:
    matches: list[HITLCheckpoint] = []
    for cp in spec.envelope.hitl_checkpoints:
        if cp.pattern != "platform_initiated":
            continue
        if _eval_condition(cp.trigger or "", spec=spec, totals=totals):
            matches.append(cp)
    return matches


def _when_matches(cp: HITLCheckpoint, caller: str, tool: str) -> bool:
    w = cp.when
    if w is None:
        return True
    if w.tool is not None and w.tool != tool:
        return False
    if w.caller is not None and w.caller != caller:
        return False
    return True


# ---------------------------------------------------------------------------
# Resource limits
# ---------------------------------------------------------------------------


def check_resource_limits(spec: Spec, totals: ResourceTotals) -> None:
    rl = spec.envelope.resource_limits
    if totals.tool_calls >= rl.total_llm_calls * 4:  # generous cap on tool calls vs llm calls
        # we don't have a dedicated total_tool_calls field yet; use 4x llm as a proxy
        raise PolicyError("global tool-call budget exhausted")
    if totals.llm_calls >= rl.total_llm_calls:
        raise PolicyError("global LLM-call budget exhausted")
    if totals.cost_usd >= rl.total_cost_usd:
        raise PolicyError("global cost budget exhausted")
    if totals.elapsed_sec >= rl.total_runtime_sec:
        raise PolicyError("global runtime budget exhausted")


def check_agent_tool_budget(
    spec: Spec,
    caller: str,
    instance_id: str,
    per_instance_tool_calls: dict[str, int],
) -> None:
    """Enforce ``autonomy[caller].max_tool_calls`` per *instance*, not per
    blueprint.

    Parallel R1 spawns and R4 template instantiations create multiple
    live instances of the same blueprint; each gets its own tool-call
    budget. The bookkeeping key is the instance id (which is unique
    per spawn) — the blueprint name is what the spec.autonomy_for
    lookup uses.
    """
    limit = spec.autonomy_for(caller).max_tool_calls
    if per_instance_tool_calls.get(instance_id, 0) >= limit:
        raise PolicyError(
            f"agent {caller!r} (instance {instance_id!r}) has exceeded "
            f"its max_tool_calls ({limit})"
        )


def check_spawn_budget(
    spec: Spec, parent: str, totals: ResourceTotals
) -> None:
    limit = spec.autonomy_for(parent).max_sub_agents
    if totals.sub_agents_spawned.get(parent, 0) >= limit:
        raise PolicyError(
            f"agent {parent!r} has reached its max_sub_agents limit ({limit})"
        )


# ---------------------------------------------------------------------------
# Restricted expression evaluator
# ---------------------------------------------------------------------------

_ALLOWED_BINOPS: dict[type, Any] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Mod: operator.mod,
}

_ALLOWED_CMPOPS: dict[type, Any] = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
    ast.In: lambda a, b: a in b,
    ast.NotIn: lambda a, b: a not in b,
}

_ALLOWED_BOOLOPS: dict[type, Any] = {
    ast.And: all,
    ast.Or: any,
}


class _Namespace:
    """Read-only attribute view over a dict, for `args.url` style access."""

    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    def __getattr__(self, name: str) -> Any:
        if name in self._data:
            v = self._data[name]
            return _Namespace(v) if isinstance(v, dict) else v
        raise AttributeError(name)


def _eval_condition(
    expr: str,
    *,
    spec: Spec,
    args: dict[str, Any] | None = None,
    totals: ResourceTotals | None = None,
) -> bool:
    if not expr.strip():
        return False
    tree = ast.parse(expr, mode="eval")

    # Build the name scope the expression can see.
    scope: dict[str, Any] = {
        "host": _host,
        "len": len,
        "True": True,
        "False": False,
        "None": None,
    }
    for ds in spec.data_sources:
        if ds.name == "web":
            scope["allowed_domains"] = list(ds.allowed_domains)
            break
    if args is not None:
        scope["args"] = _Namespace(args)
    if totals is not None:
        scope["cost_usd"] = totals.cost_usd
        scope["tool_calls"] = totals.tool_calls
        scope["llm_calls"] = totals.llm_calls
        scope["elapsed_sec"] = totals.elapsed_sec
        # count_of(name) lets a platform-initiated trigger reference the
        # completed-call count of a specific tool. Missing names read as 0.
        scope["count_of"] = lambda name, _t=totals: _t.tool_calls_by_name.get(name, 0)

    return bool(_walk(tree.body, scope))


def _walk(node: ast.AST, scope: dict[str, Any]) -> Any:
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        if node.id not in scope:
            valid = sorted(n for n in scope if not n.startswith("_"))
            raise PolicyError(
                f"condition references unknown name {node.id!r}; "
                f"valid names in this scope: {valid}"
            )
        return scope[node.id]
    if isinstance(node, ast.Attribute):
        value = _walk(node.value, scope)
        return getattr(value, node.attr)
    if isinstance(node, ast.Subscript):
        value = _walk(node.value, scope)
        key = _walk(node.slice, scope)
        return value[key]
    if isinstance(node, ast.List):
        return [_walk(e, scope) for e in node.elts]
    if isinstance(node, ast.Tuple):
        return tuple(_walk(e, scope) for e in node.elts)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return not _walk(node.operand, scope)
    if isinstance(node, ast.BinOp):
        op = _ALLOWED_BINOPS.get(type(node.op))
        if op is None:
            raise PolicyError(f"binary op {type(node.op).__name__} not allowed")
        return op(_walk(node.left, scope), _walk(node.right, scope))
    if isinstance(node, ast.BoolOp):
        reducer = _ALLOWED_BOOLOPS[type(node.op)]
        return reducer(_walk(v, scope) for v in node.values)
    if isinstance(node, ast.Compare):
        left = _walk(node.left, scope)
        for op_node, right_node in zip(node.ops, node.comparators, strict=True):
            cmp = _ALLOWED_CMPOPS.get(type(op_node))
            if cmp is None:
                raise PolicyError(f"comparison op {type(op_node).__name__} not allowed")
            right = _walk(right_node, scope)
            if not cmp(left, right):
                return False
            left = right
        return True
    if isinstance(node, ast.Call):
        func = _walk(node.func, scope)
        if func not in scope.values():
            raise PolicyError("condition called a non-whitelisted function")
        args = [_walk(a, scope) for a in node.args]
        return func(*args)
    raise PolicyError(f"condition uses unsupported AST node {type(node).__name__}")


def _host(url: str) -> str:
    parsed = urlparse(url)
    return parsed.hostname or ""
