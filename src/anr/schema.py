"""Emit the canonical JSON Schema for the application specification.

The JSON Schema is the portable contract. This module (and the
schema JSON it emits) is how a runtime implemented in a different
language — or a different Python runtime that does not use this codebase's
pydantic models — validates a YAML spec against the standard.

Usage:
    python -m anr.schema             # print schema to stdout
    python -m anr.schema > out.json  # write to a file
"""

from __future__ import annotations

import json
import sys

from .spec import Spec, _SUPPORTED_SPEC_VERSIONS


def json_schema() -> dict:
    schema = Spec.model_json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["title"] = "Agent-Native Application Specification"
    schema["description"] = (
        "Portable declarative contract for an agent-native application. "
        "Any conforming runtime consumes a YAML/JSON document that "
        "validates against this schema."
    )
    schema["x-supported-spec-versions"] = sorted(_SUPPORTED_SPEC_VERSIONS)
    return schema


def main() -> int:
    json.dump(json_schema(), sys.stdout, indent=2, sort_keys=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
