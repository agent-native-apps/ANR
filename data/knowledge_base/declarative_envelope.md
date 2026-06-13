# Declarative application specification

An agent-native application is specified declaratively, but the
specification cannot be a static blueprint of desired state the way a
Kubernetes manifest is. Because agents are autonomous decision-makers,
the specification must define an **envelope of permitted runtime
configurations**: the initial structure together with the rules
governing how that structure is allowed to evolve during execution.

## The two concerns

**Application structure.** Which agents, tools, data sources, and
human-in-the-loop checkpoints exist; which nodes are entry points; and
which edges between them are predefined (the rest emerge dynamically at
runtime from agent reasoning).

**Behavioural envelope.** The rules that constrain runtime evolution:

- which tools and data sources each agent may access,
- the autonomy granted to each agent, from broad latitude to tightly
  constrained sequences,
- where human oversight is mandatory, discretionary, or absent,
- which interactions cross the application boundary and what ingress or
  egress policies apply there,
- whether agents may spawn sub-agents, and the type, number, and
  inherited permission scope,
- resource and execution limits (cost, runtime, delegation depth),
- the degree to which the orchestrator may reshape the graph at
  runtime.

## Constitution, not blueprint

A Kubernetes manifest implicitly says "the running system should match
this." An agent-native specification says "the running system must
remain within this envelope, but may vary freely inside it." The
specification is a static authoring artefact; the running graph is
plastic within the bounds it permits. Authority flows in one direction:
the specification grants the orchestrator latitude, and the orchestrator
exercises it against the live graph.

## Open problem

An open, standardised syntax for expressing such specifications — rich
enough to describe a full behavioural envelope while remaining
approachable — has not yet been established. Mature cloud-native
manifests standardised a narrower problem; the agent-native analogue is
still in an early, fragmented state.
