"""Draft schema builder tool."""

from __future__ import annotations

import json

from langchain_core.tools import tool

from harness.ontology.schema_builder import build_draft_schema
from otology_agent_workspace.tools.path_utils import normalize_output_path, resolve_path


@tool
def schema_draft_builder(question: str, evidence_manifest_path: str, output_path: str = "") -> str:
    """Build a validated draft ontology schema at the canonical run path."""
    try:
        output = normalize_output_path(output_path, "concepts/draft_schema.py")
        result = build_draft_schema(question, resolve_path(evidence_manifest_path), output)
        return json.dumps({"ok": result.get("valid", False), **result}, ensure_ascii=False)
    except Exception as exc:
        return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)
