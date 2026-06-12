"""Workspace solver tool that writes and executes code inside the run workspace."""

from __future__ import annotations

import json

from langchain_core.tools import tool

from harness.ontology.solver import solve_company_workspace
from otology_agent_workspace.tools.path_utils import resolve_path


@tool
def workspace_solver_tool(question: str, workspace_dir: str) -> str:
    """Solve the final question by writing and running src/solve.py in workspace_dir."""
    try:
        result = solve_company_workspace(question, resolve_path(workspace_dir))
        return json.dumps(result, ensure_ascii=False)
    except Exception as exc:
        return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)
