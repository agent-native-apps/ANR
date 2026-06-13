# Spec lifecycle: load → validate → compile → run

A YAML spec file becomes a running multi-agent application through
four phases. Each phase has a clear failure mode and a clear
artifact it produces.

## Phase 1: load

`anr.loader.load_spec(path)` reads the YAML file and parses it
into a `Spec` pydantic model. Loading fails on malformed YAML; the
loader does not interpret semantic fields yet.

## Phase 2: validate

Pydantic validation runs as part of `Spec(**raw)`. This catches
schema violations: missing required fields, wrong types,
disallowed enum values. Several cross-field checks also run here —
for instance, every `may_delegate_to` target must be a declared
agent or template, every `tools` entry must be a declared tool, and
every HITL checkpoint's `when.tool` must reference a real tool.

A spec that survives Phase 2 is structurally valid, but nothing
about the runtime exists yet.

## Phase 3: compile

`anr.compiler.Compiler.build(spec, output_dir)` turns the validated
`Spec` into a `Graph` holding one `Orchestrator`, one `Mesh`, one
`MCPPool`, and N `AgentNode` instances. Compilation:

- spawns the MCP child processes declared by `tools.*.server` blocks
- registers the appropriate `AgentNode` builder for each agent
  (`AGENT_BUILDERS[kind]`) and creates the node instances
- builds the mesh's permission tables, HITL trigger tables, and
  reshape rule tables from the envelope
- opens the audit log writer

If any MCP server fails to start, compile fails fast — the runtime
will not begin a run with a missing tool surface.

## Phase 4: run

`graph.orchestrator.dispatch(task)` routes the input task to the
entry-point agent (or to a deterministic-route target if the task
matches an `input_regex`) and awaits the final string. Throughout
the run, every cross-node call goes through `mesh.invoke(...)` and
every event lands in the audit log. The run terminates when:

- the entry-point agent returns, or
- a hard budget is exceeded, or
- the orchestrator is signalled to stop (e.g. by an operator).

After the run, the MCP child processes are torn down and the audit
log is closed. The output artifacts (notes, drafts, SITREPs, etc.)
remain under the output directory.
