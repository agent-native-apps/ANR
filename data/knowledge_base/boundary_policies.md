# Boundary policies

Boundary policies are ANR's compact prototype of the paper's mesh-level
ingress and egress enforcement.

A tool can be marked with:

```yaml
boundary: egress
```

or:

```yaml
boundary: ingress
```

Then the envelope can attach policies to that tool:

```yaml
envelope:
  boundary_policies:
    - id: block_supplier_sovereignty_leaks
      direction: egress
      tool: send_supplier_message
      content_arg: message
      action: block
      match:
        - "\\b(low stock|below reorder|sole source)\\b"
```

At runtime the mesh handles the two directions at different points:

- `egress` policies inspect the declared string argument before the tool
  call is realized.
- `ingress` policies inspect the returned tool payload before the result
  is handed back to the agent. Dict results can name a field such as
  `content`; string results are exposed as `content`; other values are
  exposed as `result`.

The enforcement outcome is one of:

- `allow` — the exchange proceeds unchanged.
- `block` — the exchange is refused and the agent receives an error.
- `sanitize` — matched content is replaced before the tool sees it.
- `escalate_to_human` — execution pauses for a HITL decision.

Every decision emits a `boundary_decision` audit record containing the
policy id, direction, match count, sanitized arguments when applicable,
and the enforcement outcome. This keeps the prototype aligned with the
paper's claim that the application boundary is enforced by runtime
infrastructure, not by agent self-restraint.
