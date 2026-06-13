# The agent mesh

An **agent mesh** is a dedicated infrastructure layer that mediates
application-level interactions covered by the behavioural envelope:
tool invocations, delegations between agents, data-source queries,
cross-boundary messages, and human-in-the-loop checkpoints. It is the
data-plane counterpart to the orchestrator.

## Why a mesh is necessary

A microservice executes deterministic code. If it is programmed to log
every request, it will. An agent cannot be relied on to obey the same
instruction consistently — you cannot put "always request human approval
before executing financial transactions" in the system prompt and expect
compliance. Safety-critical constraints must therefore be enforced by
infrastructure that operates independently of the agent's behaviour.

## What a mesh enforces

1. **Behavioural envelope.** Is this caller permitted to invoke this
   tool with these parameters, given its declared scope?
2. **HITL checkpoints.** Four trigger patterns span from fully
   deterministic to fully autonomous: predefined deterministic,
   conditional deterministic, agent-initiated, and platform-initiated.
3. **Boundary policies.** Tools can be marked as `ingress` or `egress`
   boundary exchanges. `envelope.boundary_policies` inspect a declared
   string argument and produce one of four outcomes: `allow`, `block`,
   `sanitize`, or `escalate_to_human`.
4. **Resource limits.** Cost, runtime, tool-call and LLM-call budgets,
   enforced at the mesh rather than requested in prompts.
5. **Audit trails.** Every intercepted interaction produces a structured
   record, which later supports observability, forensics, and
   compliance.

## Analogy

A service mesh such as Istio or Linkerd enforces mutual TLS, retries,
and traffic splitting transparently on every request between
microservices. The agent mesh plays the analogous role for agent-
centric concerns, with the crucial addition that its enforcement goes
beyond plumbing into policy-as-code over stochastic components.
