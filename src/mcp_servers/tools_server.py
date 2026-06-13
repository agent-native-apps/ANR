"""A minimal MCP server for the research-assistant demo.

Exposes four tools over MCP stdio:

  * list_docs()             — list available documents in the corpus
  * grep_files(query, ...)  — keyword search across docs/ returning matched
                              line + surrounding context (the way a real
                              researcher locates relevant sections without
                              re-reading every file)
  * read_file(path)         — read a file under docs/
  * fetch_url(url)          — HTTP GET with a short timeout
  * write_note(filename, content) — write markdown into ANR_OUTPUT_DIR

The server enforces path sandboxing on filesystem operations (ANR_DOCS_DIR
for reads, ANR_OUTPUT_DIR for writes). The mesh layered above this server
adds policy-as-code enforcement: allow-listed domains for fetch, HITL
checkpoints on writes, per-agent tool whitelists, and so on. This MCP
server's job is narrow — implement the tool faithfully and refuse calls
that escape the sandbox.

Run with:  python -m mcp_servers.tools_server
"""

from __future__ import annotations

import logging
import os
import urllib.request
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

# Quiet the default per-request chatter; the mesh's audit log is the
# authoritative record for this POC.
logging.getLogger("mcp.server").setLevel(logging.WARNING)
logging.getLogger("mcp.server.lowlevel.server").setLevel(logging.WARNING)

DOCS_DIR = Path(os.environ.get("ANR_DOCS_DIR", "./data/knowledge_base")).resolve()
OUTPUT_DIR = Path(os.environ.get("ANR_OUTPUT_DIR", "./output/notes")).resolve()
FETCH_TIMEOUT = float(os.environ.get("ANR_FETCH_TIMEOUT", "10"))
FETCH_MAX_BYTES = int(os.environ.get("ANR_FETCH_MAX_BYTES", str(256 * 1024)))

app = FastMCP("anr-tools")


def _resolve_under(root: Path, candidate: str) -> Path:
    """Resolve `candidate` under `root` or raise if it escapes."""
    root = root.resolve()
    target = (root / candidate).resolve() if not Path(candidate).is_absolute() else Path(candidate).resolve()
    try:
        target.relative_to(root)
    except ValueError as e:
        raise ValueError(f"path {candidate!r} escapes sandbox {root}") from e
    return target


_GREP_MAX_MATCHES = int(os.environ.get("ANR_GREP_MAX_MATCHES", "40"))


@app.tool()
def list_docs() -> list[dict[str, Any]]:
    """Enumerate documents in the docs corpus (path + size).

    Useful as a one-shot bootstrap so the agent knows what's available
    without needing to grep blindly. Returns every regular file under
    DOCS_DIR sorted by path.
    """
    results: list[dict[str, Any]] = []
    if not DOCS_DIR.exists():
        return results
    for p in sorted(DOCS_DIR.rglob("*")):
        if not p.is_file():
            continue
        results.append(
            {
                "path": p.relative_to(DOCS_DIR).as_posix(),
                "size_bytes": p.stat().st_size,
            }
        )
    return results


@app.tool()
def grep_files(
    query: str,
    context_lines: int = 1,
    case_sensitive: bool = False,
    max_matches: int = 20,
) -> dict[str, Any]:
    """Keyword search across docs/.

    Searches every file under DOCS_DIR for `query` (a literal substring,
    not a regex — punctuation and spaces are matched as-typed) and
    returns the matched line plus `context_lines` of surrounding
    context. Up to `max_matches` hits are returned; the response also
    reports whether the result set was truncated.

    Args:
        query: literal substring to match.
        context_lines: lines of context above/below each match (0–5).
        case_sensitive: default False — matches are case-insensitive.
        max_matches: cap on total matches returned (1–`_GREP_MAX_MATCHES`).

    Returns a dict::

        {
          "query": "...",
          "matches": [
            {"path": "...", "line": 17, "text": "...", "context": ["..."]},
            ...
          ],
          "truncated": false,
          "files_searched": 16,
        }
    """
    if not query.strip():
        raise ValueError("query must not be empty")
    context_lines = max(0, min(5, int(context_lines)))
    cap = max(1, min(_GREP_MAX_MATCHES, int(max_matches)))

    needle = query if case_sensitive else query.lower()
    matches: list[dict[str, Any]] = []
    files_searched = 0
    truncated = False

    if not DOCS_DIR.exists():
        return {"query": query, "matches": [], "truncated": False, "files_searched": 0}

    for p in sorted(DOCS_DIR.rglob("*")):
        if not p.is_file():
            continue
        files_searched += 1
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        lines = text.splitlines()
        rel = p.relative_to(DOCS_DIR).as_posix()
        for i, line in enumerate(lines):
            hay = line if case_sensitive else line.lower()
            if needle in hay:
                lo = max(0, i - context_lines)
                hi = min(len(lines), i + context_lines + 1)
                context = [
                    f"{n + 1:4d}: {lines[n]}"
                    for n in range(lo, hi)
                ]
                matches.append(
                    {
                        "path": rel,
                        "line": i + 1,
                        "text": line,
                        "context": context,
                    }
                )
                if len(matches) >= cap:
                    truncated = True
                    break
        if truncated:
            break

    return {
        "query": query,
        "matches": matches,
        "truncated": truncated,
        "files_searched": files_searched,
    }


@app.tool()
def read_file(path: str) -> str:
    """Read a file under the docs sandbox as UTF-8 text."""
    target = _resolve_under(DOCS_DIR, path)
    if not target.is_file():
        raise FileNotFoundError(path)
    return target.read_text(encoding="utf-8", errors="replace")


@app.tool()
def fetch_url(url: str) -> str:
    """Fetch `url` and return the response body (truncated to FETCH_MAX_BYTES)."""
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "anr-research-assistant/0.1"},
    )
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
        raw = resp.read(FETCH_MAX_BYTES + 1)
    truncated = len(raw) > FETCH_MAX_BYTES
    body = raw[:FETCH_MAX_BYTES].decode("utf-8", errors="replace")
    if truncated:
        body += f"\n\n[truncated at {FETCH_MAX_BYTES} bytes]"
    return body


@app.tool()
def write_note(filename: str, content: str) -> dict[str, Any]:
    """Write `content` as UTF-8 to OUTPUT_DIR/filename."""
    target = _resolve_under(OUTPUT_DIR, filename)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return {"path": str(target), "bytes": len(content.encode("utf-8"))}


def main() -> None:
    app.run()


if __name__ == "__main__":
    main()
