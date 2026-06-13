#!/usr/bin/env python3
"""Run a spec N times unattended and collect one audit log per run.

Each run gets its own output directory so audit logs never interleave:

    output/campaign/<scenario>/run-001/audit.jsonl
    output/campaign/<scenario>/run-001/run.log        (stdout+stderr)
    output/campaign/<scenario>/manifest.jsonl         (one line per run)

HITL checkpoints are decided by the scripted ``ANR_HITL=auto`` backend
(approve-all by default) so runs never block on a human; the audit log
records each decision as scripted.

Usage:
    uv run python scripts/campaign.py specs/research_assistant.yaml -n 10
    uv run python scripts/campaign.py specs/emergency_response.yaml -n 50 --jobs 3
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def run_once(
    spec: Path, task: str | None, run_dir: Path, timeout_sec: float,
    expose_all: bool = False,
) -> dict:
    run_dir.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-m", "anr.cli", "run", str(spec), "--output-dir", str(run_dir)]
    if task:
        cmd.append(task)
    env = os.environ.copy()
    env["ANR_HITL"] = "auto"
    if expose_all:
        env["ANR_EXPOSE"] = "all"
    started = time.time()
    log_path = run_dir / "run.log"
    with log_path.open("w", encoding="utf-8") as log:
        try:
            proc = subprocess.run(
                cmd,
                stdout=log,
                stderr=subprocess.STDOUT,
                env=env,
                cwd=REPO_ROOT,
                timeout=timeout_sec,
            )
            exit_code = proc.returncode
            timed_out = False
        except subprocess.TimeoutExpired:
            exit_code = -1
            timed_out = True
    elapsed = time.time() - started

    audit = run_dir / "audit.jsonl"
    n_events = 0
    cost_usd = 0.0
    if audit.is_file():
        with audit.open(encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                n_events += 1
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                totals = rec.get("totals") or {}
                cost_usd = max(cost_usd, totals.get("cost_usd") or 0.0)
    return {
        "run_dir": str(run_dir.relative_to(REPO_ROOT)),
        "exit_code": exit_code,
        "timed_out": timed_out,
        "elapsed_sec": round(elapsed, 1),
        "audit_events": n_events,
        "cost_usd": round(cost_usd, 4),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("spec", type=Path)
    ap.add_argument("task", nargs="?", default=None,
                    help="Task string (default: the spec's example_task)")
    ap.add_argument("-n", "--runs", type=int, default=10)
    ap.add_argument("--jobs", type=int, default=1,
                    help="Concurrent runs (each is an isolated process+dir)")
    ap.add_argument("--out", type=Path, default=None,
                    help="Campaign root (default: output/campaign/<spec-stem>)")
    ap.add_argument("--timeout", type=float, default=600.0,
                    help="Per-run wall-clock timeout in seconds")
    ap.add_argument("--start", type=int, default=1,
                    help="First run index (to extend an existing campaign)")
    ap.add_argument("--expose-all", action="store_true",
                    help="Fault injection: expose all declared tools to every "
                         "agent (ANR_EXPOSE=all); mesh permissions unchanged")
    args = ap.parse_args()

    spec = args.spec.resolve()
    if not spec.is_file():
        print(f"spec not found: {spec}", file=sys.stderr)
        return 2
    default_name = spec.stem + ("_expose_all" if args.expose_all else "")
    root = (args.out or (REPO_ROOT / "output" / "campaign" / default_name)).resolve()
    root.mkdir(parents=True, exist_ok=True)
    manifest = root / "manifest.jsonl"

    indices = range(args.start, args.start + args.runs)
    print(f"[campaign] {spec.name}: {args.runs} runs -> {root} (jobs={args.jobs})")
    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futures = {
            pool.submit(
                run_once, spec, args.task, root / f"run-{i:03d}", args.timeout,
                args.expose_all,
            ): i
            for i in indices
        }
        with manifest.open("a", encoding="utf-8") as mf:
            for fut in as_completed(futures):
                i = futures[fut]
                try:
                    res = fut.result()
                except Exception as e:  # noqa: BLE001
                    res = {"run_dir": f"run-{i:03d}", "exit_code": -2, "error": str(e)}
                res["index"] = i
                res["spec"] = str(spec.relative_to(REPO_ROOT)) if spec.is_relative_to(REPO_ROOT) else str(spec)
                res["expose_all"] = args.expose_all
                results.append(res)
                mf.write(json.dumps(res) + "\n")
                mf.flush()
                status = "ok" if res.get("exit_code") == 0 else f"exit={res.get('exit_code')}"
                print(
                    f"[campaign] run-{i:03d}: {status} "
                    f"{res.get('elapsed_sec', '?')}s "
                    f"{res.get('audit_events', '?')} events "
                    f"${res.get('cost_usd', 0):.4f}",
                    flush=True,
                )

    ok = sum(1 for r in results if r.get("exit_code") == 0)
    total_cost = sum(r.get("cost_usd") or 0 for r in results)
    print(f"[campaign] done: {ok}/{len(results)} ok, total ${total_cost:.2f}")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
