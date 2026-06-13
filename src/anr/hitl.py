"""Human-in-the-loop checkpoint mechanics.

The mesh hands a checkpoint plus context to a Prompter and awaits a
Decision. The caller (the mesh) is responsible for acting on the
decision: approve → continue; reject → return an error to the agent so
it sees the refusal; modify → replace the tool args with the human's
edit.

Three prompter backends ship:

  * ``StdinPrompter`` — blocks the calling task on stdin via
    ``asyncio.to_thread`` so the rest of the event loop keeps running.
    Default; used when the operator is driving the run from the
    terminal.
  * ``UIPrompter`` — writes a request JSON into a shared directory and
    polls for a response JSON written by the visualizer. This is how
    the live `anr-viz` UI surfaces HITL prompts to the operator and
    lets them approve / reject / modify from the browser.
  * ``AutoPrompter`` — scripted fixed decision (approve by default)
    for unattended measurement campaigns; the audit record notes the
    reviewer was scripted.

Pick the backend with ``ANR_HITL=stdin|ui|auto`` (default ``stdin``).
When ``ui`` is selected the runtime expects the visualizer to be
running and pointed at the same audit directory.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from rich.console import Console
from rich.panel import Panel

console = Console(stderr=True)


@dataclass
class Decision:
    action: str  # "approve" | "reject" | "modify"
    modified_args: dict[str, Any] | None = None
    note: str = ""

    @property
    def approved(self) -> bool:
        return self.action in {"approve", "modify"}


class Prompter(Protocol):
    async def prompt(
        self,
        *,
        checkpoint_id: str,
        pattern: str,
        prompt_text: str,
        caller: str,
        tool: str,
        args: dict[str, Any],
        extra: str = "",
    ) -> Decision: ...


# ---------------------------------------------------------------------------
# Stdin
# ---------------------------------------------------------------------------


class StdinPrompter:
    """Block the awaiting task on stdin, off the event loop."""

    async def prompt(
        self,
        *,
        checkpoint_id: str,
        pattern: str,
        prompt_text: str,
        caller: str,
        tool: str,
        args: dict[str, Any],
        extra: str = "",
    ) -> Decision:
        return await asyncio.to_thread(
            _stdin_prompt,
            checkpoint_id,
            pattern,
            prompt_text,
            caller,
            tool,
            args,
            extra,
        )


def _stdin_prompt(
    checkpoint_id: str,
    pattern: str,
    prompt_text: str,
    caller: str,
    tool: str,
    args: dict[str, Any],
    extra: str,
) -> Decision:
    body = [
        f"[bold]checkpoint:[/bold] {checkpoint_id}   [dim]({pattern})[/dim]",
        f"[bold]caller:[/bold]     {caller}",
        f"[bold]tool:[/bold]       {tool}",
        f"[bold]args:[/bold]       {json.dumps(args, indent=2, default=str)}",
    ]
    if extra:
        body.append(f"[bold]context:[/bold]   {extra}")
    body.append("")
    body.append(prompt_text)
    console.print(Panel("\n".join(body), title="HUMAN-IN-THE-LOOP", border_style="yellow"))

    while True:
        console.print(
            "[yellow]Decision?[/yellow] [a]pprove / [r]eject / [m]odify : ", end=""
        )
        try:
            raw = sys.stdin.readline().strip().lower()
        except KeyboardInterrupt:
            return Decision(action="reject", note="keyboard interrupt")
        if raw in {"a", "approve", ""}:
            return Decision(action="approve")
        if raw in {"r", "reject"}:
            console.print("reason (optional): ", end="")
            reason = sys.stdin.readline().strip()
            return Decision(action="reject", note=reason)
        if raw in {"m", "modify"}:
            console.print(
                "new args as JSON (leave blank to cancel): ", end=""
            )
            raw_args = sys.stdin.readline().strip()
            if not raw_args:
                continue
            try:
                new_args = json.loads(raw_args)
            except json.JSONDecodeError as e:
                console.print(f"[red]invalid JSON:[/red] {e}")
                continue
            if not isinstance(new_args, dict):
                console.print("[red]modified args must be a JSON object[/red]")
                continue
            return Decision(action="modify", modified_args=new_args)
        console.print("[red]unrecognised choice; please answer a / r / m[/red]")


# ---------------------------------------------------------------------------
# UI (file-based bridge to anr-viz)
# ---------------------------------------------------------------------------
#
# Wire format under ``hitl_dir``:
#
#   req-<id>.json   written by the runtime, removed after a decision arrives
#   res-<id>.json   written by the visualizer, removed after the runtime
#                   reads it
#
# Both sides treat the dir as the source of truth — no in-memory queue,
# no sockets. The runtime polls for a matching ``res`` file at
# ``poll_interval`` cadence; the viz polls the dir for the live list of
# ``req`` files. Multiple in-flight prompts (e.g. several R1-spawned
# agents asking concurrently) are naturally supported because every
# request has its own id.


class UIPrompter:
    """File-based prompter the visualizer drives via HTTP.

    A non-zero ``timeout_sec`` auto-rejects a pending request if the
    operator doesn't decide in time — useful for demos and unattended
    runs where you don't want a forgotten browser tab to wedge an
    agent forever. ``None`` (or ``0``) waits indefinitely.
    """

    def __init__(
        self,
        hitl_dir: Path,
        poll_interval: float = 0.25,
        timeout_sec: float | None = None,
    ) -> None:
        self.hitl_dir = hitl_dir
        self.poll_interval = poll_interval
        self.timeout_sec = timeout_sec if (timeout_sec or 0) > 0 else None
        self.hitl_dir.mkdir(parents=True, exist_ok=True)

    async def prompt(
        self,
        *,
        checkpoint_id: str,
        pattern: str,
        prompt_text: str,
        caller: str,
        tool: str,
        args: dict[str, Any],
        extra: str = "",
    ) -> Decision:
        req_id = uuid.uuid4().hex[:12]
        req_path = self.hitl_dir / f"req-{req_id}.json"
        res_path = self.hitl_dir / f"res-{req_id}.json"
        payload = {
            "id": req_id,
            "checkpoint_id": checkpoint_id,
            "pattern": pattern,
            "prompt_text": prompt_text,
            "caller": caller,
            "tool": tool,
            "args": args,
            "extra": extra,
            "ts": time.time(),
        }
        # Atomic write (tmp + rename) so the viz never reads a half file.
        tmp = req_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        os.replace(tmp, req_path)

        timeout_hint = (
            f", timeout {self.timeout_sec:.0f}s" if self.timeout_sec else ""
        )
        console.print(
            f"[yellow]hitl:[/yellow] [dim]waiting on UI decision[/dim] "
            f"{caller} → {tool}  [dim]({checkpoint_id}{timeout_hint})[/dim]"
        )

        deadline = (
            time.monotonic() + self.timeout_sec if self.timeout_sec else None
        )
        try:
            while True:
                if res_path.is_file():
                    try:
                        data = json.loads(res_path.read_text(encoding="utf-8"))
                    except json.JSONDecodeError:
                        # Reader raced the writer; try again on the next tick.
                        await asyncio.sleep(self.poll_interval)
                        continue
                    return _parse_decision(data)
                if deadline is not None and time.monotonic() >= deadline:
                    console.print(
                        f"[red]hitl:[/red] no UI decision within "
                        f"{self.timeout_sec:.0f}s — auto-rejecting "
                        f"{checkpoint_id}"
                    )
                    return Decision(
                        action="reject",
                        note=f"auto-rejected after {self.timeout_sec:.0f}s timeout",
                    )
                await asyncio.sleep(self.poll_interval)
        finally:
            for p in (req_path, res_path):
                try:
                    p.unlink()
                except FileNotFoundError:
                    pass


def _parse_decision(data: dict[str, Any]) -> Decision:
    action = str(data.get("action", "")).lower()
    if action not in {"approve", "reject", "modify"}:
        action = "reject"
    note = str(data.get("note", "") or "")
    modified_args = data.get("modified_args")
    if action == "modify" and not isinstance(modified_args, dict):
        # A "modify" without valid args is meaningless — treat as reject.
        return Decision(
            action="reject",
            note=note or "modify decision without valid modified_args",
        )
    return Decision(action=action, modified_args=modified_args if action == "modify" else None, note=note)


# ---------------------------------------------------------------------------
# Auto (scripted, for unattended campaign runs)
# ---------------------------------------------------------------------------


class AutoPrompter:
    """Scripted decision policy for unattended runs (``ANR_HITL=auto``).

    Every checkpoint resolves immediately to a fixed action — ``approve``
    by default, or ``reject`` via ``ANR_HITL_AUTO_ACTION=reject``. The
    decision note records that the reviewer was scripted, so audit logs
    from measurement campaigns are self-describing rather than passing
    as human review.
    """

    def __init__(self, action: str = "approve") -> None:
        if action not in {"approve", "reject"}:
            raise ValueError(f"unsupported auto action: {action!r}")
        self.action = action

    async def prompt(
        self,
        *,
        checkpoint_id: str,
        pattern: str,
        prompt_text: str,
        caller: str,
        tool: str,
        args: dict[str, Any],
        extra: str = "",
    ) -> Decision:
        console.print(
            f"[yellow]hitl:[/yellow] [dim]auto-{self.action}[/dim] "
            f"{caller} → {tool}  [dim]({checkpoint_id})[/dim]"
        )
        return Decision(
            action=self.action,
            note=f"scripted auto-{self.action} (ANR_HITL=auto)",
        )


# ---------------------------------------------------------------------------
# Backend selection
# ---------------------------------------------------------------------------


def make_prompter(*, hitl_dir: Path | None = None) -> Prompter:
    """Pick a prompter from the ``ANR_HITL`` env var.

    ``stdin`` (default) → terminal prompt; ``ui`` → file-based bridge to
    anr-viz, with the request/response files under ``hitl_dir``. The UI
    backend additionally honours ``ANR_HITL_TIMEOUT_SEC``: any positive
    integer auto-rejects a pending request after that many seconds.
    ``auto`` → scripted decisions for unattended campaign runs, with the
    fixed action picked by ``ANR_HITL_AUTO_ACTION`` (default ``approve``).
    """
    backend = os.environ.get("ANR_HITL", "stdin").strip().lower()
    if backend == "auto":
        action = os.environ.get("ANR_HITL_AUTO_ACTION", "approve").strip().lower()
        return AutoPrompter(action=action)
    if backend == "ui":
        if hitl_dir is None:
            raise RuntimeError("ANR_HITL=ui requires a hitl_dir")
        timeout: float | None = None
        raw = os.environ.get("ANR_HITL_TIMEOUT_SEC", "").strip()
        if raw:
            try:
                timeout = float(raw)
            except ValueError:
                console.print(
                    f"[red]ANR_HITL_TIMEOUT_SEC={raw!r} is not a number; "
                    f"ignoring[/red]"
                )
        return UIPrompter(hitl_dir, timeout_sec=timeout)
    return StdinPrompter()
