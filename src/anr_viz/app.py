"""FastAPI app: shell page + HTMX-driven state fragment + static assets.

Endpoints:

  GET /                  — shell page (loads cytoscape, polls graph state)
  GET /state/graph.json  — current graph elements + live overlay (JSON)
  GET /state/sidebar     — events ticker + totals (polled by HTMX)
  GET /state/controls    — time-cursor control bar + step indicator
  GET /control/cursor    — mutate the viewer's cursor (op=first|prev|next|last|live)
  GET /static/...        — CSS / JS assets

When the cursor is `None` the view follows the live audit tail. When it is
an integer, we rebuild LiveState from records[:cursor+1] and serve that
frozen snapshot to the /state/* fragments. The audit tailer itself keeps
running either way; the cursor only affects what we render.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from anr.spec import Spec

from . import summary as _summary
from .model import GraphModel, build_model
from .node_info import build_node_info
from .render import (
    render_controls,
    render_event_detail,
    render_graph_state,
    render_sidebar_state,
)
from .state import AuditTailer, LiveState, build_state_at
from .summary import get_summary, has_cached
from .trace import build_trace, to_jsonable

_STATIC = Path(__file__).parent / "static"
_TEMPLATES = Path(__file__).parent / "templates"


class _AppState:
    spec: Spec | None = None
    spec_path: Path | None = None
    audit_path: Path | None = None
    hitl_dir: Path | None = None
    model: GraphModel | None = None
    live: LiveState = LiveState()
    tailer: AuditTailer | None = None
    # None = follow the live tail. Integer = park on records[cursor].
    cursor: int | None = None


_st = _AppState()


def configure(*, spec: Spec, spec_path: Path, audit_path: Path) -> None:
    _st.spec = spec
    _st.spec_path = spec_path
    _st.audit_path = audit_path
    # The runtime writes pending HITL requests to <output_dir>/hitl when
    # ANR_HITL=ui. The viz watches the same directory: by convention the
    # audit log lives next to it (output/audit.jsonl → output/hitl/).
    _st.hitl_dir = audit_path.parent / "hitl"
    _st.hitl_dir.mkdir(parents=True, exist_ok=True)
    _st.model = build_model(spec)
    _st.live = LiveState()
    _st.tailer = AuditTailer(audit_path, _st.live)
    _st.cursor = None
    # Sidecar summary cache lives next to the audit log: e.g.
    # output/audit.jsonl → output/summaries.jsonl
    summaries_path = audit_path.parent / "summaries.jsonl"
    _summary.init(summaries_path)


def _poll() -> None:
    if _st.tailer is not None:
        _st.tailer.poll()


def _view_state() -> LiveState:
    """Return the LiveState that the current view should render.

    Live mode returns the tailer's own LiveState (mutated in place by
    poll). Frozen mode replays the accumulated records up to the cursor.
    """
    if _st.cursor is None or _st.tailer is None:
        return _st.live
    idx = max(0, min(_st.cursor, len(_st.tailer.records) - 1))
    return build_state_at(_st.tailer.records, idx)


def _clamp_cursor(idx: int) -> int:
    assert _st.tailer is not None
    n = len(_st.tailer.records)
    if n == 0:
        return 0
    return max(0, min(idx, n - 1))


app = FastAPI(title="anr-viz", version="0.1.0")
app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    if _st.spec is None or _st.model is None:
        return "<h1>anr-viz: not configured</h1>"
    shell = (_TEMPLATES / "index.html").read_text(encoding="utf-8")
    return shell.replace(
        "{{ app_name }}", _st.spec.application.name
    ).replace("{{ app_version }}", _st.spec.application.version)


@app.get("/state/graph.json")
def state_graph() -> JSONResponse:
    _poll()
    assert _st.model is not None
    return JSONResponse(render_graph_state(_st.model, _view_state()))


@app.get("/state/sidebar.json")
def state_sidebar() -> JSONResponse:
    _poll()
    return JSONResponse(
        render_sidebar_state(
            _view_state(), tailer=_st.tailer, cursor=_st.cursor
        )
    )


@app.get("/state/trace.json")
def state_trace() -> JSONResponse:
    """Indented-trace projection of the audit log.

    Returns one row per audit record. The cursor controls (⏮◀▶⏭) drive
    the **graph's** time-travel; the trace itself always shows every
    row in the run so an operator can scan the full timeline while
    inspecting any moment. ``current_index`` highlights the cursor's
    position within the full list — it is a hint for the renderer, not
    a truncation marker.
    """
    _poll()
    if _st.tailer is None:
        return JSONResponse({"rows": [], "current_index": -1, "live_mode": True})
    records = list(_st.tailer.records)
    template_names = (
        {t.name for t in _st.spec.agent_templates} if _st.spec else set()
    )
    rows = build_trace(records, template_names=template_names)
    # current_index is a *record* index — TraceRow.index carries the
    # record's position, not its row position, so the JS highlight uses
    # the record-index space. (Rows < records when paired start/complete
    # collapse; the JS's latestVisibleAtOrBefore handles the gaps.)
    total_records = len(records)
    if _st.cursor is None:
        current_index = total_records - 1
    else:
        current_index = max(0, min(_st.cursor, total_records - 1))
    return JSONResponse(
        {
            "rows": to_jsonable(rows),
            "current_index": current_index,
            "live_mode": _st.cursor is None,
        }
    )


@app.get("/state/event")
def state_event(id: str) -> JSONResponse:
    """Return expanded detail payload for one audit event row."""
    _poll()
    if _st.tailer is None:
        return JSONResponse({"error": "no audit tailer"}, status_code=500)
    if not id.startswith("ev-"):
        return JSONResponse({"error": "bad event id"}, status_code=400)
    try:
        index = int(id[3:])
    except ValueError:
        return JSONResponse({"error": "bad event id"}, status_code=400)
    detail = render_event_detail(_view_state(), _st.tailer, index)
    if detail is None:
        return JSONResponse({"error": "event not found"}, status_code=404)
    return JSONResponse(detail)


@app.get("/state/node")
def state_node(id: str) -> JSONResponse:
    """Return the inspector payload for a graph node id."""
    if _st.spec is None or _st.spec_path is None:
        return JSONResponse({"error": "no spec"}, status_code=500)
    if id.startswith("inst:"):
        info = _runtime_instance_info(id)
        if info is None:
            return JSONResponse({"error": "runtime instance not found"}, status_code=404)
        return JSONResponse(info)
    info = build_node_info(_st.spec, _st.spec_path.parent, id)
    if info is None:
        return JSONResponse({"error": "node not found"}, status_code=404)
    return JSONResponse(info)


def _runtime_instance_info(node_id: str) -> dict | None:
    instance_id = node_id.split(":", 1)[1]
    view = _view_state()
    inst = view.instances.get(instance_id)
    if inst is None:
        return None
    sections = [
        {"label": "Blueprint", "kind": "text", "value": inst.blueprint},
        {"label": "Instance id", "kind": "text", "value": inst.instance_id},
    ]
    if inst.parent_instance_id:
        sections.append(
            {
                "label": "Parent instance",
                "kind": "text",
                "value": inst.parent_instance_id,
            }
        )
    if inst.granted_capabilities:
        sections.append(
            {
                "label": "Runtime grants",
                "kind": "json",
                "value": inst.granted_capabilities,
            }
        )
    return {
        "kind": "instance",
        "title": inst.instance_id,
        "subtitle": f"runtime instance · {inst.blueprint}",
        "sections": sections,
    }


@app.get("/state/summary")
async def state_summary(id: str, generate: bool = False) -> JSONResponse:
    """Return a one-sentence LLM narration for the audit event with the
    given stable id. Looks the event up in the tailer's records list.
    """
    _poll()
    if _st.tailer is None:
        return JSONResponse({"summary": ""})
    if not id.startswith("ev-"):
        return JSONResponse({"summary": "(event not found)"}, status_code=404)
    try:
        index = int(id[3:])
    except ValueError:
        return JSONResponse({"summary": "(event not found)"}, status_code=404)
    if index < 0 or index >= len(_st.tailer.records):
        return JSONResponse({"summary": "(event not found)"}, status_code=404)
    target = _st.tailer.records[index]
    cache_id = id
    legacy_id = None
    if target.get("ts") is not None:
        legacy_id = f"ev-{int(float(target['ts']) * 1000)}"
    if not has_cached(cache_id) and legacy_id and has_cached(legacy_id):
        cache_id = legacy_id
    if not generate and not has_cached(cache_id):
        return JSONResponse({"summary": "", "cached": False})
    text = await get_summary(cache_id, target)
    return JSONResponse({"summary": text, "cached": True})


@app.get("/state/controls", response_class=HTMLResponse)
def state_controls() -> str:
    _poll()
    total = len(_st.tailer.records) if _st.tailer is not None else 0
    return render_controls(
        view=_view_state(),
        cursor=_st.cursor,
        total=total,
    )


@app.get("/control/cursor")
def control_cursor(op: str, index: int | None = None) -> Response:
    _poll()
    assert _st.tailer is not None
    total = len(_st.tailer.records)
    if total == 0:
        # Nothing to scrub yet.
        return Response(status_code=204, headers={"HX-Trigger": "cursor-changed"})

    if op == "live":
        _st.cursor = None
    elif op == "first":
        _st.cursor = 0
    elif op == "last":
        _st.cursor = total - 1
    elif op == "prev":
        current = _st.cursor if _st.cursor is not None else total - 1
        _st.cursor = _clamp_cursor(current - 1)
    elif op == "next":
        if _st.cursor is None:
            # Already at the live edge — can't advance further.
            pass
        else:
            nxt = _st.cursor + 1
            if nxt >= total:
                # Stepping past the last recorded event resumes live mode.
                _st.cursor = None
            else:
                _st.cursor = nxt
    elif op == "goto":
        if index is None:
            return Response(status_code=400, content="goto requires index")
        _st.cursor = _clamp_cursor(int(index))
    else:
        return Response(status_code=400, content=f"unknown op: {op}")

    return Response(status_code=204, headers={"HX-Trigger": "cursor-changed"})


# ---------------------------------------------------------------------------
# HITL — bridge between the runtime and the operator's browser.
#
# The runtime's UIPrompter writes one ``req-<id>.json`` per pending
# checkpoint into ``hitl_dir``. This endpoint enumerates them so the JS
# UI can render a banner / modal. The operator's decision is POSTed to
# ``/control/hitl/decide?id=...``; we write ``res-<id>.json`` and the
# runtime's polling loop picks it up on the next tick.
# ---------------------------------------------------------------------------


@app.get("/state/hitl")
def state_hitl() -> JSONResponse:
    if _st.hitl_dir is None or not _st.hitl_dir.is_dir():
        return JSONResponse({"pending": []})
    pending: list[dict] = []
    for req_path in sorted(_st.hitl_dir.glob("req-*.json")):
        # Skip requests that already have a response file in flight —
        # the runtime hasn't yet had a chance to read it but it's no
        # longer "pending" from the operator's perspective.
        res_path = _st.hitl_dir / req_path.name.replace("req-", "res-", 1)
        if res_path.exists():
            continue
        try:
            data = json.loads(req_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        pending.append(data)
    return JSONResponse({"pending": pending})


@app.post("/control/hitl/decide")
async def control_hitl_decide(request: Request, id: str) -> JSONResponse:
    if _st.hitl_dir is None:
        return JSONResponse({"error": "no hitl dir"}, status_code=500)
    # Minimal id validation — ids are uuid4 hex slices, a-f0-9 only. We
    # never want a request_id like "../foo" to land us outside hitl_dir.
    if not id or any(c not in "0123456789abcdef" for c in id) or len(id) > 64:
        return JSONResponse({"error": "bad id"}, status_code=400)
    req_path = _st.hitl_dir / f"req-{id}.json"
    if not req_path.is_file():
        return JSONResponse({"error": "no such pending request"}, status_code=404)

    body = await request.json()
    action = str(body.get("action", "")).lower()
    if action not in {"approve", "reject", "modify"}:
        return JSONResponse({"error": "action must be approve|reject|modify"}, status_code=400)
    payload = {
        "action": action,
        "note": str(body.get("note", "") or ""),
        "modified_args": body.get("modified_args"),
        "decided_at": time.time(),
    }
    res_path = _st.hitl_dir / f"res-{id}.json"
    tmp = res_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, res_path)
    return JSONResponse({"ok": True})
