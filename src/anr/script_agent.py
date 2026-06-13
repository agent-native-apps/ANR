"""Script-backed nodes: one of several possible AgentNode implementations.

A `ScriptAgent` resolves `declaration.script_entry` (of the form
`module.path:callable_name`) to a Python callable and runs it as the
body of the node. The callable receives the task, the mesh-provided
`InvocationContext`, and the mesh itself, so it can invoke tools and
delegate through the same enforcement path that LLM-backed nodes use.

This exists primarily to demonstrate that the node interface
(anr/agent_base.py) accepts non-LLM, non-framework-specific workers.
The same shape is what lets a future `kind: langgraph_subgraph` or
`kind: a2a_remote` plug into the same spec, envelope, and mesh with
no changes to the runtime core.
"""

from __future__ import annotations

import importlib
import inspect
from typing import Any, Awaitable, Callable

from rich.console import Console

from .agent_base import AgentNode
from .mesh import InvocationContext, Mesh

console = Console(stderr=True)

# Signature the script entry must expose.
ScriptEntry = Callable[[str, InvocationContext, Mesh], Awaitable[str]]


class ScriptAgent(AgentNode):
    def __init__(
        self,
        *,
        declaration: Any,  # duck-typed: .name
        mesh: Mesh,
        entry: ScriptEntry,
    ) -> None:
        self.decl = declaration
        self.mesh = mesh
        self.entry = entry

    async def run(self, task: str, ctx: InvocationContext) -> str:
        console.log(
            f"[magenta]{self.decl.name}[/magenta] (script) invoking "
            f"{self.entry.__module__}:{self.entry.__name__}"
        )
        result = self.entry(task, ctx, self.mesh)
        if inspect.isawaitable(result):
            result = await result
        return str(result) if result is not None else ""


def resolve_script_entry(spec_ref: str) -> ScriptEntry:
    """Resolve 'pkg.mod:callable' to the callable object.

    We import the module with importlib (honouring the active sys.path,
    which includes the spec directory during compilation — see Compiler).
    """
    if ":" not in spec_ref:
        raise ValueError(
            f"script_entry {spec_ref!r} must be in 'module.path:callable' form"
        )
    module_name, attr = spec_ref.split(":", 1)
    module = importlib.import_module(module_name)
    if not hasattr(module, attr):
        raise ImportError(
            f"module {module_name!r} has no attribute {attr!r}"
        )
    obj = getattr(module, attr)
    if not callable(obj):
        raise TypeError(f"{spec_ref}: resolved object is not callable")
    return obj  # type: ignore[return-value]
