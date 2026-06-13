# The hybrid orchestrator

The orchestrator is the control plane of an agent-native application,
analogous to Kubernetes in a cloud-native deployment. It manages the
application graph's structural state — which agents are active, how
they are wired, and how the graph may evolve — without mediating
individual tool calls (that is the mesh's job).

## The design spectrum

Orchestrators sit on a spectrum between two ends:

- **Deterministic.** Pre-programmed workflow logic with threshold-based
  triggers. Reliable, predictable, limited to scenarios the designer
  anticipated.
- **Agentic.** An LLM-based orchestrator that reasons about how to
  decompose tasks, which agents to engage, and when to reshape the
  graph. Flexible, adaptive, can handle situations that were never
  predefined.

Neither end is universally correct. A purely deterministic orchestrator
cannot adapt to genuinely novel situations; a purely agentic
orchestrator is too unpredictable for applications where every step has
to be defensible.

## Hybrid in practice

A hybrid orchestrator combines both: deterministic routing for
well-understood workflows, with an agentic component that takes over
for novel or ambiguous tasks. In a research-assistant system, for
example, requests of the form `quick-read: ...` or `summarise: ...` can
be routed deterministically straight to a single-purpose agent, while
free-form research requests flow through a reasoning coordinator that
decomposes them and delegates.

This pattern mirrors the MAPE control loop of autonomic computing, but
with agents capable of reasoning beyond predefined control logic.

## Prototype control-plane hook

ANR keeps the orchestrator intentionally small. It performs entry-point
routing and bounded graph reshape, and it also observes the mesh event
stream. To make the paper's control-plane role concrete without building
a production planner, the prototype includes one deterministic
intervention: when the mesh records repeated boundary interventions for
the same caller/tool pair, the orchestrator emits an
`orchestrator_decision` audit event and requires human review for future
attempts on that interaction. This demonstrates aggregate-event
interpretation and envelope tightening while keeping the runtime compact.
