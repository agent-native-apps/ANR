# Human-in-the-loop trigger patterns

The agent mesh enforces human-in-the-loop oversight at runtime. Four
triggering patterns span a spectrum from fully deterministic to fully
autonomous:

## 1. Predefined deterministic

Rules defined in the application specification and enforced
unconditionally by the mesh. Example: "every call to `write_note`
requires human approval." The agent has no say. The mesh intercepts the
call before it reaches the target and fires the checkpoint. This is the
strongest guarantee because it does not depend on the agent's reasoning
at all.

## 2. Conditional deterministic

Rules defined in the specification with conditional logic evaluated
against the current call. Example: "tool invocations with a `cost`
parameter above a threshold require approval" or "fetch_url calls to
domains outside the allow-list require approval." The mesh inspects
call content and fires the checkpoint when the condition matches. Like
pattern 1, the rule and its enforcement live outside the agent.

## 3. Agent-initiated

The agent encounters uncertainty or recognises a high-stakes decision
and requests human review through its own reasoning. The mesh provides
the mechanism (a `request_human_review(reason)` pseudo-tool), but the
trigger originates from the agent. This complements patterns 1 and 2
by catching cases the specification did not anticipate.

## 4. Platform-initiated

The orchestrator or mesh monitors execution patterns (cumulative cost,
unusual tool-use sequences, runtime anomalies) and proactively triggers
human review even when no spec rule required it and the agent did not
request it. This is the safety net against runaway behaviour.

## Defence in depth

Patterns 1 and 2 are safety-critical — they must be enforced by
infrastructure, not delegated to agents. Patterns 3 and 4 are
complementary, catching situations predefined rules did not anticipate.
Together they give an application multiple independent pathways to
human oversight, aligned with the risk profile of the agent's actions.
