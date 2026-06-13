#!/usr/bin/env python3
"""Test-only HITL approver. Polls the viz, approves pending requests
via the same /control/hitl/decide endpoint the browser UI uses, and
emits one line per decision. Tracks already-handled ids so we don't
double-approve.

Usage:  python scripts/auto_approve.py [--viz http://localhost:8080]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request


def post_decide(viz: str, req_id: str, action: str = "approve", note: str = "") -> str:
    body = json.dumps({"action": action, "note": note}).encode()
    req = urllib.request.Request(
        f"{viz}/control/hitl/decide?id={req_id}",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.read().decode()


def get_pending(viz: str) -> list[dict]:
    with urllib.request.urlopen(f"{viz}/state/hitl", timeout=5) as r:
        return json.loads(r.read()).get("pending", [])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--viz", default="http://localhost:8080")
    ap.add_argument("--poll", type=float, default=0.5)
    args = ap.parse_args()

    handled: set[str] = set()
    print(f"[approver] watching {args.viz}/state/hitl (poll {args.poll}s)", flush=True)
    while True:
        try:
            pending = get_pending(args.viz)
        except Exception as e:
            print(f"[approver] poll error: {e}", flush=True)
            time.sleep(args.poll)
            continue
        for p in pending:
            pid = p.get("id", "")
            if not pid or pid in handled:
                continue
            try:
                resp = post_decide(args.viz, pid, "approve", "test-driver")
                print(
                    f"[approver] APPROVE id={pid} cp={p.get('checkpoint_id')} "
                    f"tool={p.get('tool')} caller={p.get('caller')} -> {resp}",
                    flush=True,
                )
                handled.add(pid)
            except Exception as e:
                print(f"[approver] post error for {pid}: {e}", flush=True)
        time.sleep(args.poll)


if __name__ == "__main__":
    sys.exit(main() or 0)
