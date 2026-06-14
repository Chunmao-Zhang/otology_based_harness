"""Ontology backend operations exposed as agent-callable tools.

In the pure-LLM orchestration, there is no Python state machine that persists
schemas, derives CSVs, or builds the workspace between steps. Instead the LLM
subagents invoke these deterministic, domain-agnostic backend helpers directly
as tools:

- ``save_evidence_manifest`` (evidence_collector): write the evidence manifest
  from the collected sources + schema plan, merging persisted web evidence.
- ``save_schema`` (schema_builder): validate the schema text, persist
  ``draft_schema.py`` + ``confirmed_schema.py`` and build the workspace skeleton.
- ``get_schema_outline`` (data_extractor): return the confirmed schema's exact
  class/field names so instances.json uses them verbatim.
- ``build_dataset`` (data_extractor): validate the written instances.json against
  the confirmed schema and derive ``facts.csv`` / ``relations.csv`` / report.

These tools contain no question-specific or domain-specific logic; the
intelligence stays in the LLM agents. Each tool resolves the active run directory
from the ``HARNESS_RUN_DIR`` environment variable via ``path_utils``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from langchain_core.tools import tool

from harness.ontology.data_extractor import persist_extraction, schema_outline, validate_instances
from harness.ontology.schema_builder import write_draft_schema
from harness.ontology.schema_service import confirm_schema
from harness.ontology.workspace_builder import build_workspace
from otology_agent_workspace.utils.format_validators import INSTANCES_FORMAT_SPEC, SCHEMA_FORMAT_SPEC

from .path_utils import ontology_run_dir


def _coerce_json(value: Any) -> Any:
    """Accept a Python object or a JSON string and return the parsed object."""
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except ValueError:
            return None
    return None


def _vrun(run: Path) -> str:
    return f"/runs/ontology_workspace_runs/{run.name}"


def _web_evidence_sources(run: Path) -> list[dict[str, Any]]:
    """Build manifest source entries (with retrievable chunks) from persisted web evidence."""
    web_dir = run / "intermediate" / "web_evidence"
    sources: list[dict[str, Any]] = []
    if not web_dir.exists():
        return sources
    vrun = _vrun(run)
    for path in sorted(web_dir.glob("web_*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        source_id = record.get("source_id", path.stem)
        snippet = record.get("snippet", "") or ""
        title = record.get("title", "") or ""
        sources.append({
            "source_id": source_id,
            "source_kind": "web",
            "url": record.get("url", ""),
            "title": title,
            "evidence_path": f"{vrun}/intermediate/web_evidence/{path.name}",
            "reason": snippet[:220],
            "chunks": [{
                "chunk_id": f"{source_id}#0",
                "text": f"{title}. {snippet}".strip(),
            }],
        })
    return sources


def _merge_sources(*groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for group in groups:
        for source in group:
            if not isinstance(source, dict):
                continue
            source_id = str(source.get("source_id", "")).strip()
            if not source_id or source_id in seen:
                continue
            seen.add(source_id)
            merged.append(source)
    return merged


@tool
def save_evidence_manifest(
    sources: str = "",
    schema_plan: str = "",
    needs_web_search: bool = False,
    question: str = "",
) -> str:
    """Persist the evidence manifest for the current run.

    Call this once after you have assessed evidence and recorded your schema plan.
    The manifest is written to ``intermediate/evidence_manifest.json`` and merges
    your reported upload sources with the web evidence already persisted by
    ``web_search``. Later stages read it via ``evidence_retriever``.

    Args:
        sources: JSON list of source objects you collected (uploads and/or web),
            each like {"source_id": "...", "source_kind": "upload|web", ...}.
        schema_plan: JSON list of planned entities/relations, mirroring your
            ``write_todos`` ``[plan]`` items. Each item is either
            {"kind": "entity", "name": "...", "source_id": "...", "fields": [...]}
            or {"kind": "relation", "name": "...", "head": "...", "tail": "...", "source_id": "..."}.
        needs_web_search: Whether external web evidence was required.
        question: The confirmed problem statement (stored for downstream stages).
    """
    run = ontology_run_dir()
    (run / "intermediate").mkdir(parents=True, exist_ok=True)

    agent_sources = _coerce_json(sources) or []
    if not isinstance(agent_sources, list):
        agent_sources = []
    plan = _coerce_json(schema_plan) or []
    if not isinstance(plan, list):
        plan = []

    if not plan:
        return json.dumps(
            {"ok": False, "error": "schema_plan is empty; provide the planned entities and relations."},
            ensure_ascii=False,
        )

    web_sources = _web_evidence_sources(run)
    merged = _merge_sources(agent_sources, web_sources)
    needs = bool(needs_web_search) or any(s.get("source_kind") == "web" for s in merged)

    manifest = {
        "question": question,
        "sources": merged,
        "needs_web_search": needs,
        "handler": "schema_builder",
        "schema_plan": plan,
    }
    manifest_path = run / "intermediate" / "evidence_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    return json.dumps(
        {
            "ok": True,
            "manifest_path": f"{_vrun(run)}/intermediate/evidence_manifest.json",
            "source_count": len(merged),
            "schema_plan_size": len(plan),
        },
        ensure_ascii=False,
    )


@tool
def save_schema(schema_text: str) -> str:
    """Validate and persist the ontology schema for the current run.

    Pass the full Python schema source. The tool validates it, writes
    ``concepts/draft_schema.py`` and ``concepts/confirmed_schema.py``, builds the
    workspace skeleton, and returns the schema outline (exact class/field names).

    If validation fails, nothing is persisted and the errors are returned so you
    can repair the schema and call this tool again.

    Args:
        schema_text: The complete Pydantic-style schema source.
    """
    run = ontology_run_dir()
    concepts = run / "concepts"
    concepts.mkdir(parents=True, exist_ok=True)

    draft = concepts / "draft_schema.py"
    result = write_draft_schema(schema_text, draft)
    if not result.get("valid"):
        return json.dumps(
            {"ok": False, "valid": False, "errors": result.get("errors", []), "format": SCHEMA_FORMAT_SPEC},
            ensure_ascii=False,
        )

    confirmed = concepts / "confirmed_schema.py"
    conf = confirm_schema(draft, confirmed)
    if not conf.get("valid"):
        return json.dumps(
            {"ok": False, "valid": False, "errors": conf.get("errors", []), "format": SCHEMA_FORMAT_SPEC},
            ensure_ascii=False,
        )

    ws = build_workspace(run, confirmed)
    if not ws.get("ok"):
        return json.dumps(
            {"ok": False, "valid": False, "errors": ws.get("errors", [])},
            ensure_ascii=False,
        )

    outline = schema_outline(confirmed)
    vrun = _vrun(run)
    return json.dumps(
        {
            "ok": True,
            "valid": True,
            "errors": [],
            "draft_schema_path": f"{vrun}/concepts/draft_schema.py",
            "confirmed_schema_path": f"{vrun}/concepts/confirmed_schema.py",
            "schema_outline": outline,
        },
        ensure_ascii=False,
    )


@tool
def get_schema_outline() -> str:
    """Return the confirmed schema's exact entity classes and field names.

    Use this before writing instances.json so entity_type / attribute /
    relation_type names match the schema verbatim. Returns
    {"entity_types": [{"entity_type": "...", "entity_data_type": "...",
    "attributes": [{"attribute": "...", "attribute_data_type": "..."}]}],
    "relations": [{"head_entity_type": "...", "relation_type": "...",
    "tail_entity_type": "..."}]}.
    """
    run = ontology_run_dir()
    confirmed = run / "concepts" / "confirmed_schema.py"
    schema_path = confirmed if confirmed.exists() else run / "concepts" / "draft_schema.py"
    if not schema_path.exists():
        return json.dumps({"ok": False, "error": "no confirmed_schema.py found for this run."}, ensure_ascii=False)
    return json.dumps({"ok": True, "schema_outline": schema_outline(schema_path)}, ensure_ascii=False)


@tool
def build_dataset() -> str:
    """Validate the written instances.json and derive the dataset for the current run.

    Call this after writing ``data/instances.json`` (or ``data/instances_final.json``).
    The tool validates the instances against the confirmed schema and, if they
    conform, derives ``data/facts.csv``, ``data/relations.csv`` and
    ``intermediate/extraction_report.json``.

    If validation fails, nothing is derived and the mismatch is returned so you
    can rewrite ``data/instances_final.json`` with the exact schema class/field
    names and call this tool again.
    """
    run = ontology_run_dir()
    confirmed = run / "concepts" / "confirmed_schema.py"
    if not confirmed.exists():
        return json.dumps({"ok": False, "error": "confirmed_schema.py not found; build the schema first."}, ensure_ascii=False)

    data_dir = run / "data"
    final_path = data_dir / "instances_final.json"
    inst_path = final_path if final_path.exists() else data_dir / "instances.json"
    if not inst_path.exists():
        return json.dumps(
            {"ok": False, "error": "data/instances.json not found; write the instances first."},
            ensure_ascii=False,
        )

    try:
        instances = json.loads(inst_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return json.dumps({"ok": False, "error": f"instances.json is not valid JSON: {exc}"}, ensure_ascii=False)
    if not isinstance(instances, dict) or "entities" not in instances:
        return json.dumps(
            {
                "ok": False,
                "error": "instances.json must be an object with an `entities` list "
                "and a `relations` list.",
                "format": INSTANCES_FORMAT_SPEC,
            },
            ensure_ascii=False,
        )

    validation = validate_instances(instances, confirmed)
    if not validation.get("ok"):
        return json.dumps(
            {"ok": False, "validation": validation, "format": INSTANCES_FORMAT_SPEC},
            ensure_ascii=False,
        )

    result = persist_extraction(instances, confirmed, run)
    if not result.get("ok"):
        return json.dumps({"ok": False, "errors": result.get("errors", [])}, ensure_ascii=False)

    return json.dumps(
        {
            "ok": True,
            "validation": validation,
            "report": result.get("report", {}),
            "facts_path": f"{_vrun(run)}/data/facts.csv",
            "relations_path": f"{_vrun(run)}/data/relations.csv",
        },
        ensure_ascii=False,
    )
