# Reading the ANR Application Specs

The YAML files in this directory are the paper companion artifacts for the
declarative application specification. They are meant to be read in two ways:
as runnable demos for ANR, and as concrete examples of the paper's behavioral
envelope idea.

## Reviewer Path

Start with the shortest working example, then inspect the two paper use cases:

| Goal | Artifact |
|---|---|
| See the basic spec shape | [`research_assistant.yaml`](research_assistant.yaml) |
| See platform-initiated HITL | [`inbox_triage.yaml`](inbox_triage.yaml) |
| Inspect paper Section 6.1 | [`emergency_response.yaml`](emergency_response.yaml) |
| Inspect paper Section 6.2 | [`supply_chain.yaml`](supply_chain.yaml) |
| Generate the portable JSON Schema | `uv run python -m anr.schema` |
| Replay measured runs | [`../artifacts/campaign/`](../artifacts/campaign/) |

Each spec includes an `application.example_task`, so `uv run anr run
specs/<name>.yaml` can run without inventing a prompt. The README at the repo
root has copy-paste commands for live runs and visualizer replay.

## How To Read A Spec

Most files follow the same shape:

| YAML section | Paper concept | What to inspect |
|---|---|---|
| `application` | Application artifact metadata | The runnable task, version, and description |
| `data_sources` | Data nodes | Which local or remote information sources the graph may touch |
| `tools` | Tool nodes and boundary crossings | MCP bindings, data bindings, ingress/egress markers |
| `agents` | Agent nodes | Role, model or script kind, prompt, tool authority, delegation targets |
| `envelope.autonomy` | Per-agent autonomy bounds | Tool-call budget, sub-agent budget, delegation depth |
| `envelope.resource_limits` | Run-level budget | Cost, runtime, and LLM-call limits enforced by the mesh |
| `envelope.hitl_checkpoints` | Human oversight | Predefined, conditional, agent-initiated, and platform-initiated checkpoints |
| `envelope.boundary_policies` | Boundary mediation | Ingress/egress allow, block, sanitize, and human-escalation rules |
| `envelope.graph_reshape` | Bounded graph evolution | R1 spawn, R2/R3 capability acquisition, R4 template instantiation |
| `agent_templates` | Runtime-instantiable agent types | Template authority and prompt parameters for R4 |
| `orchestrator` | Hybrid control plane | Deterministic shortcut routes and default entry agent |

The key thing to notice is that prompts do not carry the enforcement burden.
Prompts tell agents how to behave; the envelope tells the mesh and
orchestrator what behavior may actually be realized.

## `research_assistant.yaml`

This is the introductory spec. A `coordinator` reads a markdown corpus,
delegates research to `researcher`, then delegates writing to `writer`.

What it demonstrates:

- Scoped tool authority: only `writer` can call `write_note`.
- Predefined HITL: every `write_note` call is reviewed before the write.
- R1: `coordinator` may spawn parallel `researcher` instances.
- R2/R3: `researcher` or `writer` may request a scoped runtime capability.
- R4: `coordinator` may instantiate a `focused_researcher` template.

## `inbox_triage.yaml`

This spec is not one of the paper's main use cases, but it exercises the
fourth HITL pattern in a compact setting. A `triager` reads fixture email,
marks production incidents urgent, spawns extractors in parallel, and asks a
`drafter` to save a reply.

What it demonstrates:

- Platform-initiated HITL: the mesh pauses if `mark_urgent` fires three or
  more times in one run.
- Conditional HITL: suspicious urgency on newsletter-like messages escalates.
- R1: parallel `extractor` instances over separate messages.
- R4: a `domain_expert` template for borderline classification.

## `emergency_response.yaml` (Paper Section 6.1)

This is the executable version of the emergency first-response scenario. It
models an incident-command application with triage, field assessment,
logistics, communications, and a runtime-instantiable hazmat coordinator.

What maps to the paper:

- Incident reports and telemetry are declared as governed data sources.
- `incident_commander` is the entry point, but it cannot dispatch resources,
  order evacuations, write SITREPs, or consult external agencies itself.
- `field_assessment` is the telemetry reader and can be spawned in parallel.
- `logistics` owns dispatch and evacuation tools.
- Evacuation orders and SITREP transmission are predefined HITL gates.
- Large dispatches are conditionally gated.
- `hazmat_coordinator` is instantiated through R4 when the chemical signal
  appears, and may request the `consult_external_agency` capability through R2.

Prototype boundary:

ANR models the application, envelope, mesh, HITL, audit, and reshape semantics.
It does not model the paper's edge-cloud, satellite, UAV, 5G/URLLC, or
distributed cross-agency deployment layers.

## `supply_chain.yaml` (Paper Section 6.2)

This is the executable version of the cross-organizational procurement
scenario. It combines LLM agents with a deterministic `kind: script` guardrail
inside the same mesh.

What maps to the paper:

- `inventory` is confidential internal context; supplier offers are external
  inbound content.
- `read_supplier_offer` is marked `boundary: ingress`, so the mesh can sanitize
  supplier-authored prompt-injection text before the agent sees it.
- `send_supplier_message` is marked `boundary: egress`, so the mesh mediates
  outbound supplier messages.
- `block_supplier_sovereignty_leaks` blocks messages that reveal inventory
  state, urgency, sole-source dependence, or consumption metrics.
- `sanitize_supplier_pricing_strategy` redacts internal pricing constraints.
- `review_supplier_message_after_confidential_context` escalates supplier
  messages after confidential context has been read, even if regex policies do
  not find an obvious leak.
- `comms_guardrail` is deterministic preflight logic; the mesh remains the
  load-bearing enforcement boundary.
- `supplier_negotiator` is an R4 template for non-standard supplier proposals.
- Contract finalization, large purchases, and award recommendations are HITL
  gated.

Prototype boundary:

Supplier agents and cross-organization A2A are represented by local fixture
data and outbound logs. The spec is about application-layer governance, not a
production procurement network.

## Measurement Artifacts

The unattended runs behind the paper's campaign measurements live under
[`../artifacts/campaign/`](../artifacts/campaign/). Their logs are sanitized
with [`../scripts/sanitize_artifact_paths.py`](../scripts/sanitize_artifact_paths.py)
so local absolute checkout paths are replaced by repo-relative paths.
