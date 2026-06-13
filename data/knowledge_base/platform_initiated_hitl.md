# Platform-initiated HITL (Pattern 4)

Of the four §3.2.2 checkpoint patterns, **platform-initiated** is
the only one the agent cannot anticipate. The other three are
declared at the tool call site:

- *Predefined* fires on every invocation of a named tool.
- *Conditional* fires when a boolean over the call args is true.
- *Agent-initiated* fires when the agent itself calls
  `request_human_review`.

Pattern 4 fires when something about the *run as a whole* crosses a
threshold the platform — not the agent — is tracking.

## When it fires

Each `platform_initiated` checkpoint has a `trigger` expression
evaluated against a small read-only set of run-level metrics:

- `llm_calls` — total LLM calls so far this run.
- `tool_calls` — total tool invocations so far this run.
- `cost_usd` — cumulative LLM cost.
- `runtime_sec` — wall-clock seconds since the run started.
- `count_of('<tool_name>')` — how many times a given tool has been
  invoked this run.

When the expression flips to true, the mesh fires the checkpoint at
the *next* mesh interception, regardless of which agent is running.
Each platform checkpoint fires at most once per run; the mesh's
`_fired_platform` set tracks which have already triggered.

## Why this matters

Pattern 4 is the safety net for runs that drift quietly. An agent
that calls `mark_urgent` four times in a single inbox triage probably
miscalibrated. An audit pipeline that crosses $0.75 of LLM spend may
be running unnecessary fan-out. Neither problem will trip any
predefined or conditional checkpoint — only a run-level signal
catches it. The platform pauses, surfaces the metric to the
operator, and asks for confirmation before continuing.

## What it is not

Pattern 4 is not a budget enforcement mechanism. Hard budgets
(`total_cost_usd`, `total_runtime_sec`, `total_llm_calls`,
`max_tool_calls`) live in `resource_limits` / `autonomy` and abort
the run outright. The platform-initiated checkpoint is a softer
"are you still on the right track" interruption that the operator
can wave through.
