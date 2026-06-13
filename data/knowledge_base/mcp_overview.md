# The Model Context Protocol (MCP): a short overview

MCP is an open protocol introduced by Anthropic for connecting LLM-based
agents to external tools and data sources in a standardised way. It
defines a host-client-server architecture: a host application manages
client connections to one or more servers, and each server exposes some
combination of tools, data resources, and prompt templates.

## Why MCP matters

Before MCP, every agent framework that wanted to integrate with, say,
GitHub or a local filesystem had to build its own bespoke bridge. The
result was N × M integration work and very little reuse. MCP reframes
this as N + M: one protocol, with each tool provider writing a server
and each agent host writing a client.

## Core capabilities

- **Tools** are functions the agent can call. Each tool has a name, a
  description, and a JSON-schema for its input.
- **Resources** are read-only data the agent can fetch by URI.
- **Prompts** are reusable prompt templates the server publishes.
- **Sampling** (the less-used inverse direction) lets a server request
  LLM completions from the host, enabling server-side reasoning loops.

## Where it fits in the agent-native paradigm

MCP is the standardisation layer between an agent and its tools. It does
not replace the orchestrator, the behavioural envelope, or the agent
mesh; it gives the mesh a uniform dialect to speak to tool providers.
In an agent-native runtime, the mesh is the only MCP client, because the
mesh is the chokepoint at which permissions, HITL checkpoints, and audit
records are applied.
