"""The node interface: the only contract a component must satisfy to live
in an agent-native application graph.

The paper argues that agent-native is a paradigm rather than a framework
because the specification, the envelope, and the enforcement substrate
are separate from whatever machinery actually runs inside a node. This
module is the code-level embodiment of that claim: an `AgentNode` is
anything with an async `run(task, ctx) -> str` method.

The default `NativeAgent` (anr/agent.py) is one implementation — a
LiteLLM-backed tool-use loop. `ScriptAgent` (anr/script_agent.py) is
another, wrapping a plain Python callable. A future implementation could
wrap a LangGraph subgraph, an A2A remote endpoint, or an OpenAI Agents
SDK agent; nothing in the mesh, envelope, or orchestrator would need
to change. The mesh only ever sees `await node.run(...)`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from .mesh import InvocationContext


@runtime_checkable
class AgentNode(Protocol):
    """The runtime contract every node-kind implementation must satisfy.

    `run` is invoked by the orchestrator (for entry-point and delegated
    tasks) with a task string and a mesh-provided `InvocationContext`
    carrying this node's authority (allowed tools, delegation targets,
    granted capabilities). Implementations that need to invoke tools or
    delegate should do so through `mesh.invoke(...)` using the supplied
    context, so the mesh's policy, HITL, and audit machinery applies
    uniformly regardless of node kind.
    """

    async def run(self, task: str, ctx: "InvocationContext") -> str: ...
