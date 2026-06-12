"""Deterministic schema builder used by contract tests and fixtures."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from harness.ontology.schema_utils import parse_schema


COMPANY_SCHEMA = '''from typing import List, Optional


class Company:  # entity_type: Organization
    _id: str
    name: str
    country: Optional[str]
    operates_in_industry: List["Industry"]


class Industry:  # entity_type: BusinessDomain
    _id: str
    name: str
    operates_in_industry_r: List["Company"]  # reverse
'''


def build_draft_schema(question: str, evidence_manifest_path: str | Path, output_path: str | Path) -> dict[str, Any]:
    """Build a small ontology schema for the company analytics fixture."""
    manifest = {}
    manifest_path = Path(evidence_manifest_path)
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    schema_text = _select_schema(question, manifest)
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(schema_text, encoding="utf-8")

    parsed = parse_schema(schema_text)
    return {
        "schema_path": str(output),
        "valid": parsed.valid,
        "errors": parsed.errors,
    }


def _select_schema(question: str, manifest: dict[str, Any]) -> str:
    text = question.lower() + " " + json.dumps(manifest, ensure_ascii=False).lower()
    if any(token in text for token in ["company", "公司", "analytics", "分析"]):
        return COMPANY_SCHEMA
    return COMPANY_SCHEMA
