"""LLM-generated step narrations for the viz, cached on disk.

Each audit event gets one plain-English summary sentence. Summaries
are immutable (events are append-only and never edited), so they're
cached two ways:

1. In-memory dict — fast lookup.
2. JSONL file alongside the audit log — survives viz restarts and
   means we never pay the LLM for the same event twice.

`init(summaries_path)` is called by the viz at startup with a path
derived from the audit log's location; it loads any pre-existing
summaries into memory. `get_summary(event_id, record)` returns the
cached summary if known, otherwise calls the LLM, persists, and
returns the new text.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
from pathlib import Path
from typing import Any

# litellm is already a direct dependency of the runtime — reuse it so
# we don't need a second SDK in the viz process.
import litellm

_DEFAULT_MODEL = os.environ.get("ANR_VIZ_SUMMARY_MODEL", "anthropic/claude-haiku-4-5")
_MAX_TOKENS = 120

_cache: dict[str, str] = {}
_cache_lock = threading.Lock()
_summaries_path: Path | None = None
_inflight: dict[str, asyncio.Task[str]] = {}
_inflight_lock = threading.Lock()


def init(summaries_path: Path) -> None:
    """Configure persistence and warm the in-memory cache from disk."""
    global _summaries_path
    _summaries_path = Path(summaries_path)
    if not _summaries_path.is_file():
        return
    loaded = 0
    try:
        with _summaries_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                eid = rec.get("id")
                summary = rec.get("summary")
                if eid and isinstance(summary, str):
                    with _cache_lock:
                        _cache[eid] = summary
                    loaded += 1
    except OSError:
        return
    if loaded:
        # Stderr-friendly so it shows up in the uvicorn log without
        # competing with HTTP request logs.
        import sys

        print(f"[anr-viz] loaded {loaded} cached summaries", file=sys.stderr)


def has_cached(event_id: str) -> bool:
    with _cache_lock:
        return event_id in _cache


def _persist(event_id: str, summary: str) -> None:
    if _summaries_path is None:
        return
    try:
        _summaries_path.parent.mkdir(parents=True, exist_ok=True)
        with _summaries_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"id": event_id, "summary": summary}) + "\n")
    except OSError:
        # Persistence is best-effort; the in-memory cache still wins.
        pass


async def get_summary(event_id: str, record: dict[str, Any]) -> str:
    """Return a one-sentence narration of `record`, cached by event_id.

    If a generation for the same id is already in flight, await its
    result rather than racing a second LLM call.
    """
    with _cache_lock:
        cached = _cache.get(event_id)
    if cached is not None:
        return cached

    # Coalesce concurrent requests for the same event id.
    with _inflight_lock:
        task = _inflight.get(event_id)
        if task is None:
            loop = asyncio.get_running_loop()
            task = loop.create_task(_generate_and_store(event_id, record))
            _inflight[event_id] = task

    try:
        return await task
    finally:
        with _inflight_lock:
            _inflight.pop(event_id, None)


async def _generate_and_store(event_id: str, record: dict[str, Any]) -> str:
    prompt = _build_prompt(record)
    try:
        text = await asyncio.to_thread(_call_llm, prompt)
    except Exception as e:  # noqa: BLE001
        text = f"(summary unavailable: {type(e).__name__})"
    text = text.strip().strip('"').strip()
    with _cache_lock:
        _cache[event_id] = text
    _persist(event_id, text)
    return text


def _call_llm(prompt: str) -> str:
    response = litellm.completion(
        model=_DEFAULT_MODEL,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=_MAX_TOKENS,
    )
    return response.choices[0].message.content or ""


def _build_prompt(rec: dict[str, Any]) -> str:
    caller = rec.get("caller") or "?"
    instance = rec.get("instance_id") or ""
    tool = rec.get("tool") or "?"
    kind = rec.get("kind") or "tool_call"
    args = rec.get("args") or {}
    outcome = rec.get("outcome") or {}
    ok = outcome.get("ok")
    preview = outcome.get("value_preview") or ""

    args_str = json.dumps(args, default=str)[:500]
    preview_str = preview if isinstance(preview, str) else str(preview)
    if len(preview_str) > 600:
        preview_str = preview_str[:600] + "…"

    status = "succeeded" if ok is True else "refused/failed" if ok is False else "n/a"

    return (
        "You are narrating a multi-agent system run for a developer who "
        "is watching it live in a debugger UI. They can already see the "
        "raw fields below — your job is to turn them into one short, "
        "plain-English sentence (max 25 words, no leading filler like "
        "\"the agent\" if you can avoid it) that says what just happened "
        "and, if relevant, why it matters. Don't restate field names; "
        "don't speculate beyond the data.\n\n"
        f"event kind: {kind}\n"
        f"caller: {caller}{(' (' + instance + ')') if instance and instance != caller else ''}\n"
        f"action / tool: {tool}\n"
        f"args: {args_str}\n"
        f"outcome status: {status}\n"
        f"outcome preview: {preview_str}\n\n"
        "Respond with the sentence only — no preamble, no quotation marks."
    )
