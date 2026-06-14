"""Validate ontology schema text or files."""

from __future__ import annotations

import json

from langchain_core.tools import tool

from harness.ontology.schema_utils import parse_schema, parsed_schema_to_dict
from otology_agent_workspace.utils.format_validators import SCHEMA_FORMAT_SPEC
from .path_utils import resolve_path


@tool
def schema_validator(schema_text: str = "", schema_path: str = "") -> str:
    """Validate an ontology schema and return structured JSON.

    On failure the result includes a ``format`` field restating the required
    schema format so the schema_builder can repair its output and resend.
    """
    try:
        if schema_text:
            text = schema_text
        elif schema_path:
            text = resolve_path(schema_path).read_text(encoding="utf-8")
        else:
            return json.dumps(
                {"valid": False, "errors": ["schema_text or schema_path is required"], "format": SCHEMA_FORMAT_SPEC},
                ensure_ascii=False,
            )
        result = parsed_schema_to_dict(parse_schema(text))
        if not result.get("valid"):
            result["format"] = SCHEMA_FORMAT_SPEC
        return json.dumps(result, ensure_ascii=False)
    except Exception as exc:
        return json.dumps({"valid": False, "errors": [str(exc)], "format": SCHEMA_FORMAT_SPEC}, ensure_ascii=False)
