# Campaign data behind the paper's runs

Raw audit logs from the unattended runs reported in the paper's companion
prototype section. Each `run-NNN/audit.jsonl` is the complete, append-only
audit log the mesh wrote during that run. Local absolute checkout paths have
been sanitized to repo-relative paths with `scripts/sanitize_artifact_paths.py`.

## Layout

```
<scenario>/                       calibrated runs (specs as declared)
<scenario>_expose_all/            fault-injected runs (ANR_EXPOSE=all:
                                  all tool schemas visible to every agent,
                                  mesh-side authority unchanged)
  manifest.jsonl                  one line per run: exit code, duration,
                                  event count, cost, expose_all flag
  run-NNN/audit.jsonl             the audit log (source of every count)
  run-NNN/run.log                 console output of the run
```

Scenarios: `research_assistant`, `emergency_response`, `supply_chain`.
For each scenario, the calibrated directory contains 25 runs using the spec as
declared, and the matching `_expose_all` directory contains 25 fault-injected
runs. That is 50 runs per scenario and 150 runs total. All runs used
`anthropic/claude-haiku-4-5` with each spec's built-in `example_task`,
and the scripted HITL backend (`ANR_HITL=auto`, approve-all); every HITL
decision in the logs is marked as scripted.

Concretely:

| Scenario | Calibrated logs | Fault-injected logs |
|---|---|---|
| `research_assistant` | `research_assistant/run-001..run-025/` | `research_assistant_expose_all/run-001..run-025/` |
| `emergency_response` | `emergency_response/run-001..run-025/` | `emergency_response_expose_all/run-001..run-025/` |
| `supply_chain` | `supply_chain/run-001..run-025/` | `supply_chain_expose_all/run-001..run-025/` |

The full logs are the `run-NNN/audit.jsonl` files. The `manifest.jsonl` files
are indexes with run status, elapsed time, event count, cost, and fault mode.

## Reading a run

Each audit line is one mesh-mediated event. The `kind` field is the key:
`agent_turn` (LLM call), `tool_call` (allowed action), `hitl_checkpoint`
(human-review episode + decision), `reshape` (graph evolution),
`boundary_decision` (ingress/egress allow / sanitize / escalate / block),
`policy_refusal` (action refused: permission, delegation, or budget),
`tool_error` (allowed action that failed). Refusals are returned to the
agent *before* dispatch, so a refused action never has a corresponding
`tool_call`.

## Mapping to the paper table

The ANR table in the paper reports descriptive counts derived from these audit
logs, not a performance or security benchmark. The main quantities map to audit
records as follows:

| Paper-table quantity | Audit records counted |
|---|---|
| Mesh-mediated tool calls per run | `kind == "tool_call"` |
| HITL episodes per run | `kind == "hitl_checkpoint"` |
| Graph-reshape ops per run | `kind == "reshape"` |
| Boundary decisions | `kind == "boundary_decision"`, grouped by `outcome.enforcement_outcome`; the paper row reports allow / sanitize / block, while raw logs also include `escalate_to_human` |
| Agent actions refused | `kind == "policy_refusal"` |

For the refusal rows, the left side of the paper's `x -> y` notation is the
number of out-of-envelope actions proposed by agents. The right side is the
number realized after mesh mediation; for these committed campaigns it is zero
because refusals happen before dispatch.

To recompute a compact summary from the committed logs:

```bash
uv run python scripts/summarize_campaign.py
```

To replay any run step-by-step in the visualizer:

```bash
uv run anr-viz specs/supply_chain.yaml artifacts/campaign/supply_chain/run-001/audit.jsonl
```

Before publishing freshly regenerated logs, remove local absolute checkout
paths from text artifacts:

```bash
uv run python scripts/sanitize_artifact_paths.py output/campaign
```

To run fresh campaigns instead of replaying these logs, see
"Paper measurement data and campaigns" in the top-level README.
