"""Deterministic schema judging helpers for contract tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from harness.ontology.schema_utils import parse_schema, read_schema_text


def judge_schema(question: str, schema_text: str | None = None, schema_path: str | Path | None = None) -> dict[str, Any]:
    text = read_schema_text(schema_text=schema_text, schema_path=schema_path)
    parsed = parse_schema(text)
    if not parsed.valid:
        return {
            "answerable": False,
            "coverage_score": 0.0,
            "missing_requirements": parsed.errors,
            "recommended_action": "fix_schema",
        }

    class_map = {item.name: item for item in parsed.classes}
    missing: list[str] = []

    company = class_map.get("Company")
    if company is None:
        missing.append("缺少 Company 实体，无法表达答案对象")
    else:
        field_names = {field.name for field in company.fields}
        relation_targets = {field.target for field in company.fields if field.kind == "relation"}
        if _question_needs_country(question) and "country" not in field_names and "Country" not in relation_targets:
            missing.append("Company 缺少 country 字段或 Country 关系，无法过滤美国公司")
        if _question_needs_industry(question) and "industry" not in field_names and "Industry" not in relation_targets:
            missing.append("Company 缺少 industry 字段或 Industry 关系，无法表达数据分析领域")

    score = 1.0 if not missing else max(0.1, 1.0 - 0.3 * len(missing))
    return {
        "answerable": not missing,
        "coverage_score": round(score, 2),
        "missing_requirements": missing,
        "recommended_action": "confirm_schema" if not missing else "patch_schema",
    }


def _question_needs_country(question: str) -> bool:
    return any(token in question.lower() for token in ["美国", "united states", " u.s.", " us "])


def _question_needs_industry(question: str) -> bool:
    return any(token in question.lower() for token in ["数据分析", "analytics", "data analysis", "行业", "industry"])
