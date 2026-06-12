"""Validate ontology schema text or files."""

from __future__ import annotations

import json

from langchain_core.tools import tool

from harness.ontology.schema_utils import parse_schema, parsed_schema_to_dict
from .path_utils import resolve_path


@tool
def schema_validator(schema_text: str = "", schema_path: str = "") -> str:
    """Validate an ontology schema and return structured JSON."""
    try:
        if schema_text:
            text = schema_text
        elif schema_path:
            text = resolve_path(schema_path).read_text(encoding="utf-8")
        else:
            return json.dumps({"valid": False, "errors": ["schema_text or schema_path is required"]}, ensure_ascii=False)
        return json.dumps(parsed_schema_to_dict(parse_schema(text)), ensure_ascii=False)
    except Exception as exc:
        return json.dumps({"valid": False, "errors": [str(exc)]}, ensure_ascii=False)
