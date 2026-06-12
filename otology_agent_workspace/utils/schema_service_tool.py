"""Schema service tools."""

from __future__ import annotations

import json
from langchain_core.tools import tool

from harness.ontology.schema_service import confirm_schema, schema_to_form as _schema_to_form
from otology_agent_workspace.tools.path_utils import normalize_output_path, resolve_path


@tool
def schema_to_form(schema_path: str) -> str:
    """Render an ontology schema file into editable form JSON."""
    try:
        form = _schema_to_form(schema_path=resolve_path(schema_path))
        return json.dumps({"ok": True, "form": form}, ensure_ascii=False)
    except Exception as exc:
        return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)


@tool
def schema_confirm(draft_schema_path: str, confirmed_schema_path: str) -> str:
    """Validate and copy draft_schema.py to confirmed_schema.py."""
    try:
        confirmed = normalize_output_path(confirmed_schema_path, "concepts/confirmed_schema.py")
        result = confirm_schema(resolve_path(draft_schema_path), confirmed)
        return json.dumps({"ok": result.get("valid", False), **result}, ensure_ascii=False)
    except Exception as exc:
        return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)
