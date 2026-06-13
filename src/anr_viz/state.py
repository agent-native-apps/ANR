"""Live runtime state derived from tailing the mesh audit log.

The LiveState is the visualizer's only source of truth about what's
happening at runtime. It is decoupled from the runtime: we reopen the
audit log on every poll, seek to the last offset we read, and
incorporate any new lines. The runtime never sees the visualizer.

State model:
  * latest event + a bounded ring of recent events
  * which declared caller is currently active (the caller of the
    most-recent event that touched a regular tool or the mesh)
  * live instance IDs seen in the recent ring, grouped by blueprint
  * granted capabilities, keyed by instance_id (from reshape events)
  * the most recent totals snapshot (tool_calls / llm_calls / cost)
  * ephemeral edge highlights: which (src, dst) pairs fired in the
    last polling window, for the animation layer to draw

Why a ring and not the full log: we want the dashboard to feel
"current." A long run of 100s of events would otherwise cause every
nodeto be perpetually "active" in the highlight animation.
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

# Size of the rolling event ring and the "recent edge fires" window.
RECENT_EVENTS = 40
EDGE_HIGHLIGHT_SEC = 1.5


@dataclass
class AuditEvent:
    ts: float
    run_id: str
    kind: str
    caller: str
    instance_id: str | None
    parent: str | None
    parent_instance_id: str | None
    tool: str
    outcome_ok: bool | None
    checkpoint_id: str | None
    pattern: str | None
    change: str | None
    raw: dict[str, Any]


@dataclass
class LiveInstance:
    instance_id: str
    blueprint: str
    parent_instance_id: str | None
    last_seen: float
    granted_capabilities: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class LiveState:
    recent_events: deque[AuditEvent] = field(
        default_factory=lambda: deque(maxlen=RECENT_EVENTS)
    )
    instances: dict[str, LiveInstance] = field(default_factory=dict)
    totals: dict[str, Any] = field(default_factory=dict)
    latest_run_id: str = ""
    # (src_node_id, dst_node_id, fired_at, label, edge_kind_hint) tuples
    # for animation pulses. label is the verb to render on the firing
    # edge; edge_kind_hint constrains which model edges should highlight
    # so a "delegate" event lights only the delegation_edge (not the
    # parallel reshape_spawn that shares the same src/dst pair).
    edge_fires: deque[tuple[str, str, float, str, str]] = field(
        default_factory=lambda: deque(maxlen=64)
    )
    # Timestamp used as "now" reference (wall-clock for live, event-ts for
    # replay). Renderers pass this into recent_edge_fires so frozen views
    # show the pulses that were firing at the cursor moment.
    view_now: float = field(default_factory=time.time)

    @property
    def active_caller(self) -> str | None:
        if not self.recent_events:
            return None
        return self.recent_events[-1].caller

    @property
    def active_instance_id(self) -> str | None:
        if not self.recent_events:
            return None
        return self.recent_events[-1].instance_id

    def instances_of(self, blueprint: str) -> list[LiveInstance]:
        return [i for i in self.instances.values() if i.blueprint == blueprint]

    def recent_edge_fires(
        self, now: float | None = None
    ) -> list[tuple[str, str, str, str]]:
        """Return (src, dst, label, edge_kind_hint) tuples within the
        animation window."""
        now = now if now is not None else self.view_now
        return [
            (s, d, lbl, kh)
            for (s, d, ts, lbl, kh) in self.edge_fires
            if now - ts < EDGE_HIGHLIGHT_SEC
        ]


# ---------------------------------------------------------------------------


class AuditTailer:
    """Reads the audit log incrementally, keyed by byte offset.

    On each `poll()` we stat the file, seek to the last offset, read any
    new lines, parse them into AuditEvents, and apply them to the given
    LiveState. Cheap enough to run every few hundred ms.
    """

    def __init__(self, audit_path: Path, state: LiveState) -> None:
        self.audit_path = audit_path
        self.state = state
        self._offset = 0
        # FastAPI sync handlers run in a thread pool, so several
        # /state/* endpoints can call poll() concurrently. Without this
        # lock they can each see the old offset, read the same lines,
        # and double-append to records.
        self._lock = threading.Lock()
        # Raw audit records accumulated since the last reset. Kept in the
        # tailer (not LiveState) so that replaying to a frozen cursor
        # position never mutates the ever-growing log.
        self.records: list[dict[str, Any]] = []

    def _reset_state(self) -> None:
        """Drop all in-memory state — used on detected truncation or
        cross-run boundary. Caller holds ``self._lock``.
        """
        self._offset = 0
        self.state.recent_events.clear()
        self.state.instances.clear()
        self.state.edge_fires.clear()
        self.state.totals = {}
        self.state.latest_run_id = ""
        self.records.clear()

    def poll(self) -> int:
        """Incorporate any new audit lines. Returns the count added."""
        if not self.audit_path.is_file():
            return 0
        with self._lock:
            size = self.audit_path.stat().st_size
            if size < self._offset:
                # File was truncated (e.g. fresh run started). Reset.
                self._reset_state()
            if size == self._offset:
                return 0
            added = 0
            cross_run = False
            with self.audit_path.open("r", encoding="utf-8") as f:
                f.seek(self._offset)
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    # Cross-run detection: every AuditLog instance gets
                    # a fresh run_id at startup. If a new line arrives
                    # with a different run_id than the one we've been
                    # tailing, the previous run's records are stale —
                    # reset and re-read from the top. Catches the
                    # truncation window where size==0 was never observed.
                    new_run_id = rec.get("run_id")
                    if (
                        new_run_id
                        and self.state.latest_run_id
                        and new_run_id != self.state.latest_run_id
                    ):
                        cross_run = True
                        break
                    self.records.append(rec)
                    apply_record(self.state, rec)
                    added += 1
                if not cross_run:
                    self._offset = f.tell()
            if cross_run:
                # Reset and tail the whole file from scratch in one more
                # pass. (Releases + re-acquires the read; offset goes
                # back to 0.)
                self._reset_state()
                return self._read_from_zero()
            # Wall-clock GC: in live mode we fade instances that have been
            # silent for a while even if no new event has arrived.
            self.state.view_now = time.time()
            _gc_stale(self.state, self.state.view_now - 30.0)
            return added

    def _read_from_zero(self) -> int:
        """Read the whole audit file from offset 0. Caller holds the lock
        and has already cleared in-memory state."""
        added = 0
        with self.audit_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                self.records.append(rec)
                apply_record(self.state, rec)
                added += 1
            self._offset = f.tell()
        self.state.view_now = time.time()
        _gc_stale(self.state, self.state.view_now - 30.0)
        return added


def apply_record(state: LiveState, rec: dict[str, Any]) -> None:
    """Fold a single audit record into the live state.

    Pure: no wall-clock reads, no side effects beyond `state`. This makes
    it safe to replay from the start of `records` into a fresh LiveState
    when the viewer is parked on a historical cursor.
    """
    ev = _record_to_event(rec)
    state.recent_events.append(ev)
    state.latest_run_id = ev.run_id
    totals = rec.get("totals")
    if isinstance(totals, dict):
        state.totals = totals

    if ev.instance_id:
        inst = state.instances.get(ev.instance_id)
        if inst is None:
            inst = LiveInstance(
                instance_id=ev.instance_id,
                blueprint=ev.caller,
                parent_instance_id=ev.parent_instance_id,
                last_seen=ev.ts,
            )
            state.instances[ev.instance_id] = inst
        else:
            inst.last_seen = ev.ts

    if ev.kind == "reshape" and ev.change == "acquire_capability":
        inst = state.instances.get(ev.instance_id or "")
        if inst is not None:
            inst.granted_capabilities.append(
                {
                    "kind": rec.get("capability_kind"),
                    "name": rec.get("capability_name"),
                }
            )

    for src, dst, label, edge_kind_hint in _event_to_edges(ev, rec):
        state.edge_fires.append((src, dst, ev.ts, label, edge_kind_hint))

    state.view_now = ev.ts


def _gc_stale(state: LiveState, cutoff: float) -> None:
    stale = [iid for iid, inst in state.instances.items() if inst.last_seen < cutoff]
    for iid in stale:
        state.instances.pop(iid, None)


def build_state_at(records: list[dict[str, Any]], upto_idx: int) -> LiveState:
    """Replay `records[:upto_idx + 1]` into a fresh LiveState.

    Uses event-time GC (relative to the cursor's own timestamp) so the
    frozen view shows what was alive *then*, not what would be alive now.
    """
    st = LiveState()
    last_ts = 0.0
    for i, rec in enumerate(records):
        if i > upto_idx:
            break
        apply_record(st, rec)
        last_ts = float(rec.get("ts", last_ts))
    if last_ts:
        _gc_stale(st, last_ts - 30.0)
        st.view_now = last_ts
    return st


def _record_to_event(rec: dict[str, Any]) -> AuditEvent:
    outcome = rec.get("outcome")
    ok = outcome.get("ok") if isinstance(outcome, dict) else None
    return AuditEvent(
        ts=float(rec.get("ts", time.time())),
        run_id=str(rec.get("run_id", "")),
        kind=str(rec.get("kind", "")),
        caller=str(rec.get("caller", "")),
        instance_id=rec.get("instance_id") or None,
        parent=rec.get("parent") or None,
        parent_instance_id=rec.get("parent_instance_id") or None,
        tool=str(rec.get("tool", "")),
        outcome_ok=ok,
        checkpoint_id=rec.get("checkpoint_id") or None,
        pattern=rec.get("pattern") or None,
        change=rec.get("change") or None,
        raw=rec,
    )


def _event_to_edges(
    ev: AuditEvent, rec: dict[str, Any]
) -> Iterable[tuple[str, str, str, str]]:
    """Map an audit event to (src, dst, label, edge_kind_hint) tuples.

    `label` is the short verb shown on the firing edge.
    `edge_kind_hint` is the model edge kind that should light up — used
    to disambiguate when several edges share the same src/dst (e.g. a
    delegation_edge and a reshape_spawn between the same pair). If "*",
    any matching edge highlights.

    Node-id namespace matches anr_viz.model.Node.id:
      agent:<name>, template:<name>, tool:<name>, data:<name>, hitl:<id>
    """
    caller_node = f"agent:{ev.caller}"
    args = rec.get("args") or {}
    # Spawning pseudo-tools emit a tool_call_start before the child runs
    # and then a tool_call (and possibly reshape) completion afterwards,
    # all sharing a call_id. The start record carries the edge fire so
    # the animation plays in causal order; suppress the duplicates from
    # the paired completion records.
    if rec.get("call_id") and ev.kind in {"tool_call", "reshape"}:
        return
    if ev.kind == "hitl_checkpoint" and ev.checkpoint_id and ev.tool:
        yield (f"hitl:{ev.checkpoint_id}", f"tool:{ev.tool}", "hitl", "hitl_gate")
    elif ev.tool == "delegate":
        target = args.get("target_agent")
        if target:
            yield (caller_node, f"agent:{target}", "delegate", "delegation_edge")
    elif ev.tool == "spawn_parallel":
        of_agent = args.get("of_agent")
        n = len(args.get("tasks", [])) if isinstance(args.get("tasks"), list) else 0
        if of_agent:
            label = f"spawn ×{n}" if n else "spawn"
            yield (caller_node, f"agent:{of_agent}", label, "reshape_spawn")
    elif ev.tool == "instantiate_template":
        tpl = args.get("template")
        if tpl:
            yield (caller_node, f"template:{tpl}", "instantiate", "reshape_template")
    elif ev.tool == "request_grant":
        kind = args.get("kind")
        name = args.get("name")
        if name:
            dst_prefix = "tool:" if kind == "tool" else "agent:"
            yield (caller_node, f"{dst_prefix}{name}", "grant", "reshape_acquire")
    elif ev.tool == "request_human_review":
        # No structural edge — surfaces only in the sidebar.
        return
    elif ev.kind == "boundary_decision":
        outcome = ((rec.get("outcome") or {}).get("enforcement_outcome") or "boundary")
        yield (caller_node, f"tool:{ev.tool}", str(outcome), "tool_binding")
    elif ev.kind == "orchestrator_decision":
        decision = rec.get("decision") or "orchestrator"
        yield (caller_node, f"tool:{ev.tool}", str(decision), "tool_binding")
    elif ev.tool:
        yield (caller_node, f"tool:{ev.tool}", ev.tool, "tool_binding")
