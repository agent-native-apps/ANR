# Safety invariants the mesh guarantees

The mesh is the enforcement boundary for everything the paper
calls "agent-native safety". An invariant in this list holds for
*every* run, regardless of what the LLM tries to do, what
delegation chain it inherits, or what creative tool argument it
synthesises.

## I1 — No tool call bypasses the mesh

The only path an agent has to a tool is `mesh.invoke(...)`. There
is no back door. Native agents reach tools through the LiteLLM
tool-use loop, which itself dispatches each call through the
mesh; script agents receive a `mesh` reference and are required
to use it. A new agent `kind` added in the future plugs into the
same chokepoint or it does not run at all.

## I2 — Every call is permission-checked

Before dispatch, the mesh consults the caller's tool whitelist.
A tool not in the whitelist is refused with `permission_denied`
and an audit record. The model cannot "argue" its way past this —
the check is on the call site, not on the prompt.

## I3 — HITL fires before the action, not after

When a checkpoint matches, the mesh pauses the call **before**
the side effect executes. The prompter (terminal or UI) returns
one of: `approve` (proceed with original args), `reject` (return
a tool error to the caller), or `modify` (proceed with substituted
args). The audit log records the decision.

## I4 — Reshape stays inside declared rules

`request_grant`, `spawn_parallel`, and `instantiate_template` all
go through the orchestrator's `permitted_changes` table. A reshape
that does not match a declared rule is refused. R4
(`instantiate_template`) always fires HITL regardless of the rule,
because creating a new node at runtime is the most consequential
mutation in the runtime.

## I5 — Budgets are hard

Per-envelope and run-wide budgets abort the offending call. There
is no grace, no negotiation, no override path short of editing
the spec and restarting the run.

## I6 — Everything is audited

Every event — tool call, delegation, HITL request and response,
reshape, permission denial, budget exceedance — produces an audit
record. The audit log is append-only and is the canonical record
of what happened. Forensic replay reproduces the live state
exactly because of this.

## What the mesh does *not* guarantee

The mesh cannot guarantee that an LLM produces accurate output,
that a tool's downstream side effect is intended, or that a human
approver makes a wise decision. Those are out-of-scope; the mesh's
contract is about *enforcement of the declared envelope*, not
*correctness of the work product*.
