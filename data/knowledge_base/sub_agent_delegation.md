# Sub-agent delegation semantics

`delegate(target_agent, task)` and `spawn_parallel(of_agent, tasks)`
are the two ways an agent hands work to another agent. Both go
through the mesh; both produce audit records; both are bounded by
the caller's envelope.

## Stateless children

Each delegation is a fresh agent invocation. **The child sees only
the task string it was handed** — no prior conversation history,
no memory of earlier delegations, no shared scratchpad. If the
parent wants the child to know about an earlier finding, the parent
must encode that finding in the task string.

This is deliberate. Statelessness gives the parent precise control
over what the child sees, keeps token costs predictable, and means
that a child's prompt-injection or misbehaviour cannot leak into
sibling subtrees.

## The Python call stack is the delegation tree

`mesh.invoke(...)` for a delegation is a nested `await`. The whole
runtime is one process, one asyncio event loop, one thread; there
is no message queue, no IPC, no scheduling layer between a parent
and its children. The Python call stack at any moment **is** the
live delegation tree. This is also why no locks are needed on
shared state like `mesh.totals` — `await` is the only suspension
point.

## delegate vs spawn_parallel

`delegate` is sequential — one child runs, the parent awaits its
result, then proceeds. `spawn_parallel` runs N children
concurrently and returns when all complete. The envelope caps how
many parallel instances a given caller may spawn for a given child
type; the mesh refuses anything over the cap.

Prefer `spawn_parallel` when the sub-tasks are genuinely
independent — three field assessments on three sites, three
researchers on three sub-questions. Use sequential `delegate` when
later children depend on earlier results.

## Returning to the parent

A child returns a single string. The parent receives that string
back from `delegate(...)` or as one element of the list returned by
`spawn_parallel(...)`. The parent decides how to fold that string
into its own reasoning.
