"""Write evidence manifests for ontology runs."""

from __future__ import annotations

import json
from typing import Any

from langchain_core.tools import tool

from otology_agent_workspace.tools.path_utils import normalize_output_path


@tool
def evidence_manifest_writer(sources_json: str, output_path: str, needs_web_search: bool = False, handler: str = "schema_builder") -> str:
    """Write an evidence manifest JSON file.

    Args:
        sources_json: JSON string containing a list of sources, or an object with a sources field.
        output_path: Manifest output path.
        needs_web_search: Whether the next step needs web search.
        handler: Suggested next handler, default schema_builder.
    """
    try:
        parsed = json.loads(sources_json)
        sources = parsed.get("sources", parsed) if isinstance(parsed, dict) else parsed
        if not isinstance(sources, list):
            return json.dumps({"ok": False, "error": "sources_json must contain a list"}, ensure_ascii=False)
        path = normalize_output_path(output_path, "intermediate/evidence_manifest.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        manifest = {
            "sources": sources,
            "needs_web_search": bool(needs_web_search),
            "handler": handler,
        }
        path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        return json.dumps({"ok": True, "evidence_manifest_path": str(path), **manifest}, ensure_ascii=False)
    except Exception as exc:
        return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)
