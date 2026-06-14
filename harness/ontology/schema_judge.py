"""Mechanical schema checks supporting the schema_judger agent.

Answerability is decided semantically by the `schema_judger` LLM subagent. This
module only provides a domain-agnostic mechanical check (does the schema parse,
how many entities/relations does it define) that the agent and the backend can
use as a guardrail. It contains no question-specific heuristics.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from harness.ontology.schema_utils import parse_schema, read_schema_text, relation_fields


def mechanical_schema_check(
    schema_text: str | None = None,
    schema_path: str | Path | None = None,
) -> dict[str, Any]:
    """Return a domain-agnostic structural report for a schema."""
    text = read_schema_text(schema_text=schema_text, schema_path=schema_path)
    parsed = parse_schema(text)
    entity_names = [c.name for c in parsed.classes]
    relation_count = sum(len(relation_fields(c)) for c in parsed.classes)
    return {
        "valid": parsed.valid,
        "errors": parsed.errors,
        "entities": entity_names,
        "entity_count": len(entity_names),
        "relation_count": relation_count,
    }
