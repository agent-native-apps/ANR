"""Thin wrapper around the MCP Python SDK for the mesh to use.

The mesh is the only MCP client in the system. For each distinct
`MCPServerConfig` appearing in the spec, we spawn one stdio child process
and keep a `ClientSession` open for the lifetime of the run. Tool calls
made by any agent — after passing the mesh's policy checks — ultimately
hit one of these sessions.

Multiple tools in the spec may share the same server (YAML anchors make
this natural); we deduplicate on `(command, tuple(args))`.
"""

from __future__ import annotations

import json
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from .spec import MCPServerConfig


@dataclass(frozen=True)
class _ServerKey:
    command: str
    args: tuple[str, ...]

    @classmethod
    def from_config(cls, cfg: MCPServerConfig) -> "_ServerKey":
        return cls(cfg.command, tuple(cfg.args))


class MCPPool:
    """A collection of MCP stdio sessions keyed by server launch command."""

    def __init__(self) -> None:
        self._stack = AsyncExitStack()
        self._sessions: dict[_ServerKey, ClientSession] = {}

    async def __aenter__(self) -> "MCPPool":
        await self._stack.__aenter__()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._stack.__aexit__(*exc)

    async def ensure_server(self, cfg: MCPServerConfig) -> _ServerKey:
        key = _ServerKey.from_config(cfg)
        if key in self._sessions:
            return key
        params = StdioServerParameters(
            command=cfg.command,
            args=list(cfg.args),
            env={**(cfg.env or {})} or None,
        )
        read, write = await self._stack.enter_async_context(stdio_client(params))
        session = await self._stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        self._sessions[key] = session
        return key

    async def call(
        self,
        cfg: MCPServerConfig,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> Any:
        key = await self.ensure_server(cfg)
        session = self._sessions[key]
        result = await session.call_tool(tool_name, arguments)
        if result.isError:
            # Flatten the error-content payload into a readable string.
            msg = _content_to_text(result.content) or "MCP tool reported an error"
            raise RuntimeError(msg)
        return _content_to_value(result.content)

    async def list_tools(self, cfg: MCPServerConfig) -> list[Any]:
        """Return the raw Tool objects advertised by the remote server."""
        key = await self.ensure_server(cfg)
        result = await self._sessions[key].list_tools()
        return list(result.tools)


def _content_to_text(content: list[Any]) -> str:
    parts: list[str] = []
    for item in content or []:
        if getattr(item, "type", None) == "text":
            parts.append(item.text)
        else:
            parts.append(str(item))
    return "\n".join(parts)


def _content_to_value(content: list[Any]) -> Any:
    """Return structured content when the server emitted JSON, else text.

    MCP servers typically emit one TextContent per item in a list return,
    so a list of three rows arrives as three separate text chunks. We try
    to parse each chunk as JSON; if all chunks parse, we return a list;
    if only one chunk is present, we return it unwrapped; if any chunk
    fails, we fall back to the concatenated text.
    """
    items = list(content or [])
    if not items:
        return None

    parsed: list[Any] = []
    for item in items:
        if getattr(item, "type", None) != "text":
            return _content_to_text(items)
        try:
            parsed.append(json.loads(item.text))
        except (json.JSONDecodeError, ValueError):
            return _content_to_text(items)

    return parsed[0] if len(parsed) == 1 else parsed
