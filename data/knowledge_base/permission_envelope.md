# The permission envelope (§3.1.2)

Every agent in an agent-native application runs inside a declared
**permission envelope** that the mesh enforces on every call. The
envelope is not advice handed to the model — it is infrastructure
that intercepts each tool invocation and refuses anything outside the
declared scope.

## What the envelope declares

A per-agent envelope answers four questions:

1. **Tool whitelist.** Which named tools may this agent invoke directly?
   The envelope is a hard allow-list; anything absent is refused with a
   `permission_denied` audit record, regardless of what the LLM tries.
2. **Delegation targets.** Which other agents may this caller hand a
   sub-task to via `delegate(...)` or `spawn_parallel(...)`? An agent
   cannot delegate to a sibling that the spec does not list.
3. **Autonomy budget.** How many sub-agents may this caller spawn, how
   deep may its delegation chain go, and how many tool calls may it
   make in a single run. The mesh tracks these against the live
   `InvocationContext`.
4. **Reshape authority.** Whether (and how) this caller may mutate the
   running graph via `request_grant`, `spawn_parallel`, or
   `instantiate_template`. See `reshape_kinds.md`.

## Why declarative over prompted

You cannot put "do not call write_note unless approved" in a system
prompt and expect compliance. The model will obey on 99 inputs out of
100 and surprise you on the hundredth. The envelope removes the
question — `write_note` is either in the whitelist or it is not, and
no amount of clever phrasing in a delegated task description can
synthesise a permission the spec does not grant.

## Interaction with HITL

The envelope and the HITL checkpoints (see `hitl_patterns.md`) are
orthogonal. The envelope decides *whether* a call may proceed at all;
HITL decides whether a permitted call needs human approval before it
runs. A call can be refused on permission grounds without ever
reaching a checkpoint, and a permitted call can still be paused by
a checkpoint.
