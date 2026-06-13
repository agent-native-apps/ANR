# The visualizer (anr-viz)

`anr-viz` is a FastAPI + HTMX web app that renders the declared
agent graph from a spec and overlays live activity by tailing the
mesh audit log. It is mostly a **read-side** observer of the
runtime — the runtime process never depends on the visualizer for
correctness.

## Architecture

`AuditTailer` keeps the full ordered list of raw audit records.
`apply_record(state, rec)` folds one record into a `LiveState`
holding per-agent counters, the active delegation tree, recent
tool calls, HITL pending/decided queues, and reshape history.

Time-travel works by replaying `records[:cursor+1]` into a fresh
`LiveState` whenever a cursor is set server-side. Clicks on the
cursor buttons hit `/control/cursor?op=...` and the response
includes `HX-Trigger: cursor-changed`; the svg, sidebar, and
controls panels all listen for that event and refresh in lockstep.

## The one write path: HITL

Most of the visualizer is read-only. The exception is HITL.
When the runtime is launched with `ANR_HITL=ui`, the mesh's
prompter is `UIPrompter` (in `anr/hitl.py`), which writes pending
requests as `output/hitl/req-<id>.json` and polls for a matching
`res-<id>.json`. The visualizer's `/state/hitl` endpoint
enumerates the pending request files; the operator's
approve/reject/modify decision is POSTed to
`/control/hitl/decide?id=...`, which writes the response file the
runtime is blocked on.

The default backend remains `StdinPrompter` (terminal); pick the
UI backend per-run via `ANR_HITL=ui uv run anr run …` while
`anr-viz` is running against the same output directory.

## Why a separate process

The runtime and the visualizer are decoupled by design. The audit
log is the contract between them. A run with no visualizer
attached produces the same audit log; a visualizer attached to an
already-finished run can still replay history. This is the same
read-side/write-side split that real production observability
stacks rely on.
