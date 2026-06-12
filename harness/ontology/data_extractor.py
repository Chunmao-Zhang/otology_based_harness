"""Small deterministic data extraction helpers for fixture-backed validation."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from harness.ontology.schema_utils import parse_schema


def extract_company_csv(schema_path: str | Path, csv_path: str | Path, run_dir: str | Path) -> dict[str, Any]:
    """Extract a simple Company/Industry dataset from a CSV fixture."""
    schema_text = Path(schema_path).read_text(encoding="utf-8")
    parsed = parse_schema(schema_text)
    if not parsed.valid:
        return {"ok": False, "errors": parsed.errors}

    run = Path(run_dir)
    data_dir = run / "data"
    intermediate_dir = run / "intermediate"
    data_dir.mkdir(parents=True, exist_ok=True)
    intermediate_dir.mkdir(parents=True, exist_ok=True)

    rows = list(csv.DictReader(Path(csv_path).read_text(encoding="utf-8").splitlines()))
    instances: dict[str, list[dict[str, Any]]] = {"Company": [], "Industry": []}
    industry_seen: set[str] = set()
    facts: list[dict[str, Any]] = []
    relations: list[dict[str, Any]] = []

    for index, row in enumerate(rows, start=1):
        name = row.get("name") or row.get("company") or row.get("Company") or f"company_{index}"
        country = row.get("country", "")
        industry = row.get("industry", "")
        company_id = f"company:{_slug(name)}"
        industry_id = f"industry:{_slug(industry)}" if industry else ""
        source_ref = f"{Path(csv_path).name}#row_{index}"

        item = {
            "_id": company_id,
            "_concept": "Company",
            "name": name,
            "country": country,
            "source_refs": [source_ref],
            "confidence": 0.95,
        }
        if industry_id:
            item["operates_in_industry"] = [industry_id]
        instances["Company"].append(item)

        for attr in ["name", "country"]:
            if item.get(attr):
                facts.append({
                    "subject": company_id,
                    "concept": "Company",
                    "attribute": attr,
                    "value": item[attr],
                    "value_type": "str",
                    "source_refs": source_ref,
                    "confidence": "0.95",
                })

        if industry and industry_id not in industry_seen:
            industry_seen.add(industry_id)
            instances["Industry"].append({
                "_id": industry_id,
                "_concept": "Industry",
                "name": industry,
                "source_refs": [source_ref],
                "confidence": 0.95,
            })
        if industry_id:
            relations.append({
                "subject": company_id,
                "subject_concept": "Company",
                "relation": "operates_in_industry",
                "object": industry_id,
                "object_concept": "Industry",
                "source_refs": source_ref,
                "confidence": "0.95",
            })

    instances_path = data_dir / "instances.json"
    facts_path = data_dir / "facts.csv"
    relations_path = data_dir / "relations.csv"
    report_path = intermediate_dir / "extraction_report.json"

    instances_path.write_text(json.dumps(instances, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_csv(facts_path, facts, ["subject", "concept", "attribute", "value", "value_type", "source_refs", "confidence"])
    _write_csv(relations_path, relations, ["subject", "subject_concept", "relation", "object", "object_concept", "source_refs", "confidence"])

    report = {
        "total_instances": sum(len(items) for items in instances.values()),
        "total_facts": len(facts),
        "total_relations": len(relations),
        "entity_count": sum(len(items) for items in instances.values()),
        "relation_types_used": sorted({item["relation"] for item in relations}),
        "avg_confidence": 0.95 if rows else 0,
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    return {
        "ok": True,
        "instances_path": str(instances_path),
        "facts_path": str(facts_path),
        "relations_path": str(relations_path),
        "extraction_report_path": str(report_path),
    }


def _write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _slug(value: str) -> str:
    return "".join(ch.lower() if ch.isalnum() else "_" for ch in value).strip("_") or "unknown"
