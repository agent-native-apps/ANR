# The audit log

The mesh writes one JSON-lines record per intercepted event to
`output/audit.jsonl`. The audit log is the **source of truth** for
anything observational — the visualizer, post-run forensics, and any
compliance review work read this file and only this file.

## What gets logged

Every call that crosses the mesh produces a record:

- **tool_call** — a tool was invoked (MCP, HTTP, or pseudo-tool).
  Records the caller, the tool, the arguments, the result or error,
  the latency, and the envelope context (sub-agent count, depth,
  cumulative tool calls).
- **delegate / spawn_parallel** — one agent called another. Records
  the parent, child, task, and depth.
- **hitl_request / hitl_response** — a checkpoint fired. Records the
  pattern (`predefined` / `conditional` / `agent_initiated` /
  `platform_initiated` / `boundary`), the prompt, the decision
  (`approve` / `reject` / `modify`), and any modified arguments.
- **boundary_decision** — a boundary policy inspected an ingress or
  egress payload. Records the policy id, direction, inspected field,
  match count, enforcement plane, and enforcement outcome (`allow`,
  `block`, `sanitize`, or `escalate_to_human`).
- **orchestrator_decision** — the control plane interpreted recent mesh
  events and changed how future interactions are governed. The prototype
  currently emits this when repeated boundary interventions cause the
  orchestrator to require human review for future attempts by the same
  caller/tool pair.
- **reshape** — a graph mutation succeeded. Records the kind
  (`spawn_instance`, `acquire_capability`, `instantiate_template`),
  the caller, and the new node id (if any).
- **permission_denied** — a call was refused because the caller is
  not authorised. Includes the attempted tool and the agent that
  tried it.
- **budget_exceeded** — a per-envelope budget was hit
  (`max_tool_calls`, `total_cost_usd`, `total_runtime_sec`, etc).

## Record shape

Each record has `ts`, `run_id`, `kind`, `caller`, `tool`, `args`,
`outcome`, `totals`, `mesh_context`, and event-specific fields. The
visualizer's `apply_record(state, rec)` function consumes one record at
a time to fold the live state; replaying the file from the start
reproduces any past state exactly. Time-travel debugging in the
visualizer relies on this property.

## What is *not* logged

The mesh does not log internal LLM token streams. The LiteLLM client
emits its own usage telemetry which is summarised into the audit
log's `tool_call` cost field — but the prompts and intermediate
reasoning are not captured. If you need that level of trace, run
with LiteLLM debug logging enabled.
