"""Command-line entry point for the runtime."""

from __future__ import annotations

import argparse
import asyncio
import os
import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

from dotenv import load_dotenv
from rich.console import Console

from .compiler import Compiler
from .loader import load_spec

console = Console(stderr=True)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="anr", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="Run one task against a compiled spec")
    run.add_argument("spec_path", help="Path to a YAML application specification")
    run.add_argument(
        "task",
        nargs="?",
        default=None,
        help=(
            "Task to dispatch to the entry-point agent. If omitted, the "
            "spec's application.example_task is used (run `anr list` to see it)."
        ),
    )
    run.add_argument(
        "--output-dir",
        default="./output",
        help="Where to write the audit log and generated notes",
    )
    run.add_argument(
        "--viz",
        action="store_true",
        help=(
            "Spawn the anr-viz server alongside the run, open a browser tab, "
            "and shut it down when the run finishes."
        ),
    )
    run.add_argument(
        "--viz-port",
        type=int,
        default=8080,
        help="Port for --viz (default: 8080).",
    )

    lst = sub.add_parser("list", help="List available application specs")
    lst.add_argument(
        "specs_dir",
        nargs="?",
        default="specs",
        help="Directory to scan for *.yaml specs (default: ./specs).",
    )

    return parser.parse_args(argv)


def _port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        try:
            sock.connect((host, port))
            return True
        except OSError:
            return False


def _spawn_viz(spec_path: Path, audit_path: Path, port: int) -> subprocess.Popen | None:
    """Launch anr-viz in a child process, opening a browser tab.

    Returns the Popen handle so the caller can terminate it on exit;
    returns None if the port is already in use (the user probably has
    a viz running already).
    """
    if _port_in_use(port):
        console.print(
            f"[yellow]port {port} in use — assuming an anr-viz is already running. "
            f"Open http://127.0.0.1:{port} in a browser to follow this run.[/yellow]"
        )
        return None
    cmd = [
        sys.executable,
        "-m",
        "anr_viz",
        str(spec_path),
        str(audit_path),
        "--port",
        str(port),
        "--no-browser",  # we open the browser ourselves once we know it's up
    ]
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=os.environ.copy(),
    )
    # Wait for the server to start accepting connections.
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if _port_in_use(port):
            break
        if proc.poll() is not None:
            console.print("[red]anr-viz failed to start[/red]")
            return None
        time.sleep(0.1)
    url = f"http://127.0.0.1:{port}"
    console.print(f"[bold]viz:[/bold] {url}")
    try:
        webbrowser.open(url, new=2)
    except Exception:  # noqa: BLE001
        pass
    return proc


# LiteLLM model strings here look like "anthropic/claude-haiku-4-5". The text
# before the slash is the provider, which maps to the env var LiteLLM reads.
_PROVIDER_ENV = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "mistral": "MISTRAL_API_KEY",
    "cohere": "COHERE_API_KEY",
    "groq": "GROQ_API_KEY",
}


def _required_env_vars(spec) -> dict[str, list[str]]:
    """Map each required provider env var -> the model strings that need it."""
    models: set[str] = set()
    for agent in spec.agents:
        if agent.model:
            models.add(agent.model)
    for tmpl in spec.agent_templates:
        models.add(tmpl.model)
    needed: dict[str, list[str]] = {}
    for model in sorted(models):
        provider = model.split("/", 1)[0] if "/" in model else (
            "openai" if model.startswith(("gpt", "o1", "o3")) else
            "anthropic" if model.startswith("claude") else ""
        )
        env = _PROVIDER_ENV.get(provider)
        if env:
            needed.setdefault(env, []).append(model)
    return needed


def _preflight_keys(spec) -> int | None:
    """Return an exit code if a required API key is missing, else None."""
    missing = {
        env: models
        for env, models in _required_env_vars(spec).items()
        if not os.environ.get(env)
    }
    if not missing:
        return None
    console.print("[red]Missing API key(s) this spec needs:[/red]")
    for env, models in missing.items():
        console.print(f"  [bold]{env}[/bold]  (used by: {', '.join(models)})")
    if Path(".env").is_file():
        console.print(
            "\nAdd the key(s) to your [bold].env[/bold] file, then re-run."
        )
    else:
        console.print(
            "\nFix: copy the example env file, add your key, then re-run:\n"
            "  [bold]cp .env.example .env[/bold]\n"
            "  # then edit .env and fill in the key shown above"
        )
    console.print(
        "\n[dim]No key needed to explore first — replay a recorded run in the "
        "visualizer:\n"
        "  uv run anr-viz specs/emergency_response.yaml "
        "examples/audits/paper_alignment_audit.jsonl[/dim]"
    )
    return 2


def _cmd_list(specs_dir: str) -> int:
    out = Console()  # stdout — this is the user-facing payload
    root = Path(specs_dir)
    if not root.is_dir():
        console.print(f"[red]not a directory:[/red] {root}")
        return 2
    paths = sorted(root.glob("*.yaml"))
    if not paths:
        console.print(f"[yellow]no *.yaml specs found in[/yellow] {root}")
        return 0
    for path in paths:
        try:
            spec = load_spec(path)
        except Exception as exc:  # noqa: BLE001
            out.print(f"[dim]{path}  (skipped: {type(exc).__name__})[/dim]")
            continue
        meta = spec.application
        out.print(f"[bold cyan]{meta.name}[/bold cyan] [dim]v{meta.version}[/dim]")
        if meta.description:
            out.print(f"  {meta.description}")
        out.print(f"  [dim]run:[/dim] uv run anr run {path}")
        if meta.example_task:
            out.print(f"  [dim]task:[/dim] {meta.example_task}")
        out.print("")
    return 0


async def _run(args: argparse.Namespace) -> int:
    spec_path = Path(args.spec_path).resolve()
    if not spec_path.is_file():
        console.print(f"[red]spec not found:[/red] {spec_path}")
        return 2
    spec = load_spec(spec_path)

    task = args.task if args.task is not None else spec.application.example_task
    if not task:
        console.print(
            "[red]no task given[/red] and this spec has no application.example_task.\n"
            f"Provide one:  uv run anr run {args.spec_path} \"<task>\""
        )
        return 2

    preflight = _preflight_keys(spec)
    if preflight is not None:
        return preflight

    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    viz_proc: subprocess.Popen | None = None
    if args.viz:
        viz_proc = _spawn_viz(spec_path, output_dir / "audit.jsonl", args.viz_port)

    try:
        console.rule(
            f"[bold]compiling {spec.application.name} v{spec.application.version}"
        )
        async with Compiler(
            spec, spec_dir=spec_path.parent, output_dir=output_dir
        ) as graph:
            console.rule("[bold]running task")
            console.print(f"[bold cyan]task:[/bold cyan] {task}")
            result = await graph.run(task)
            console.rule("[bold]final response")
            console.print(result)
            console.rule("[bold]run summary")
            console.print(
                {
                    "totals": {
                        "tool_calls": graph.mesh.totals.tool_calls,
                        "llm_calls": graph.mesh.totals.llm_calls,
                        "cost_usd": round(graph.mesh.totals.cost_usd, 4),
                        "elapsed_sec": round(graph.mesh.totals.elapsed_sec, 2),
                        "sub_agents_spawned": dict(
                            graph.mesh.totals.sub_agents_spawned
                        ),
                    },
                    "audit_log": str(graph.audit_path),
                }
            )
    finally:
        if viz_proc is not None and viz_proc.poll() is None:
            console.print(
                f"[dim]viz still running at http://127.0.0.1:{args.viz_port} "
                f"— press Ctrl+C to stop.[/dim]"
            )
            try:
                viz_proc.wait()
            except KeyboardInterrupt:
                viz_proc.terminate()
                try:
                    viz_proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    viz_proc.kill()
    return 0


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    if args.command == "run":
        return asyncio.run(_run(args))
    if args.command == "list":
        return _cmd_list(args.specs_dir)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
