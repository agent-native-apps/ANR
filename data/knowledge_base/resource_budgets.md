# Resource budgets

The envelope declares two flavours of resource limit: per-agent
autonomy budgets and run-wide resource limits. Both are enforced by
the mesh on every call; neither lives in the prompt.

## Per-agent autonomy budgets

Declared under `envelope.autonomy.<agent_name>`:

- `max_tool_calls` — cap on tool invocations made by all instances
  of this agent **blueprint** in the current run. A parallel fan-out
  of three researcher instances at ~5 calls each consumes ~15 of the
  researcher's budget, not 5 per instance. Size accordingly.
- `max_sub_agents` — cap on how many child agents this caller may
  spawn (via `delegate` or `spawn_parallel`) over the run. Each
  `instantiate_template` also counts as one.
- `max_delegation_depth` — cap on how deep the delegation chain
  rooted at this caller may go. Depth 1 = direct child, 2 = grandchild.

## Run-wide resource limits

Declared under `envelope.resource_limits`:

- `total_cost_usd` — cumulative LLM cost across every native agent
  call in the run. The cost is sourced from LiteLLM's per-call
  accounting.
- `total_runtime_sec` — wall-clock cap from run start.
- `total_llm_calls` — count of distinct LLM round-trips.

## What happens when a budget is hit

The mesh emits a `budget_exceeded` audit record and refuses the
offending call. Whether the run can recover depends on which agent
hit the limit and where it sits in the delegation tree:

- A run-wide cap usually ends the run.
- A per-agent `max_tool_calls` for a leaf node aborts that subtree;
  the parent receives a tool error and may decide how to proceed.

## Interaction with platform-initiated HITL

Budgets are hard stops. The platform-initiated checkpoint
(`platform_initiated_hitl.md`) is a softer earlier signal — it
pauses for the operator before the run is forcibly aborted. A
well-tuned spec sizes its `platform_initiated` triggers to fire
*before* the hard budget would.
