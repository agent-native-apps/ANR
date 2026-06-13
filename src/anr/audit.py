"""JSONL audit trail writer (§3.2.2 and §5.3.3).

Every mesh interception produces exactly one audit record. The record is
serialised to a JSON line and appended to output/audit.jsonl. Records are
structured enough that a later analysis tool can reconstruct the full
delegation tree and enforcement history of a run.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


class AuditLog:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Each run starts with a clean log. Without this, repeated
        # `anr run` invocations append to the same file and the live
        # visualizer shows stale events from previous runs alongside
        # new ones. The viz's AuditTailer detects file truncation
        # (size < last offset) and resets its in-memory state, so a
        # fresh-write here flips it cleanly on the next poll.
        self.path.write_text("", encoding="utf-8")
        self._run_id = f"{int(time.time() * 1000):x}"

    def write(self, **fields: Any) -> dict[str, Any]:
        record = {
            "ts": time.time(),
            "run_id": self._run_id,
            **fields,
        }
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")
        return record
