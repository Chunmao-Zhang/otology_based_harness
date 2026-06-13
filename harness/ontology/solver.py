"""Solver result helpers for the ontology harness.

Solving is performed by the `workspace_solver` LLM subagent: it writes
``src/solve.py`` against the confirmed schema and the workspace data, executes it
with ``execute_code`` and writes ``intermediate/solver_result.json``. This module
only reads back that result. It contains no domain-specific query logic.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def read_solver_result(run_dir: str | Path) -> dict[str, Any]:
    """Read the solver_result.json an agent wrote, or an empty result."""
    path = Path(run_dir) / "intermediate" / "solver_result.json"
    if not path.exists():
        return {"ok": False, "error": "solver_result.json not found"}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"failed to read solver_result.json: {exc}"}
