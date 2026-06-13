# Agent-Native Application Specification

This directory holds the canonical JSON Schema for the agent-native
application specification. The schema is the portable artifact: any
conforming runtime — implemented in Python, Go, Rust, TypeScript, or
anything else — consumes a YAML or JSON document that validates
against it.

## Files

- `anr-spec-v0.json` — JSON Schema (2020-12) for spec_version `0.1`.

## Versioning

Every conforming specification document declares its target version:

```yaml
spec_version: "0.1"
application:
  name: my-application
```

A runtime validates the document against `anr-spec-v<major>.json` for
the declared version. This repository's Python reference runtime also
checks `spec_version` against its own supported-versions set in
`src/anr/spec.py`.

## Regenerating the schema

The committed schema is derived from the pydantic reference models in
`src/anr/spec.py`. After changing those models:

```bash
uv run python -m anr.schema > docs/schema/anr-spec-v0.json
```

The pydantic models and the JSON Schema are two representations of the
same contract — the pydantic classes are one runtime's binding; the
JSON Schema is what any other runtime targets.

## Prototype boundary

This schema is a compact demonstration contract for the paper's
application-layer concepts: graph structure, authority, HITL, boundary
policies, resource limits, and bounded runtime graph evolution. It is
not a full standard for A2A/ANP, discovery, identity credentials, cloud
placement, distributed inference, networking, or production operations.

## Extension points

Two fields are open discriminators designed to admit
runtime-registered extensions:

- `tools[].kind` — currently `mcp`, `http`, `builtin`. A runtime may
  register additional tool kinds (e.g. `grpc`, `a2a`) by extending
  the schema and its own dispatch table.
- `agents[].kind` — currently `native`, `script`. A runtime may
  register additional node kinds (e.g. `langgraph_subgraph`,
  `a2a_remote`, `openai_agents_sdk`) that wrap third-party frameworks
  or remote endpoints behind the same node interface. The mesh,
  envelope, and orchestrator do not need to know which kind a node is
  — the contract is `async run(task, ctx) -> str`.

Extensions are out of band: a runtime declares which kinds it
supports, and a spec author opts in only to those the target runtime
understands.
