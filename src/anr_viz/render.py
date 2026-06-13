"""Live state serializers + small HTML fragment renderer for controls.

The graph and the sidebar are both drawn client-side. This module
exposes:

  * ``render_graph_state(model, state)`` — JSON describing the static
    graph + the live overlay (active node, firing edges with verb
    labels, instance counts).
  * ``render_sidebar_state(state)`` — JSON describing the run totals
    and the rolling event ring, with a compact-row summary plus the
    fields needed for an expanded debug view.
  * ``render_controls(...)`` — time-cursor toolbar (HTML, served via
    HTMX so the cursor controls keep working without JS plumbing).
"""

from __future__ import annotations

import ast
from typing import Any

from .model import GraphModel, Node
from .state import AuditEvent, LiveState

# Result-preview value keys, in order of preference. When a tool returns
# a dict like {"path": "x.md", "size_bytes": 1576}, the user mostly cares
# about "x.md" — size_bytes is plumbing noise. We unwrap dicts to the
# primary scalar before showing them in the sidebar.
_PRIMARY_OUTCOME_KEYS = (
    "content",
    "result",
    "value",
    "text",
    "path",
    "file",
    "filename",
    "url",
    "name",
    "id",
    "message",
)

# Lane order for dagre rankDir='LR'. Lower number = further left.
_LANE = {"data": 0, "tool": 1, "hitl": 1.35, "agent": 2, "instance": 2.65, "template": 3}

# Preferred arg keys per tool — pick the one that's most informative
# in a one-line summary. Falls back to the first arg if none match.
_PREFERRED_ARG_KEYS = (
    "pattern",
    "path",
    "url",
    "subject",
    "topic",
    "query",
    "email_id",
    "id",
    "name",
    "key",
)


# ---------------------------------------------------------------------------
# Graph
# ---------------------------------------------------------------------------


def render_graph_state(model: GraphModel, state: LiveState) -> dict[str, Any]:
    """Return cytoscape-ready elements + a live-state overlay block."""
    nodes_by_id: dict[str, Node] = {n.id: n for n in model.nodes}
    # When a caller's name belongs to a template (R4 instantiation), the
    # runtime emits caller="<template_name>" but the graph carries no
    # agent:<name> node — only template:<name>. Track this so we can
    # resolve callers to whichever node id actually exists.
    template_names: set[str] = {
        n.label for n in model.nodes if n.kind == "template"
    }

    def _resolve_caller_node(caller: str) -> str:
        if caller in template_names and f"agent:{caller}" not in nodes_by_id:
            return f"template:{caller}"
        return f"agent:{caller}"

    hitl_guarded: set[str] = set()
    for n in model.nodes:
        if n.kind != "hitl":
            continue
        for e in model.edges:
            if e.src == n.id and e.kind == "hitl_gate":
                hitl_guarded.add(e.dst)

    elements: list[dict[str, Any]] = []
    for n in model.nodes:
        sublabel = n.sublabel
        if n.kind == "template" and not sublabel:
            sublabel = "agent blueprint"
        elements.append(
            {
                "group": "nodes",
                "data": {
                    "id": n.id,
                    "kind": n.kind,
                    "label": n.label,
                    "sublabel": sublabel,
                    "tags": n.tags,
                    "lane": _LANE[n.kind],
                    "has_hitl": n.id in hitl_guarded,
                },
            }
        )

    for e in model.edges:
        if e.src not in nodes_by_id or e.dst not in nodes_by_id:
            continue
        src_lane = _LANE[nodes_by_id[e.src].kind]
        dst_lane = _LANE[nodes_by_id[e.dst].kind]
        reversed_for_layout = src_lane > dst_lane
        layout_src = e.dst if reversed_for_layout else e.src
        layout_dst = e.src if reversed_for_layout else e.dst

        elements.append(
            {
                "group": "edges",
                "data": {
                    "id": e.id,
                    "source": layout_src,
                    "target": layout_dst,
                    "kind": e.kind,
                    "label": e.label,
                    "semantic_src": e.src,
                    "semantic_dst": e.dst,
                    "reversed": reversed_for_layout,
                },
            }
        )

    # Runtime instances are dynamic nodes derived from the audit log. They
    # make sub-agents visible as live invocations rather than collapsing them
    # into a count on the declared blueprint.
    instance_node_ids: set[str] = set()
    for inst in state.instances.values():
        node_id = _instance_node_id(inst.instance_id)
        instance_node_ids.add(node_id)
        suffix = inst.instance_id.split("#", 1)[-1]
        elements.append(
            {
                "group": "nodes",
                "data": {
                    "id": node_id,
                    "kind": "instance",
                    "label": f"{inst.blueprint}#{suffix}",
                    "sublabel": "runtime instance",
                    "tags": ["runtime_instance"],
                    "lane": _LANE["instance"],
                    "blueprint": inst.blueprint,
                },
            }
        )

        blueprint_id = (
            f"template:{inst.blueprint}"
            if inst.blueprint in template_names and f"agent:{inst.blueprint}" not in nodes_by_id
            else f"agent:{inst.blueprint}"
        )
        if blueprint_id in nodes_by_id:
            elements.append(
                {
                    "group": "edges",
                    "data": {
                        "id": f"instance_of:{inst.instance_id}",
                        "source": blueprint_id,
                        "target": node_id,
                        "kind": "instance_of",
                        "label": "instance",
                        "semantic_src": blueprint_id,
                        "semantic_dst": node_id,
                    },
                }
            )
        if inst.parent_instance_id:
            parent_id = _instance_node_id(inst.parent_instance_id)
            if parent_id in instance_node_ids or parent_id in {
                _instance_node_id(i.instance_id) for i in state.instances.values()
            }:
                elements.append(
                    {
                        "group": "edges",
                        "data": {
                            "id": f"runtime_parent:{inst.parent_instance_id}->{inst.instance_id}",
                            "source": parent_id,
                            "target": node_id,
                            "kind": "runtime_parent",
                            "label": "sub-agent",
                            "semantic_src": parent_id,
                            "semantic_dst": node_id,
                        },
                    }
                )

    # ---- Live overlay -----------------------------------------------------
    active_id = None
    latest = state.recent_events[-1] if state.recent_events else None
    if latest and latest.kind == "hitl_checkpoint" and latest.checkpoint_id:
        gate_id = f"hitl:{latest.checkpoint_id}"
        if gate_id in nodes_by_id:
            active_id = gate_id
    if active_id is None and state.active_instance_id:
        maybe_inst = _instance_node_id(state.active_instance_id)
        if maybe_inst in instance_node_ids:
            active_id = maybe_inst
    if active_id is None and state.active_caller:
        active_id = _resolve_caller_node(state.active_caller)
        if active_id not in nodes_by_id:
            active_id = None

    # Highlight only the *single* most recently fired edge. The sidebar
    # already shows the event trail; the graph just needs to answer
    # "what action is happening right now". In cursor mode this is the
    # cursor's event; in live mode it's the latest event whose 1.5s
    # glow window has not yet expired.
    #
    # When the same edge fires several times in a row (e.g. three
    # grep_files calls back to back), a single highlighted edge looks
    # static across steps. We tack on a "×N" repeat counter so stepping
    # the cursor visibly increments the label.
    firing_edges: list[dict[str, str]] = []
    fires = list(state.edge_fires)
    if fires:
        src, dst, ts, label, kind_hint = fires[-1]
        if src.startswith("agent:"):
            name = src.split(":", 1)[1]
            if name in template_names and f"agent:{name}" not in nodes_by_id:
                src = f"template:{name}"
        from .state import EDGE_HIGHLIGHT_SEC

        if state.view_now - ts < EDGE_HIGHLIGHT_SEC:
            # Count consecutive trailing fires that hit the same edge.
            run_count = 1
            for prev in reversed(fires[:-1]):
                p_src, p_dst, _, _, p_kind = prev
                # Apply the same template rewrite to comparisons.
                if p_src.startswith("agent:"):
                    p_name = p_src.split(":", 1)[1]
                    if (
                        p_name in template_names
                        and f"agent:{p_name}" not in nodes_by_id
                    ):
                        p_src = f"template:{p_name}"
                if (p_src, p_dst, p_kind) == (src, dst, kind_hint):
                    run_count += 1
                else:
                    break
            display_label = f"{label} ×{run_count}" if run_count > 1 else label
            if active_id and active_id.startswith("inst:") and kind_hint != "hitl_gate":
                edge_id = f"runtime_action:{active_id}->{dst}:{kind_hint}"
                if dst in nodes_by_id or dst in instance_node_ids:
                    elements.append(
                        {
                            "group": "edges",
                            "data": {
                                "id": edge_id,
                                "source": active_id,
                                "target": dst,
                                "kind": "runtime_action",
                                "label": display_label,
                                "semantic_src": active_id,
                                "semantic_dst": dst,
                            },
                        }
                    )
                    firing_edges.append({"id": edge_id, "label": display_label})
            else:
                for e in model.edges:
                    if e.src == src and e.dst == dst and e.kind == kind_hint:
                        firing_edges.append({"id": e.id, "label": display_label})
                        break

    instance_counts: dict[str, int] = {}
    for inst in state.instances.values():
        instance_counts[inst.blueprint] = instance_counts.get(inst.blueprint, 0) + 1

    return {
        "elements": elements,
        "live": {
            "active_node_id": active_id,
            "firing_edges": firing_edges,
            "instance_counts": instance_counts,
            "active_instance_id": state.active_instance_id,
        },
    }


def _instance_node_id(instance_id: str) -> str:
    return f"inst:{instance_id}"


# ---------------------------------------------------------------------------
# Sidebar (JSON — rendered by JS)
# ---------------------------------------------------------------------------


def render_sidebar_state(
    state: LiveState,
    tailer: Any | None = None,
    cursor: int | None = None,
) -> dict[str, Any]:
    """Run totals + the *full* event log.

    The events list is always every audit record the tailer has seen,
    not the cursor-filtered window — scrubbing back must not hide the
    future. Each event carries its absolute index into ``tailer.records``
    so a click can jump the cursor right to it.

    ``current_index`` marks which row corresponds to the current cursor
    position so the client can highlight it (and pull the right row's
    id for the LLM step-summary fetch).
    """
    # Build events from the tailer's full records list when available.
    # Fall back to state.recent_events for cases where the tailer hasn't
    # been wired (defensive — production path always passes one).
    if tailer is not None and getattr(tailer, "records", None):
        from .state import _record_to_event  # local import: avoid cycle

        records = list(tailer.records)
        events = [_record_to_event(rec) for rec in records]
    else:
        records = []
        events = list(state.recent_events)

    run_start_ts = events[0].ts if events else 0.0

    # When the cursor parks on a step, the active blueprint counts come
    # from the cursor's LiveState. For the live tail (cursor=None) this
    # is just the live state. Either way state.instances is correct.
    multi_blueprints: set[str] = set()
    blueprint_counts: dict[str, int] = {}
    for inst in state.instances.values():
        blueprint_counts[inst.blueprint] = blueprint_counts.get(inst.blueprint, 0) + 1
    for bp, c in blueprint_counts.items():
        if c > 1:
            multi_blueprints.add(bp)

    out_events: list[dict[str, Any]] = []
    for i, ev in enumerate(events):
        d = _event_to_sidebar_dict(
            ev,
            run_start_ts,
            multi_blueprints,
            row_id=f"ev-{i}",
            index=i,
            include_detail=False,
        )
        out_events.append(d)

    if cursor is None:
        current_index = len(out_events) - 1 if out_events else -1
    else:
        current_index = max(0, min(cursor, len(out_events) - 1))

    return {
        "totals": {
            "tool_calls": state.totals.get("tool_calls", 0),
            "llm_calls": state.totals.get("llm_calls", 0),
            "cost_usd": state.totals.get("cost_usd", 0),
            "elapsed_sec": state.totals.get("elapsed_sec", 0),
        },
        "events": out_events,
        "current_index": current_index,
        "live_mode": cursor is None,
    }


def render_event_detail(
    state: LiveState,
    tailer: Any,
    index: int,
) -> dict[str, Any] | None:
    """Return heavyweight fields for one sidebar event.

    The polling sidebar payload intentionally stays compact; expanded rows
    fetch args/raw/full outcome through this endpoint on demand.
    """
    records = list(getattr(tailer, "records", []))
    if not records or index < 0 or index >= len(records):
        return None

    from .state import _record_to_event  # local import: avoid cycle

    events = [_record_to_event(rec) for rec in records]
    run_start_ts = events[0].ts if events else 0.0

    blueprint_counts: dict[str, int] = {}
    for inst in state.instances.values():
        blueprint_counts[inst.blueprint] = blueprint_counts.get(inst.blueprint, 0) + 1
    multi_blueprints = {bp for bp, c in blueprint_counts.items() if c > 1}

    return _event_to_sidebar_dict(
        events[index],
        run_start_ts,
        multi_blueprints,
        row_id=f"ev-{index}",
        index=index,
        include_detail=True,
    )


def _event_to_sidebar_dict(
    ev: AuditEvent,
    run_start_ts: float,
    multi_blueprints: set[str],
    *,
    row_id: str,
    index: int,
    include_detail: bool,
) -> dict[str, Any]:
    rec = ev.raw
    args = rec.get("args") or {}
    verb, arg_preview = _verb_and_arg(ev, args)
    outcome_short, outcome_full, outcome_ok = _outcome(rec)

    instance_short = None
    if ev.instance_id and ev.caller in multi_blueprints:
        # Show the per-instance suffix when multiple instances of the
        # same blueprint exist (R1 spawn / R4 instantiate).
        instance_short = ev.instance_id.split("#", 1)[-1]

    detail_extras: dict[str, Any] = {}
    if ev.kind == "hitl_checkpoint":
        detail_extras["checkpoint_id"] = ev.checkpoint_id
        detail_extras["pattern"] = ev.pattern
        detail_extras["decision"] = rec.get("decision") or rec.get("action")
    if ev.kind == "reshape":
        detail_extras["change"] = ev.change
    if ev.kind == "agent_turn":
        detail_extras["model"] = args.get("model")
        detail_extras["turn"] = args.get("turn")
        detail_extras["requested_tools"] = args.get("requested_tools") or []
        detail_extras["exposed_tools"] = args.get("exposed_tools") or []
    if ev.kind == "boundary_decision":
        detail_extras["policy_id"] = rec.get("policy_id")
        detail_extras["direction"] = rec.get("direction")
        detail_extras["match_count"] = rec.get("match_count")
    if ev.kind == "orchestrator_decision":
        detail_extras["decision"] = rec.get("decision")
        detail_extras["reason"] = rec.get("reason")
        detail_extras["trigger_event"] = rec.get("trigger_event")
        detail_extras["control_plane_context"] = rec.get("control_plane_context")
    if rec.get("parent_instance_id"):
        detail_extras["parent_instance_id"] = rec["parent_instance_id"]
    if rec.get("delegation_depth"):
        detail_extras["delegation_depth"] = rec["delegation_depth"]

    has_detail = bool(args) or outcome_full or detail_extras

    d: dict[str, Any] = {
        "id": row_id,
        "index": index,
        "ts": ev.ts,
        "ts_offset": max(0.0, ev.ts - run_start_ts),
        "kind": ev.kind,
        "agent": ev.caller,
        "instance_id": ev.instance_id,
        "instance_short": instance_short,
        "verb": verb,
        "arg_preview": arg_preview,
        "outcome_ok": outcome_ok,
        "outcome_preview": outcome_short,
        "has_detail": has_detail,
    }
    if include_detail:
        d.update(
            {
                "outcome_full": outcome_full,
                "args": args,
                "extras": detail_extras,
                "raw": rec,
            }
        )
    return d


def _verb_and_arg(ev: AuditEvent, args: dict[str, Any]) -> tuple[str, str]:
    """Pick the verb (action word) and the most informative one-arg snippet."""
    tool = ev.tool or ""
    if ev.kind == "agent_turn":
        requested = args.get("requested_tools") or []
        turn = args.get("turn")
        if isinstance(requested, list) and requested:
            return ("think", f"turn {turn} → {', '.join(map(str, requested[:3]))}")
        return ("think", f"turn {turn}" if turn else "")
    if tool == "delegate":
        target = args.get("target_agent", "")
        return ("delegate", f"→ {target}" if target else "")
    if tool == "spawn_parallel":
        of = args.get("of_agent", "")
        tasks = args.get("tasks") or []
        n = len(tasks) if isinstance(tasks, list) else 0
        return ("spawn", f"{of} ×{n}" if n else of)
    if tool == "instantiate_template":
        return ("instantiate", str(args.get("template", "")))
    if tool == "request_grant":
        kind = args.get("kind", "")
        name = args.get("name", "")
        return ("grant", f"{kind} {name}".strip())
    if tool == "request_human_review":
        reason = _truncate(str(args.get("reason", "")), 36)
        return ("ask human", reason)

    if ev.kind == "hitl_checkpoint":
        cp = ev.checkpoint_id or ""
        pat = ev.pattern or ""
        return ("hitl", f"{cp} ({pat})".strip(" ()"))
    if ev.kind == "reshape":
        return ("reshape", ev.change or "")
    if ev.kind == "policy_refusal":
        return ("refused", tool)
    if ev.kind == "tool_error":
        return ("error", tool)
    if ev.kind == "boundary_decision":
        outcome = ((ev.raw.get("outcome") or {}).get("enforcement_outcome") or "boundary")
        return (str(outcome), tool)
    if ev.kind == "orchestrator_decision":
        return ("orchestrator", str(ev.raw.get("decision") or tool))

    # Regular tool call — pick a representative arg.
    if not args:
        return (tool, "")
    for key in _PREFERRED_ARG_KEYS:
        if key in args:
            return (tool, _format_kv(key, args[key], 30))
    k, v = next(iter(args.items()))
    return (tool, _format_kv(k, v, 30))


def _outcome(rec: dict[str, Any]) -> tuple[str, str, bool | None]:
    """Return (short_preview, full_preview, ok).

    The runtime emits ``outcome.value_preview`` as ``repr(value)``,
    which means string values come back as ``"'# Title\\n\\nbody...'"``
    (literal backslash-n, surrounding quotes). For display we recover
    the original Python value via ``literal_eval`` so the modal shows
    real newlines and dicts get pretty-printed JSON-style.
    """
    outcome = rec.get("outcome")
    if not isinstance(outcome, dict):
        return ("", "", None)
    ok = outcome.get("ok")
    preview = outcome.get("value_preview") or ""
    if not preview and outcome.get("error"):
        preview = str(outcome["error"])
    if not isinstance(preview, str):
        preview = str(preview)

    truncated_marker = preview.endswith("...<truncated>")
    parseable = preview[: -len("...<truncated>")] if truncated_marker else preview
    parsed: Any = None
    try:
        parsed = ast.literal_eval(parseable)
    except (ValueError, SyntaxError, MemoryError):
        # Truncation may have lopped off a closing quote — retry once.
        if parseable.startswith("'"):
            try:
                parsed = ast.literal_eval(parseable + "'")
            except (ValueError, SyntaxError, MemoryError):
                parsed = None
        elif parseable.startswith('"'):
            try:
                parsed = ast.literal_eval(parseable + '"')
            except (ValueError, SyntaxError, MemoryError):
                parsed = None

    if isinstance(parsed, str):
        full = parsed + ("\n\n…<truncated>" if truncated_marker else "")
    elif isinstance(parsed, (dict, list)):
        try:
            import json as _json

            full = _json.dumps(parsed, indent=2, ensure_ascii=False, default=str)
            if truncated_marker:
                full += "\n\n…<truncated>"
        except (TypeError, ValueError):
            full = preview
    else:
        full = preview

    short_source = _unwrap_primary(preview)
    short = _truncate(short_source.replace("\n", " "), 48)
    if ok is False and not short:
        short = "error" if rec.get("kind") == "tool_error" else "refused"
    return (short, full, ok)


def _unwrap_primary(preview: str) -> str:
    """If `preview` is a Python repr of a dict, return the most
    informative scalar value inside; otherwise return it unchanged."""
    s = preview.strip()
    if not s or not (s.startswith("{") and s.endswith("}")):
        return preview
    try:
        v = ast.literal_eval(s)
    except (ValueError, SyntaxError, MemoryError):
        return preview
    if not isinstance(v, dict):
        return preview
    for key in _PRIMARY_OUTCOME_KEYS:
        if key in v:
            inner = v[key]
            return inner if isinstance(inner, str) else repr(inner)
    return preview


def _format_kv(key: str, value: Any, max_value_len: int) -> str:
    if isinstance(value, str):
        v = _truncate(value, max_value_len)
        return f'{key}="{v}"'
    if isinstance(value, (list, tuple)):
        return f"{key}=[{len(value)}]"
    if isinstance(value, dict):
        return f"{key}={{{len(value)}}}"
    s = str(value)
    return f"{key}={_truncate(s, max_value_len)}"


def _truncate(s: str, n: int) -> str:
    if len(s) <= n:
        return s
    return s[: max(1, n - 1)] + "…"


# ---------------------------------------------------------------------------
# Controls (HTML — fragment served to HTMX)
# ---------------------------------------------------------------------------


def render_controls(*, view: LiveState, cursor: int | None, total: int) -> str:
    """Time-cursor control bar."""
    if total == 0:
        step_label = "step — / 0"
        mode_cls, mode_label = "mode mode--live", "LIVE"
    elif cursor is None:
        step_label = f"step {total} / {total}"
        mode_cls, mode_label = "mode mode--live", "LIVE"
    else:
        step_label = f"step {cursor + 1} / {total}"
        mode_cls, mode_label = "mode mode--paused", "PAUSED"

    ts_label = ""
    if view.recent_events:
        run_start = view.recent_events[0].ts
        ts_label = (
            f"<span class='ctrl__ts'>+{view.recent_events[-1].ts - run_start:.2f}s</span>"
        )

    at_first = total == 0 or (cursor is not None and cursor <= 0)
    at_live = cursor is None
    at_last_recorded = total == 0 or (cursor is not None and cursor >= total - 1)

    def _btn(op: str, glyph: str, title: str, disabled: bool = False) -> str:
        attrs = (
            f'data-cursor-op="{op}" title="{title}" aria-label="{title}"'
        )
        cls = "ctrl__btn" + (" ctrl__btn--disabled" if disabled else "")
        dis = " disabled" if disabled else ""
        return f'<button class="{cls}" {attrs}{dis}>{glyph}</button>'

    buttons = (
        _btn("first", "⏮", "jump to first event", at_first)
        + _btn("prev", "◀", "previous event", at_first)
        + _btn("next", "▶", "next event", at_live)
        + _btn("last", "⏭", "jump to last recorded event", at_last_recorded and not at_live)
        + _btn("live", "⏺ LIVE", "resume live tail", at_live)
    )

    return (
        f'<div class="ctrl__row">'
        f'<div class="ctrl__buttons">{buttons}</div>'
        f'<div class="ctrl__status">'
        f'<span class="{mode_cls}">{mode_label}</span>'
        f'<span class="ctrl__step">{step_label}</span>'
        f"{ts_label}"
        f"</div>"
        f"</div>"
    )
