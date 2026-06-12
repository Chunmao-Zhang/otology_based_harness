"""Fixture-backed data extraction tool."""

from __future__ import annotations

import json
from langchain_core.tools import tool

from harness.ontology.data_extractor import extract_company_csv
from otology_agent_workspace.tools.path_utils import normalize_output_path, resolve_path


@tool
def data_extract_company_csv(schema_path: str, csv_path: str, run_dir: str) -> str:
    """Extract Company/Industry instances, facts, and relations from a CSV file."""
    try:
        run = normalize_output_path(run_dir, "")
        result = extract_company_csv(resolve_path(schema_path), resolve_path(csv_path), run)
        return json.dumps(result, ensure_ascii=False)
    except Exception as exc:
        return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)
