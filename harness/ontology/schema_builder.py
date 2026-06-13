"""Schema writing/validation backend for the ontology harness.

The intelligent part (deciding which entities, attributes and relations the
question needs) is produced by the `schema_builder` LLM subagent. This module is
the deterministic backend that persists the LLM-provided schema text and
validates it. It contains no question-specific or domain-specific schema.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from harness.ontology.schema_utils import parse_schema


def write_draft_schema(schema_text: str, output_path: str | Path) -> dict[str, Any]:
    """Persist an LLM-produced ontology schema and validate it.

    Args:
        schema_text: The Pydantic-style schema source produced by the schema
            builder agent.
        output_path: Where to write ``draft_schema.py``.
    """
    text = (schema_text or "").strip()
    if not text:
        return {"schema_path": str(output_path), "valid": False, "errors": ["schema_text is empty"]}

    parsed = parse_schema(text)
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")

    return {
        "schema_path": str(output),
        "valid": parsed.valid,
        "errors": parsed.errors,
    }
