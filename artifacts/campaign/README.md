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

Scenarios: `research_assistant`, `emergency_response`, `supply_chain` —
25 runs per scenario per condition (150 runs total). All runs used
`anthropic/claude-haiku-4-5` with each spec's built-in `example_task`,
and the scripted HITL backend (`ANR_HITL=auto`, approve-all); every HITL
decision in the logs is marked as scripted.

## Reading a run

Each audit line is one mesh-mediated event. The `kind` field is the key:
`agent_turn` (LLM call), `tool_call` (allowed action), `hitl_checkpoint`
(human-review episode + decision), `reshape` (graph evolution),
`boundary_decision` (ingress/egress allow / sanitize / block),
`policy_refusal` (action refused: permission, delegation, or budget),
`tool_error` (allowed action that failed). Refusals are returned to the
agent *before* dispatch, so a refused action never has a corresponding
`tool_call`.

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
