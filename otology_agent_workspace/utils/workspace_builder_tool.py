"""Run workspace builder tool."""

from __future__ import annotations

import json
from langchain_core.tools import tool

from harness.ontology.workspace_builder import build_workspace
from otology_agent_workspace.tools.path_utils import normalize_output_path, resolve_path


@tool
def workspace_builder_tool(run_dir: str, schema_path: str, instances_path: str, facts_path: str, relations_path: str) -> str:
    """Build a solver-ready run workspace from schema and data files."""
    try:
        result = build_workspace(
            normalize_output_path(run_dir, ""),
            resolve_path(schema_path),
            resolve_path(instances_path),
            resolve_path(facts_path),
            resolve_path(relations_path),
        )
        return json.dumps(result, ensure_ascii=False)
    except Exception as exc:
        return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)
