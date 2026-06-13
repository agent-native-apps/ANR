"""Trace projection of the audit log — a flat, time-ordered list of rows
indented by ``delegation_depth``.

This is the data model behind the trace-first visualizer panel agreed in
``DIALOGUE.md`` (turns 1–4). Each row corresponds to exactly one audit
record. Indentation, ordering, and parent/child relationship come from
fields the mesh already emits (``delegation_depth``, ``parent_instance_id``,
``ts``); we add a small amount of derived metadata for the cross-panel
highlight protocol that lets the trace and the topology panel speak to
each other without the client doing schema-aware lookups.

Stage 1 of the rewrite: this is intentionally a point-event projection.
The audit log emits records on completion (``tool_call`` after the call
returns; ``hitl_checkpoint`` after the human decides) — there are no
``agent_start`` / ``agent_end`` events yet. So rows have a timestamp but
not a duration. Bars / true Gantt waterfall are Stage 2; the consumer is
designed so a future ``duration_sec`` field can be added without
restructuring.

The mesh is the source of truth; this module is purely derived state.
"""

from __future__ import annotations

import ast
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

# Pseudo-tools handled by the mesh directly (not via MCP). The trace
# treats these specially because their "target" is another agent or
# template, not a tool node.
_PSEUDO_TOOLS: frozenset[str] = frozenset(
    {
        "delegate",
        "spawn_parallel",
        "request_grant",
        "instantiate_template",
        "request_human_review",
    }
)


@dataclass
class TraceRow:
    """One row in the trace panel — derived from a single audit record.

    Fields split into three groups:

      * **Identity** — id/index/ts/depth and the caller's blueprint +
        instance ids.
      * **Display fields** — what the JS renders directly. Computed
        server-side so the client never re-parses audit shape (per
        DIALOGUE.md turn 6's display-fields architecture decision).
      * **Raw fields** — kind/tool/args/outcome and the full record
        for inline-expand and side-panel detail surfaces.
    """

    # ---- identity --------------------------------------------------
    id: str
    index: int
    ts: float
    ts_offset_sec: float
    depth: int
    caller: str
    caller_instance_id: str
    parent_instance_id: str | None

    # ---- display fields (the four columns + extras) ----------------
    # The agent who did the thing. Usually equals ``caller``; kept as a
    # separate field so future renderings can localise / abbreviate.
    agent_label: str = ""
    # The action verb / category word. Examples: ``read_file``,
    # ``delegate``, ``spawn``, ``HITL``, ``instantiate``, ``grant``,
    # ``ask human``, ``refused``, ``error``, ``turn``.
    action_label: str = ""
    # The action's target / argument in human terms. Examples:
    # ``pattern="permission"`` (tool args), ``risk_scorer`` (delegate
    # target), ``policy_inspector ×4`` (spawn fan-out), ``GDPR``
    # (template instantiation parameter), ``budget_checkpoint`` (HITL
    # checkpoint id), or empty when the action is its own subject.
    subject_label: str = ""
    # The outcome state in one short phrase. Examples: ``ok`` (a
    # successful tool call with no notable return), a truncated
    # value_preview (`"# Title\n..."`), an error message (``refused: …``),
    # or a HITL decision (``approve`` / ``reject`` / ``modify``). Empty
    # when the row's outcome is structurally trivial (e.g. ``None``
    # returns) — per the user's "narrate the run" criterion in turn 8.
    status_label: str = ""
    # Sibling disambiguation, e.g. ``#1`` … ``#N``, *only* when ≥2 live
    # instances of this caller's blueprint exist in the run. Empty
    # otherwise — keeps the random hex `#abc123` out of the default row.
    # The full ``caller_instance_id`` is still available for the side
    # detail panel via the ``raw`` field.
    instance_label: str = ""
    # Category key for color: ``tool`` | ``delegate`` | ``spawn`` |
    # ``reshape`` | ``hitl`` | ``refused`` | ``error`` | ``turn``.
    # The CSS picks the chip / row tint from this single value.
    category: str = ""

    # ---- raw / kind-specific ---------------------------------------
    kind: str = ""
    tool: str = ""
    pseudo: bool = False
    arg_preview: str = ""
    outcome_ok: bool | None = None
    outcome_preview: str = ""
    extras: dict[str, Any] = field(default_factory=dict)
    target_node_ids: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------


def build_trace(
    records: list[dict[str, Any]],
    *,
    template_names: Iterable[str] = (),
) -> list[TraceRow]:
    """Project the audit records list into a flat list of TraceRow.

    ``template_names`` lets the cross-reference resolver pick
    ``template:<name>`` instead of ``agent:<name>`` when the caller is a
    runtime-instantiated template (no ``agent:`` node exists for it).
    Pass the spec's ``[t.name for t in spec.agent_templates]``; default
    of ``()`` produces ``agent:`` ids for every caller and is fine for
    the (common) case where no R4 instantiation has happened yet.

    Two-pass: first builds a stable 1-based sibling index per blueprint
    so ``instance_label`` only fires when there are ≥2 instances of the
    same caller in the run. Then folds each record into a TraceRow.

    Spawning pseudo-tools (``delegate`` / ``spawn_parallel`` /
    ``instantiate_template``) emit a ``tool_call_start`` before awaiting
    the sub-agent and a ``tool_call`` (+ optional ``reshape``) on
    completion, all sharing a ``call_id``. The start owns the row's
    timeline position (so children appear under their parent), and the
    completion records are merged into that row to supply the outcome /
    extras — they don't produce rows of their own.
    """
    if not records:
        return []
    template_set = set(template_names)
    run_start = float(records[0].get("ts") or 0.0)
    instance_index, blueprint_count = _index_instances(records)
    completions = _pair_completions(records)
    rows: list[TraceRow] = []
    for i, r in enumerate(records):
        cid = r.get("call_id")
        kind = r.get("kind")
        if cid and kind in {"tool_call", "reshape", "policy_refusal", "tool_error"}:
            # Paired completion of an earlier tool_call_start — its outcome
            # is folded into the start row, no separate row here.
            continue
        if kind == "tool_call_start" and cid:
            r = _merge_completion(r, completions.get(cid, {}))
        rows.append(
            _build_row(
                i,
                r,
                run_start=run_start,
                template_set=template_set,
                instance_index=instance_index,
                blueprint_count=blueprint_count,
            )
        )
    return rows


def _pair_completions(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Collect the merged completion fields for each ``call_id``.

    A spawning pseudo-tool writes two or three records:
      * ``tool_call_start`` — emitted before the await; owns the row.
      * ``tool_call`` — outcome (value_preview / ok).
      * ``reshape`` — only for ``instantiate_template``; carries
        ``change`` / ``template`` / ``parameters`` for the side panel.

    The merged dict overrides the start's ``kind`` so the row's
    projection (``_action_subject_status``) uses the completion's branch.
    """
    out: dict[str, dict[str, Any]] = {}
    for r in records:
        cid = r.get("call_id")
        kind = r.get("kind")
        if not cid or kind == "tool_call_start":
            continue
        merged = out.setdefault(cid, {})
        if kind == "reshape":
            merged["kind"] = "reshape"
            for k in (
                "change",
                "template",
                "parameters",
                "capability_kind",
                "capability_name",
            ):
                if r.get(k) is not None:
                    merged[k] = r[k]
        else:
            # tool_call / policy_refusal / tool_error all carry outcome.
            if "outcome" in r:
                merged["outcome"] = r["outcome"]
            # Don't override a kind already set by a reshape completion —
            # the reshape branch produces the richer row for templates.
            merged.setdefault("kind", kind)
    return out


def _merge_completion(
    start: dict[str, Any], completion: dict[str, Any]
) -> dict[str, Any]:
    """Overlay a paired completion onto a tool_call_start record.

    Returns a *new* dict so the original audit record is left alone.
    """
    if not completion:
        # In-flight: render as a tool_call with no outcome yet.
        return {**start, "kind": "tool_call"}
    return {**start, **completion}


def _index_instances(
    records: list[dict[str, Any]],
) -> tuple[dict[str, int], dict[str, int]]:
    """Pre-pass over the records to assign a 1-based sibling index per
    instance, in first-seen order within its blueprint.

    Returns ``(instance_id → index, blueprint_name → instance_count)``
    so the row builder can emit ``instance_label = "#N"`` only when the
    blueprint has ≥2 instances live in the run.
    """
    blueprint_to_instances: dict[str, list[str]] = {}
    for r in records:
        instance_id = r.get("instance_id")
        caller = r.get("caller")
        if not instance_id or not caller:
            continue
        ids = blueprint_to_instances.setdefault(str(caller), [])
        if instance_id not in ids:
            ids.append(str(instance_id))
    instance_index: dict[str, int] = {}
    blueprint_count: dict[str, int] = {}
    for bp, ids in blueprint_to_instances.items():
        blueprint_count[bp] = len(ids)
        for i, iid in enumerate(ids, start=1):
            instance_index[iid] = i
    return instance_index, blueprint_count


def _build_row(
    index: int,
    rec: dict[str, Any],
    *,
    run_start: float,
    template_set: set[str],
    instance_index: dict[str, int],
    blueprint_count: dict[str, int],
) -> TraceRow:
    ts = float(rec.get("ts") or run_start)
    caller = str(rec.get("caller", ""))
    instance_id = str(rec.get("instance_id", "") or "")
    parent_instance_id = rec.get("parent_instance_id") or None
    depth = int(rec.get("delegation_depth", 0) or 0)
    kind = str(rec.get("kind", ""))
    tool = str(rec.get("tool", "") or "")
    args = rec.get("args") or {}
    outcome = rec.get("outcome") if isinstance(rec.get("outcome"), dict) else None

    arg_preview = _arg_preview(tool, args)
    outcome_ok = outcome.get("ok") if outcome else None
    outcome_preview = _outcome_preview(kind, rec, outcome)
    extras = _extras(kind, rec)
    target_node_ids = _target_nodes(
        caller=caller,
        tool=tool,
        kind=kind,
        args=args,
        rec=rec,
        template_set=template_set,
    )

    # Display fields (the four-column row + sibling label + category).
    # Sibling-disambiguation: only emit `#N` when the blueprint has ≥2
    # instances live in the run — keeping random hex out of the default
    # row was Codex's number-one cut from turn 6.
    sibling_idx = instance_index.get(instance_id)
    show_instance_label = (
        sibling_idx is not None
        and blueprint_count.get(caller, 0) >= 2
    )
    instance_label = f"#{sibling_idx}" if show_instance_label else ""

    action_label, subject_label, status_label, category = _action_subject_status(
        kind=kind,
        tool=tool,
        args=args,
        outcome=outcome,
        rec=rec,
        arg_preview=arg_preview,
        outcome_preview=outcome_preview,
    )

    return TraceRow(
        id=f"ev-{index}",
        index=index,
        ts=ts,
        ts_offset_sec=max(0.0, ts - run_start),
        depth=depth,
        caller=caller,
        caller_instance_id=instance_id,
        parent_instance_id=parent_instance_id,
        agent_label=caller,
        action_label=action_label,
        subject_label=subject_label,
        status_label=status_label,
        instance_label=instance_label,
        category=category,
        kind=kind,
        tool=tool,
        pseudo=tool in _PSEUDO_TOOLS,
        arg_preview=arg_preview,
        outcome_ok=outcome_ok,
        outcome_preview=outcome_preview,
        extras=extras,
        target_node_ids=target_node_ids,
        raw=rec,
    )


def to_jsonable(rows: list[TraceRow]) -> list[dict[str, Any]]:
    """asdict each row — keeps the JSON encoder simple in the endpoint."""
    return [asdict(r) for r in rows]


# ---------------------------------------------------------------------------
# Per-kind helpers (kept small + tested by the audit shape, not invented)
# ---------------------------------------------------------------------------


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
    "filename",
    "target_agent",
    "of_agent",
    "template",
)


def _arg_preview(tool: str, args: dict[str, Any]) -> str:
    if not args:
        return ""
    if tool == "delegate":
        target = args.get("target_agent", "")
        return f"→ {target}" if target else ""
    if tool == "spawn_parallel":
        of = args.get("of_agent", "")
        n = len(args.get("tasks", [])) if isinstance(args.get("tasks"), list) else 0
        return f"{of} ×{n}" if (of and n) else of
    if tool == "instantiate_template":
        params = args.get("parameters") or {}
        if isinstance(params, dict) and params:
            head = next(iter(params.values()))
            return f"{args.get('template', '')}({head})" if head else str(args.get("template", ""))
        return str(args.get("template", ""))
    if tool == "request_grant":
        return f"{args.get('kind', '')} {args.get('name', '')}".strip()
    if tool == "request_human_review":
        return _truncate(str(args.get("reason", "")), 48)
    for key in _PREFERRED_ARG_KEYS:
        if key in args:
            return _format_kv(key, args[key])
    k, v = next(iter(args.items()))
    return _format_kv(k, v)


def _outcome_preview(kind: str, rec: dict[str, Any], outcome: dict | None) -> str:
    if kind == "hitl_checkpoint":
        decision = rec.get("decision") or rec.get("action") or ""
        cp = rec.get("checkpoint_id") or ""
        return f"{cp} · {decision}" if decision else cp
    if kind == "reshape":
        change = rec.get("change") or ""
        if change == "instantiate_template":
            return f"{change} {rec.get('template', '')}".strip()
        if change == "acquire_capability":
            return f"{change} {rec.get('capability_kind', '')} {rec.get('capability_name', '')}".strip()
        return change
    if kind == "orchestrator_decision":
        return _truncate(str(rec.get("reason") or rec.get("decision") or ""), 64)
    if kind in {"policy_refusal", "tool_error"}:
        if outcome:
            err = outcome.get("error", "") or ""
            return _truncate(err, 64)
        return kind
    # tool_call: outcome.value_preview is repr() — unwrap to the
    # underlying value so ``"# Title…"`` shows as ``# Title…`` (no
    # leading/trailing single-quote noise) and ``[…]`` collapses to
    # something compact like ``[6 items]``.
    if outcome:
        prev = outcome.get("value_preview", "") or ""
        if isinstance(prev, str):
            return _truncate(_unwrap_value_preview(prev).replace("\n", " "), 56)
    return ""


def _unwrap_value_preview(preview: str) -> str:
    """Best-effort decode of an audit ``value_preview`` (which is
    always ``repr(value)``) back to a display-friendly form."""
    s = preview.strip()
    if not s:
        return s
    truncated = s.endswith("...<truncated>")
    parseable = s[: -len("...<truncated>")] if truncated else s
    try:
        parsed = ast.literal_eval(parseable)
    except (ValueError, SyntaxError, MemoryError):
        # Truncation may have lopped a closing quote — retry with one
        # appended to recover a partial string repr.
        if parseable.startswith("'"):
            try:
                parsed = ast.literal_eval(parseable + "'")
            except (ValueError, SyntaxError, MemoryError):
                return s
        elif parseable.startswith('"'):
            try:
                parsed = ast.literal_eval(parseable + '"')
            except (ValueError, SyntaxError, MemoryError):
                return s
        else:
            return s
    if isinstance(parsed, str):
        return parsed + ("…" if truncated else "")
    if isinstance(parsed, list):
        return f"[{len(parsed)} items]"
    if isinstance(parsed, dict):
        # Show the most informative scalar value if there is one.
        for key in ("path", "file", "filename", "url", "name", "id", "message"):
            if key in parsed and isinstance(parsed[key], (str, int, float)):
                return str(parsed[key])
        return f"{{{len(parsed)} keys}}"
    return repr(parsed)


def _extras(kind: str, rec: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if kind == "hitl_checkpoint":
        out["checkpoint_id"] = rec.get("checkpoint_id")
        out["pattern"] = rec.get("pattern")
        out["decision"] = rec.get("decision") or rec.get("action")
        out["note"] = rec.get("note")
    elif kind == "reshape":
        out["change"] = rec.get("change")
        if rec.get("capability_kind"):
            out["capability_kind"] = rec.get("capability_kind")
        if rec.get("capability_name"):
            out["capability_name"] = rec.get("capability_name")
        if rec.get("template"):
            out["template"] = rec.get("template")
        if rec.get("parameters"):
            out["parameters"] = rec.get("parameters")
    return out


# ---------------------------------------------------------------------------
# Display-field projection — the four columns the trace panel renders.
# Defined here (server-side) so the JS doesn't re-parse audit shape (per
# DIALOGUE.md turn 6's display-fields architecture decision).
# ---------------------------------------------------------------------------


# Outcome previews that are visually trivial — we suppress them to satisfy
# the user's "narrate the run" criterion: showing `→ None` adds zero
# information and just consumes attention.
_TRIVIAL_OUTCOMES: frozenset[str] = frozenset({"None", "True", "False", "{}"})


def _outcome_short(preview: str, ok: bool | None) -> str:
    """Collapse an outcome preview to a single short status phrase.

    Empty-but-successful → ``"ok"``. Empty-but-failed → ``"failed"``.
    Trivial scalar repr (``None``/``True``/``False``) → ``""`` so the
    row stays quiet. Anything else returns the preview unchanged
    (already truncated by ``_outcome_preview``).
    """
    if preview and preview not in _TRIVIAL_OUTCOMES:
        return preview
    if ok is True:
        return "ok"
    if ok is False:
        return "failed"
    return ""


def _outcome_terse(preview: str, ok: bool | None) -> str:
    """Like ``_outcome_short`` but drops the ``"ok"`` fallback. Used for
    plain tool calls where success is implied by the absence of an
    error chip — spelling it out adds visual weight without information
    (a row of all-``ok``s makes failures harder to spot, not easier).
    """
    if preview and preview not in _TRIVIAL_OUTCOMES:
        return preview
    if ok is False:
        return "failed"
    return ""


def _action_subject_status(
    *,
    kind: str,
    tool: str,
    args: dict[str, Any],
    outcome: dict | None,
    rec: dict[str, Any],
    arg_preview: str,
    outcome_preview: str,
) -> tuple[str, str, str, str]:
    """Project an audit record into the (action, subject, status, category)
    tuple the trace panel renders as columns.

    Each branch keeps the row's *grammar* compact:
      ``<agent>  <action>  <subject>  <status>``
    See DIALOGUE.md turn 9 for the full table of disclosure layers.
    """
    ok = outcome.get("ok") if outcome else None

    if kind == "agent_turn":
        # LLM round-trip. Subject is the model name; status is a tiny
        # snippet of the LLM's reply so an operator scrolling a visible
        # turn list can still see what the agent was thinking.
        model = (args.get("model") or "llm") if isinstance(args, dict) else "llm"
        # Strip the provider prefix (e.g. ``anthropic/claude-haiku-4-5``).
        model_short = model.split("/", 1)[-1]
        return ("turn", model_short, outcome_preview, "turn")

    if kind == "policy_refusal":
        err = (outcome or {}).get("error", "") or ""
        return ("refused", tool or "", _truncate(err, 64), "refused")

    if kind == "tool_error":
        err = (outcome or {}).get("error", "") or ""
        return ("error", tool or "", _truncate(err, 64), "error")

    if kind == "reshape":
        change = rec.get("change") or ""
        if change == "instantiate_template":
            tpl = rec.get("template", "") or ""
            params = rec.get("parameters") or {}
            head = next(iter(params.values()), "") if params else ""
            subject = f"{tpl}({head})" if (tpl and head) else tpl
            return ("instantiate", subject, "ok", "reshape")
        if change == "acquire_capability":
            ck = rec.get("capability_kind", "") or ""
            cn = rec.get("capability_name", "") or ""
            return ("grant", f"{ck} {cn}".strip(), "granted", "reshape")
        return (change or "reshape", "", "", "reshape")

    if kind == "orchestrator_decision":
        decision = str(rec.get("decision") or "decision")
        reason = _truncate(str(rec.get("reason") or ""), 64)
        return ("orchestrator", decision, reason, "reshape")

    if kind == "hitl_checkpoint":
        cp = rec.get("checkpoint_id") or ""
        decision = (rec.get("decision") or rec.get("action") or "").strip()
        # Synthetic mandatory-R4 checkpoint — give it a human label.
        cp_label = "R4 mandatory" if cp.startswith("_r4") else cp
        return ("HITL", cp_label, decision, "hitl")

    # Pseudo-tool calls
    if tool == "delegate":
        target = (args.get("target_agent") or "").strip()
        return ("delegate", target, _outcome_short(outcome_preview, ok), "delegate")
    if tool == "spawn_parallel":
        of_agent = (args.get("of_agent") or "").strip()
        n = len(args.get("tasks", [])) if isinstance(args.get("tasks"), list) else 0
        subject = f"{of_agent} ×{n}" if (of_agent and n) else of_agent
        # The spawn outcome is the list of child results — too long to
        # belong in the status column. Either of the children's content
        # is reachable via the children's own rows. Show only ``ok`` /
        # ``failed`` / count.
        if ok is True:
            status = f"{n} done" if n else "ok"
        elif ok is False:
            status = "failed"
        else:
            status = ""
        return ("spawn", subject, status, "spawn")
    if tool == "instantiate_template":
        tpl = (args.get("template") or "").strip()
        params = args.get("parameters") or {}
        head = next(iter(params.values()), "") if params else ""
        subject = f"{tpl}({head})" if (tpl and head) else tpl
        return ("instantiate", subject, _outcome_short(outcome_preview, ok), "reshape")
    if tool == "request_grant":
        ck = (args.get("kind") or "").strip()
        cn = (args.get("name") or "").strip()
        return ("grant", f"{ck} {cn}".strip(), _outcome_short(outcome_preview, ok), "reshape")
    if tool == "request_human_review":
        reason = _truncate(str(args.get("reason", "") or ""), 64)
        return ("ask human", reason, "acknowledged", "hitl")

    # Plain MCP / HTTP tool call. Use the terser outcome helper here:
    # successful tool calls don't need a literal "ok" cluttering every
    # row — failures still surface visibly via "failed" / error text.
    return (
        tool or "tool",
        arg_preview,
        _outcome_terse(outcome_preview, ok),
        "tool",
    )


def _target_nodes(
    *,
    caller: str,
    tool: str,
    kind: str,
    args: dict[str, Any],
    rec: dict[str, Any],
    template_set: set[str],
) -> list[str]:
    """Topology node ids the trace-row selection should highlight.

    Always includes the caller's blueprint node. Adds the action's
    target (tool / agent / template / HITL gate) where the audit shape
    makes it identifiable.
    """
    out: list[str] = []
    if caller:
        # Templates have ``template:<name>`` ids; declared agents have
        # ``agent:<name>``. Resolve based on what the GraphModel has.
        if caller in template_set:
            out.append(f"template:{caller}")
        else:
            out.append(f"agent:{caller}")

    if kind == "agent_turn":
        # An agent's LLM round-trip has no topology counterpart — there
        # is no ``tool:llm`` node in the graph. Caller alone is enough.
        return out

    if kind == "hitl_checkpoint":
        cp_id = rec.get("checkpoint_id")
        if cp_id and not str(cp_id).startswith("_"):
            # ``_r4_mandatory`` is synthetic and has no model node; skip.
            out.append(f"hitl:{cp_id}")
        # Also light up the gated tool when known.
        if tool and tool not in _PSEUDO_TOOLS and tool != "llm":
            out.append(f"tool:{tool}")
        return out

    if tool == "delegate":
        target = args.get("target_agent")
        if target:
            out.append(f"template:{target}" if target in template_set else f"agent:{target}")
    elif tool == "spawn_parallel":
        of_agent = args.get("of_agent")
        if of_agent:
            out.append(
                f"template:{of_agent}" if of_agent in template_set else f"agent:{of_agent}"
            )
    elif tool == "instantiate_template":
        tpl = args.get("template")
        if tpl:
            out.append(f"template:{tpl}")
    elif tool == "request_grant":
        kind_ = args.get("kind")
        name = args.get("name")
        if name:
            if kind_ == "tool":
                out.append(f"tool:{name}")
            else:
                out.append(
                    f"template:{name}" if name in template_set else f"agent:{name}"
                )
    elif tool == "request_human_review":
        # No specific node — surfaces only in the trace itself.
        pass
    elif tool:
        # Regular MCP / HTTP tool call.
        out.append(f"tool:{tool}")

    return out


# ---------------------------------------------------------------------------
# Tiny formatting helpers (kept here to avoid pulling in render.py — that
# module owns the sidebar's text shape, this one owns the trace's).
# ---------------------------------------------------------------------------


_WS = re.compile(r"\s+")


def _truncate(s: str, n: int) -> str:
    s = _WS.sub(" ", s).strip()
    if len(s) <= n:
        return s
    return s[: max(1, n - 1)] + "…"


def _format_kv(key: str, value: Any, max_len: int = 36) -> str:
    if isinstance(value, str):
        return f'{key}="{_truncate(value, max_len)}"'
    if isinstance(value, (list, tuple)):
        return f"{key}=[{len(value)}]"
    if isinstance(value, dict):
        return f"{key}={{{len(value)}}}"
    return f"{key}={_truncate(str(value), max_len)}"
