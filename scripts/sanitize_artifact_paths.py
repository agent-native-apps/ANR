#!/usr/bin/env python3
"""Remove local absolute repository paths from publishable text artifacts."""
from __future__ import annotations

import argparse
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
REPO_NAME = REPO_ROOT.name

TEXT_SUFFIXES = {
    ".csv",
    ".json",
    ".jsonl",
    ".log",
    ".md",
    ".tex",
    ".txt",
    ".yaml",
    ".yml",
}


def sanitize_text(text: str) -> str:
    """Replace absolute paths to this repo with repo-relative paths."""
    current_root = str(REPO_ROOT)
    text = text.replace(current_root + "/", "")
    text = text.replace(current_root, ".")

    # Handles the same repo checked out elsewhere, e.g.
    # /home/user/work/agent-native-runtime/output/audit.jsonl -> output/audit.jsonl.
    repo_prefix_with_slash = re.compile(
        rf"/(?:[^/\s\"']+/)*{re.escape(REPO_NAME)}/"
    )
    repo_root_only = re.compile(
        rf"/(?:[^/\s\"']+/)*{re.escape(REPO_NAME)}(?=[\s\"']|$)"
    )
    text = repo_prefix_with_slash.sub("", text)
    return repo_root_only.sub(".", text)


def sanitize_file(path: Path, *, check: bool) -> bool:
    raw = path.read_text(encoding="utf-8")
    clean = sanitize_text(raw)
    changed = clean != raw
    if changed and not check:
        path.write_text(clean, encoding="utf-8")
    return changed


def iter_text_files(root: Path) -> list[Path]:
    if root.is_file():
        return [root] if root.suffix in TEXT_SUFFIXES else []
    return sorted(
        p for p in root.rglob("*")
        if p.is_file() and p.suffix in TEXT_SUFFIXES
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        default=[REPO_ROOT / "artifacts" / "campaign"],
        help="Files or directories to sanitize.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit non-zero if any file would change, but do not write.",
    )
    args = parser.parse_args()

    changed: list[Path] = []
    for root in args.paths:
        root = root if root.is_absolute() else REPO_ROOT / root
        for path in iter_text_files(root):
            if sanitize_file(path, check=args.check):
                changed.append(path)

    for path in changed:
        print(path.relative_to(REPO_ROOT))

    if args.check and changed:
        print(f"{len(changed)} file(s) contain local absolute repo paths.")
        return 1
    print(f"{len(changed)} file(s) sanitized.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
