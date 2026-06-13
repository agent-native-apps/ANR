# Tool kinds

Just as agent `kind` is pluggable (`native` / `script` / future),
tool `kind` is pluggable. The compiler reads each entry under
`tools` and registers a dispatcher keyed by `kind`. The mesh
intercepts every dispatch identically regardless of which kind is
in play.

## kind: mcp

The standard production-shape tool: a separate child process
speaking MCP over stdio. Declared with a `server` block that gives
the launch command and any extra args; the runtime spawns the
child during compile, multiplexes calls over the connection, and
tears down on shutdown.

The MCP child runs in its own OS process. It is sandboxed by
whatever environment the host gives it — typically a restricted
filesystem subtree via `ANR_DOCS_DIR`, `ANR_OUTPUT_DIR`, and
similar env vars. The mesh layers policy *on top* of this
sandboxing: even a "legal" filesystem read may be refused by the
mesh's permission table.

## kind: http

A direct REST call, no MCP round-trip. Useful for tools whose
implementation is "GET this URL" or "POST this JSON" — keeping
them inline avoids a child-process boundary for trivial behaviour.
The HTTP tool kind enforces `allowed_domains` if declared on the
bound data source.

## kind: pseudo-tool

`delegate`, `spawn_parallel`, `request_grant`,
`instantiate_template`, and `request_human_review` are pseudo-tools
the mesh injects into every agent's effective tool surface. They
are not declared in the spec's `tools` block — they exist because
the runtime exists. They dispatch to the orchestrator rather than
to an external endpoint.

## Why this matters

A reviewer reading the spec sees one uniform shape for any
capability the agent can reach. Whether it is an MCP server, an
HTTP endpoint, or a built-in pseudo-tool, the permission table
governs it, the HITL table can gate it, the audit log records it.
The runtime does not give MCP a special role; it just happens to
be the most common kind.
