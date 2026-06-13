"""Native agent runtime — a minimal LiteLLM-backed tool-use loop.

The agent never touches tools directly: every tool call goes through the
mesh. That is the paper's §3.2.2 argument made concrete. The only thing
the agent knows how to do locally is talk to its LLM provider.

We deliberately avoid depending on any agent framework. The loop is short
and explicit so the reader can see that the interesting infrastructure —
policy enforcement, HITL, delegation scoping, audit — is in the mesh, not
smeared across a framework.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import litellm
from rich.console import Console

from .mesh import InvocationContext, Mesh
from .spec import Spec

console = Console(stderr=True)

# Silence LiteLLM's own chatter — we do our own audit.
litellm.suppress_debug_info = True


class NativeAgent:
    """An agent implementation that runs one task to completion.

    `tool_schemas` is the subset of the run's tool schemas this agent is
    permitted to see (its `tools` list in the spec). Each schema is in the
    OpenAI tool-use format that LiteLLM accepts.
    """

    def __init__(
        self,
        *,
        spec: Spec,
        declaration: Any,  # spec.Agent or orchestrator._TemplateDecl (duck-typed: .name, .model)
        mesh: Mesh,
        tool_schemas: list[dict[str, Any]],
        system_prompt: str,
        max_turns: int = 20,
    ) -> None:
        self.spec = spec
        self.decl = declaration
        self.mesh = mesh
        self.tool_schemas = tool_schemas  # base set, mutated per turn by ctx grants
        self.system_prompt = system_prompt
        self.max_turns = max_turns

    async def run(self, task: str, ctx: InvocationContext) -> str:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": task},
        ]

        for turn in range(self.max_turns):
            # Effective tool list = base + any tool grants attached to ctx
            # since the previous turn (R2 capability acquisition).
            effective_tools = self._effective_tool_schemas(ctx)
            console.log(
                f"[cyan]{self.decl.name}[/cyan] turn {turn + 1}: calling LLM "
                f"({len(messages)} messages, {len(effective_tools)} tools)"
            )
            response = await litellm.acompletion(
                model=self.decl.model,
                messages=messages,
                tools=effective_tools or None,
                tool_choice="auto" if effective_tools else None,
            )
            cost = _cost_of(response)
            self.mesh.record_llm_usage(cost_usd=cost)

            msg = response.choices[0].message
            messages.append(_assistant_message_to_dict(msg))

            tool_calls = getattr(msg, "tool_calls", None) or []
            requested_tools = [call.function.name for call in tool_calls]
            self.mesh.record_agent_turn(
                ctx=ctx,
                turn=turn + 1,
                model=self.decl.model,
                exposed_tools=[
                    t["function"]["name"]
                    for t in effective_tools
                    if "function" in t and "name" in t["function"]
                ],
                requested_tools=requested_tools,
                response_preview=_preview_text(msg.content or ""),
                cost_usd=cost,
            )
            if not tool_calls:
                return msg.content or ""

            for call in tool_calls:
                name = call.function.name
                try:
                    args = json.loads(call.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {"_raw_arguments": call.function.arguments}
                console.log(
                    f"  [dim]{self.decl.name} -> mesh.invoke({name}, "
                    f"{json.dumps(args, default=str)[:160]})[/dim]"
                )
                result = await self.mesh.invoke(tool=name, arguments=args, ctx=ctx)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": json.dumps(result, default=str),
                    }
                )

        return (
            f"[{self.decl.name} terminated after {self.max_turns} turns "
            f"without producing a final response]"
        )

    def _effective_tool_schemas(
        self, ctx: InvocationContext
    ) -> list[dict[str, Any]]:
        """Base schemas plus the schemas of any tools granted to this ctx."""
        if not ctx.granted_capabilities:
            return self.tool_schemas
        seen = {s["function"]["name"] for s in self.tool_schemas}
        extras: list[dict[str, Any]] = []
        for g in ctx.granted_capabilities:
            if g.kind != "tool" or g.name in seen or g.tool_schema is None:
                continue
            extras.append(g.tool_schema)
            seen.add(g.name)
        return self.tool_schemas + extras


def load_system_prompt(path: str | Path) -> str:
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"system_prompt_file not found: {p}")
    return p.read_text(encoding="utf-8")


def _cost_of(response: Any) -> float:
    try:
        return float(getattr(response, "_hidden_params", {}).get("response_cost") or 0.0)
    except Exception:
        return 0.0


def _preview_text(text: str, limit: int = 1200) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "...<truncated>"


def _assistant_message_to_dict(msg: Any) -> dict[str, Any]:
    """Re-serialise an assistant message (possibly with tool_calls) for the next turn."""
    out: dict[str, Any] = {"role": "assistant", "content": msg.content or ""}
    tc = getattr(msg, "tool_calls", None)
    if tc:
        out["tool_calls"] = [
            {
                "id": c.id,
                "type": "function",
                "function": {
                    "name": c.function.name,
                    "arguments": c.function.arguments,
                },
            }
            for c in tc
        ]
    return out
