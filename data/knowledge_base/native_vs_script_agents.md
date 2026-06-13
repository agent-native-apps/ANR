# Native vs script agents (§3.1.1)

The runtime treats agent **kind** as a pluggable axis. Both
implementations satisfy the same `AgentNode` Protocol — an async
`run(task, ctx) -> str` method — and the compiler registers builders
in `AGENT_BUILDERS` keyed by `kind`. Adding a new node kind means
registering a builder; it does not require touching the mesh, the
orchestrator, or the envelope.

## kind: native

A native agent is LLM-backed. Its `run` body is the LiteLLM
tool-use loop: the model receives the task plus a description of its
permitted tools (the envelope's whitelist plus the pseudo-tools the
mesh exposes), iterates between tool calls and reasoning, and
returns a final-message string. Most agents in the demo specs are
`native`.

## kind: script

A script agent is a plain Python callable resolved via importlib
from the `script_entry` field of the spec. It receives the same
arguments — `task`, `ctx`, plus a `mesh` reference so it can invoke
tools through the same chokepoint — but it does no model reasoning.
Script agents are useful for:

- **Deterministic guardrails.** The `comms_guardrail` in
  `supply_chain.yaml` is a regex-driven filter that says APPROVED or
  BLOCKED on outbound text. The procurement agent must clear every
  outbound message through it before calling `send_supplier_message`.
- **Deterministic transforms.** Format normalisation, structured-
  output validators, or any step where stochasticity would be a bug.
- **Cheap bookkeeping.** Aggregation, summarisation, or counter
  updates that do not warrant a model call.

A script node inhabits the same mesh as its native peers. It is
bound by the same permission envelope; its calls go through
`mesh.invoke(...)`; its activity shows up in the audit log alongside
LLM calls. The point is that "agent" in agent-native is a role in
the graph, not a synonym for "LLM call".

## Future kinds

The contract is intentionally narrow so that a `kind: a2a_remote`,
`kind: langgraph_subgraph`, or `kind: tool_using_workflow` would
plug in the same way. The mesh remains the chokepoint regardless.
