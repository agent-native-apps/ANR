# Runtime graph reshape (R1–R4)

A static spec declares the agents, tools, and edges that exist before
the run starts. Real workloads sometimes call for mutating that graph
while the run is in flight. The runtime supports four bounded reshape
operations, each declared in the envelope's `graph_reshape` block.

## R1 — spawn_instance (parallel fan-out)

A caller spawns N concurrent instances of an existing agent type,
each on its own sub-task. `spawn_parallel(of_agent="researcher",
tasks=[...])` is the canonical pseudo-tool. The envelope caps
concurrency per caller; each instance is a distinct live node in the
graph for the duration of the run and counts toward the caller's
`max_sub_agents` budget.

R1 is the lightest reshape — every instance is bound by the same
permission envelope as the blueprint, so no extra authority is
created.

## R2 — acquire_capability (tool grant)

An agent calls `request_grant(capability="...", reason="...")` to
borrow a tool it does not normally hold. Each grant is short-lived
(scoped to one call or one task) and the spec may require HITL
approval before the grant is issued. R2 is how a normally-read-only
agent can do a one-off write, or how a normally-local agent can
reach an external service for a single verification.

## R3 — same machinery as R2, different framing

The paper distinguishes R3 (acquire data-source access) from R2
(acquire tool access) but the runtime treats them identically — both
go through `request_grant` and produce an `acquire_capability`
reshape audit record. Whether the granted capability is a *tool* or
a *data source* is a label, not a code path.

## R4 — instantiate_template (new node at runtime)

A caller calls `instantiate_template(template="...", parameters={...},
task="...")` to create a brand-new agent node from a declared
template. R4 is the most consequential reshape: it adds an agent
that did not exist at compile time. The mesh therefore **always
fires a HITL checkpoint on R4**, regardless of what the rule says.

Templates have a `parameters` list (filled in at instantiation) and
a `system_prompt_template` that substitutes those parameters. The
instantiated node inherits an envelope derived from the template's
declared tools and delegation targets.
