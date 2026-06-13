"""CLI: `python -m anr_viz <spec.yaml> [<audit.jsonl>]`."""

from __future__ import annotations

import argparse
import sys
import threading
import time
import webbrowser
from pathlib import Path

import uvicorn

from anr.loader import load_spec


def _parse(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="anr-viz",
        description="Live visualizer for an agent-native runtime session.",
    )
    p.add_argument("spec_path", help="Path to the YAML application specification.")
    p.add_argument(
        "audit_path",
        nargs="?",
        default="./output/audit.jsonl",
        help="Path to the mesh audit JSONL (default: ./output/audit.jsonl).",
    )
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8080)
    p.add_argument(
        "--no-browser",
        action="store_true",
        help="Don't open the visualizer in a browser tab on startup.",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse(argv if argv is not None else sys.argv[1:])

    spec_path = Path(args.spec_path).resolve()
    audit_path = Path(args.audit_path).resolve()
    if not spec_path.is_file():
        print(f"spec not found: {spec_path}", file=sys.stderr)
        return 2
    # audit_path is allowed to not exist yet — it will appear once a run starts.
    audit_path.parent.mkdir(parents=True, exist_ok=True)

    # Validate the spec eagerly so bad YAML is surfaced before the server starts.
    spec = load_spec(spec_path)

    # The FastAPI app is constructed with these two paths as ambient state.
    # We set them on the module so the factory can read them.
    from . import app as app_module
    app_module.configure(spec=spec, spec_path=spec_path, audit_path=audit_path)

    url = f"http://{args.host}:{args.port}"
    print(
        f"anr-viz: serving {spec.application.name} v{spec.application.version} "
        f"at {url}",
        file=sys.stderr,
    )
    print(f"  spec:  {spec_path}", file=sys.stderr)
    print(f"  audit: {audit_path}", file=sys.stderr)

    # Open the browser shortly after uvicorn binds. Run in a daemon
    # thread so it doesn't block the event loop. Suppressed by
    # --no-browser, or when the user is on a host where webbrowser
    # can't find anything to launch.
    if not args.no_browser:
        def _open_browser() -> None:
            time.sleep(0.7)
            try:
                webbrowser.open(url, new=2)
            except Exception:  # noqa: BLE001
                pass

        threading.Thread(target=_open_browser, daemon=True).start()

    uvicorn.run(
        "anr_viz.app:app",
        host=args.host,
        port=args.port,
        log_level="warning",
        reload=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
