"""Load a YAML specification into a validated Spec object."""

from __future__ import annotations

from pathlib import Path

import yaml

from .spec import Spec


def load_spec(path: str | Path) -> Spec:
    path = Path(path)
    with path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    return Spec.model_validate(raw)
