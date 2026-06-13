# Agent-Native Runtime (ANR)

Reference implementation of the agent-native application paradigm — companion
to the paper *Agent-Native Applications: A New Computing Paradigm*. A YAML
spec compiles into a running multi-agent application governed by a declarative
behavioral envelope. Policy-relevant interactions flow through a
policy-enforcing **mesh**, while a lightweight orchestrator provides the
control-plane hooks for bounded graph evolution. Proof-of-concept, not
production.

## What you'll see

ANR runs scenario tasks against YAML application specifications. Each spec
instantiates an agent graph whose agents delegate to each other, call tools
through a single policy-enforcing agent mesh, and pause for approval at
human-in-the-loop (HITL) checkpoints. Every step — each tool call, delegation,
boundary decision, and graph reshape — is written to an audit log that can be
replayed step-by-step in the browser visualizer.

## Paper companion map

The paper's **Companion Prototype: ANR** section is represented here by the
runtime, the YAML application specifications, and the committed campaign
logs. The table below maps the main ANR concepts and reported artifacts from
the paper to the corresponding repo artifacts:

| Paper concept or artifact | Where to inspect it |
|---|---|
| Declarative application specification and behavioral envelope | [`specs/research_assistant.yaml`](specs/research_assistant.yaml) for the compact baseline; [`specs/README.md`](specs/README.md) for a field-by-field guide |
| Emergency first-response use case (§6.1) | [`specs/emergency_response.yaml`](specs/emergency_response.yaml) |
| Cross-organizational supply-chain use case (§6.2) | [`specs/supply_chain.yaml`](specs/supply_chain.yaml) |
| Campaign measurements in the ANR table | [`artifacts/campaign/`](artifacts/campaign/README.md), with 25 calibrated and 25 fault-injected runs for each reported scenario |
| Extra HITL demonstration not reported in the paper table | [`specs/inbox_triage.yaml`](specs/inbox_triage.yaml) |

The reported scenarios are `research_assistant`, `emergency_response`, and
`supply_chain`. `inbox_triage` is included as an additional runnable example,
but it is not part of the paper's campaign measurements.

## Quickstart

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync          # 1. install deps into .venv/
uv run anr list  # 2. see the four application specs, what they do, and a ready task each
```

**3. See it run first — no API key needed.** Replay a recorded run in the
visualizer and step through it with the cursor controls:

```bash
uv run anr-viz specs/emergency_response.yaml examples/audits/paper_alignment_audit.jsonl
# open http://127.0.0.1:8080
```

**4. Run it live** (needs a provider API key):

```bash
cp .env.example .env   # then add ANTHROPIC_API_KEY (or OPENAI_API_KEY, etc.)
uv run anr run --viz specs/research_assistant.yaml   # task is optional — uses the spec's example_task
```

`anr run` preflights the key the spec needs and prints what to set if it's
missing. Omit the task to use the spec's built-in `example_task`, or pass your
own as a final quoted argument.

## Run a demo

Four application specs ship under `specs/`. For a guide to how the YAML fields
map to the paper's graph, envelope, HITL, boundary, and reshape concepts, see
[`specs/README.md`](specs/README.md). Each command below is copy-paste-ready —
fixture data is already in `data/`. The trailing task is optional (each spec
carries the same one as its `example_task`); it's shown here so you can see and
tweak it.

### `research_assistant` — introductory delegation demo

Coordinator → researcher + writer over a markdown corpus. Predefined HITL on
`write_note`; exercises R1 (parallel researchers), R2/R3 (runtime tool
acquisition), R4 (`focused_researcher` template).

```bash
uv run anr run --viz specs/research_assistant.yaml \
    "Write a short briefing on the agent-native application paradigm based on the knowledge-base corpus."
```

### `inbox_triage` — parallel sub-agents + platform-initiated HITL

Triager + parallel extractors + drafter. Rate threshold on `mark_urgent`
fires a platform-initiated HITL checkpoint.

```bash
uv run anr run --viz specs/inbox_triage.yaml \
    "Triage this morning's eight inbox messages (data/inbox/e001..e008). Mark any production incidents urgent, extract action items in parallel, and draft a single status reply to the on-call lead."
```

### `emergency_response` — §6.1 use case

Incident command application: triage / field assessment / logistics /
communication / hazmat coordinator. Parallel field assessments (R1),
mid-incident `hazmat_coordinator` instantiation (R4), runtime acquisition of
`consult_external_agency` (R2), mandatory HITL on evacuation orders.

```bash
# terminal 1 — visualizer (--port if 8080 is busy)
uv run anr-viz specs/emergency_response.yaml --port 8090

# terminal 2 — runtime (HITL routed to the browser)
ANR_HITL=ui uv run anr run specs/emergency_response.yaml \
    "Survey today's open incidents (post-quake at 06:42). Triage them, run parallel field assessments on the high-severity sites, instantiate a hazmat coordinator if a chemical signal surfaces, and produce a consolidated SITREP."
```

### `supply_chain` — §6.2 use case

Procurement application with a deterministic Python-script sovereignty
guardrail (`comms_guardrail`) beside LLM agents in the same mesh, plus
mesh-enforced ingress filtering on supplier-authored offers and egress
boundary policies on outbound supplier messages. One egress policy is
data-context-aware: after an agent reads confidential inventory or market
context, the mesh escalates supplier messages for human review even when
the text has no obvious regex leak. `supplier_negotiator` template (R4);
HITL on long-term contracts and large purchase commits.

```bash
# terminal 1
uv run anr-viz specs/supply_chain.yaml --port 8090

# terminal 2
ANR_HITL=ui uv run anr run specs/supply_chain.yaml \
    "Run this quarter's procurement cycle: full inventory survey, market read on flagged components, source the controller_board_v3 (target ~10,000 units). Meridian, a newly discovered supplier, has proposed a 36-month exclusive — handle it appropriately. Produce an award recommendation."
```

### Task conventions

Tasks aren't free-form prompts. Each spec's agents expect the task to *name
the artifacts to work on* — corpus, inbox, incident set, supplier batch —
plus the operation (triage, audit, source, draft) and any specific
identifiers (incident times, supplier names, component SKUs). The prefilled
tasks above all follow this shape; use them verbatim or as templates.

## Visualizer & HITL

`--viz` spawns the visualizer alongside the runtime and opens
`http://127.0.0.1:8080`. Run them separately to drive HITL from the browser,
pick a non-default port, or watch a finished run:

```bash
uv run anr-viz specs/<spec>.yaml [audit.jsonl] [--port 8090] [--no-browser]
```

HITL prompts go to stdin by default. Set `ANR_HITL=ui` to route them through
the visualizer instead — the mesh writes `output/hitl/req-<id>.json` and the
viz surfaces an approve / reject / modify modal. `ANR_HITL_TIMEOUT_SEC=<n>`
auto-rejects after *n* seconds. For unattended runs, `ANR_HITL=auto` resolves
every checkpoint with a fixed scripted decision (approve by default;
`ANR_HITL_AUTO_ACTION=reject` flips it) — interception and audit are identical
to a human-driven run, and the audit record notes the decision was scripted.

To inspect the paper-alignment mechanics without LLM calls or live HITL,
replay the canned audit artifact:

```bash
uv run anr-viz specs/emergency_response.yaml examples/audits/paper_alignment_audit.jsonl --port 8090
```

## Prototype scope

ANR demonstrates the paper's application-layer and trust-layer mechanics:
portable YAML specs, per-agent authority, mesh-mediated tool calls and
delegation, ingress/egress boundary decisions including data-context-aware
egress review, HITL checkpoints, bounded runtime graph changes,
control-plane audit events, and replayable audit logs.
It intentionally does not implement production A2A/ANP, distributed discovery,
verifiable credentials, cloud or edge placement, distributed inference,
network QoS, or scalable deployment operations.

## Parallel runs

The runtime writes everything under `./output/` and truncates `audit.jsonl`
at startup. Two concurrent runs in the same directory will clobber each
other's audit log and HITL inbox. Give each its own directory:

```bash
# terminal 1
uv run anr run --output-dir ./output/er specs/emergency_response.yaml "<task>"
uv run anr-viz specs/emergency_response.yaml ./output/er/audit.jsonl --port 8090

# terminal 2 (independent process — its own MCP children, its own audit)
uv run anr run --output-dir ./output/ra specs/research_assistant.yaml "<task>"
uv run anr-viz specs/research_assistant.yaml ./output/ra/audit.jsonl --port 8091
```

## Paper measurement data and campaigns

The audit logs behind the paper's campaign runs are committed under
[`artifacts/campaign/`](artifacts/campaign/README.md). For each reported
scenario, there are 25 calibrated runs using the spec as declared and 25
fault-injected runs in which every agent sees all declared tool schemas while
mesh-side authority remains scoped. That gives 50 runs per scenario and 150
runs total across `research_assistant`, `emergency_response`, and
`supply_chain`.

The calibrated directories are named after the scenario
(`research_assistant/`, `emergency_response/`, `supply_chain/`). The
fault-injected directories add `_expose_all/`. In each directory,
`manifest.jsonl` is a one-line-per-run index, while each
`run-NNN/audit.jsonl` is the complete audit log used to compute the table
counts. Any committed run can be replayed step-by-step in the visualizer:

```bash
uv run anr-viz specs/supply_chain.yaml artifacts/campaign/supply_chain/run-001/audit.jsonl
```

To recompute a compact table-style summary from the committed logs:

```bash
uv run python scripts/summarize_campaign.py
```

To run fresh campaigns instead: each run is an isolated process writing its
own `output/campaign/<name>/run-NNN/` directory, with HITL decided by the
scripted `ANR_HITL=auto` backend (approve-all, recorded as scripted in the
audit log). The only thing that varies across runs of a scenario is LLM
sampling — same spec, same `example_task`, same model.

```bash
# calibrated; repeat for emergency_response.yaml and supply_chain.yaml
uv run python scripts/campaign.py specs/research_assistant.yaml -n 25 --jobs 2

# fault-injected: every agent sees ALL declared tool schemas while
# mesh-side authority stays scoped (a deliberately miscalibrated node);
# writes to a separate <name>_expose_all/ root, recorded in the manifest
uv run python scripts/campaign.py specs/research_assistant.yaml -n 25 --jobs 2 --expose-all
```

At Haiku pricing a 25-run campaign costs roughly \$1.70--4.70 per scenario
root and completes unattended in minutes to tens of minutes, depending on
run length.

Before publishing regenerated artifacts, strip local checkout paths from text
logs:

```bash
uv run python scripts/sanitize_artifact_paths.py output/campaign
```

## Layout

```
src/
  anr/              runtime: spec, compiler, mesh, policies, agents, MCP client, CLI
  anr_viz/          FastAPI + HTMX live visualizer (tails output/audit.jsonl)
  mcp_servers/      FastMCP tool servers (tools / inbox / incident / procurement)
specs/              application specs, spec guide, supply_chain_agents.py, prompts/
data/               read-only fixtures (knowledge_base / inbox / incidents / suppliers / inventory)
examples/audits/    canned audit logs for visualizer replay without live LLM calls
artifacts/campaign/ committed audit logs behind the paper's campaign runs (150 runs)
output/             runtime writes here: audit.jsonl, hitl/, notes/, sitreps/, campaign/, …
scripts/            campaign.py, sanitize_artifact_paths.py, auto_approve.py,
                    summarize_campaign.py
```
