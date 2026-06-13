"""Generic extraction persistence for the ontology harness.

The `data_extractor` LLM subagent decides *what* instances exist for the
confirmed schema and writes ``data/instances.json``. This module is the
domain-agnostic backend that, given any confirmed schema and an instances
collection, derives ``facts.csv`` and ``relations.csv`` and an extraction
report. It has no Company/Industry-specific logic.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from harness.ontology.schema_utils import ClassInfo, parse_schema

_RESERVED_KEYS = {"_id", "_concept", "source_refs", "confidence"}
_FACT_FIELDS = ["subject", "concept", "attribute", "value", "value_type", "source_refs", "confidence"]
_RELATION_FIELDS = ["subject", "subject_concept", "relation", "object", "object_concept", "source_refs", "confidence"]


def _class_index(classes: list[ClassInfo]) -> dict[str, ClassInfo]:
    return {c.name: c for c in classes}


_PRIMITIVE_KINDS = {"primitive", "optional_primitive"}


def _primitive_fields(cls: ClassInfo) -> dict[str, str]:
    return {
        f.name: (f.value_type or "str")
        for f in cls.fields
        if f.kind in _PRIMITIVE_KINDS and f.name != "_id"
    }


def _relation_fields(cls: ClassInfo) -> dict[str, dict[str, Any]]:
    return {
        f.name: {"target": f.target, "container": f.container}
        for f in cls.fields
        if f.kind == "relation" and not f.reverse
    }


def _as_source_refs(value: Any) -> str:
    if isinstance(value, list):
        return ";".join(str(item) for item in value)
    return str(value or "")


def schema_outline(schema_path: str | Path) -> list[dict[str, Any]]:
    """Return the exact entity classes / fields the extractor must populate.

    This is fed to the data_extractor so its instances.json uses the schema's
    real class names and field names verbatim instead of paraphrased keys.
    """
    parsed = parse_schema(Path(schema_path).read_text(encoding="utf-8"))
    outline: list[dict[str, Any]] = []
    for cls in parsed.classes:
        rels = _relation_fields(cls)
        outline.append({
            "concept": cls.name,
            "primitive_fields": sorted(_primitive_fields(cls).keys()),
            "relation_fields": [
                {"name": name, "target": meta.get("target")} for name, meta in rels.items()
            ],
        })
    return outline


def validate_instances(
    instances: dict[str, Any],
    schema_path: str | Path,
) -> dict[str, Any]:
    """Check instance keys/fields against the confirmed schema (no remapping)."""
    parsed = parse_schema(Path(schema_path).read_text(encoding="utf-8"))
    if not parsed.valid:
        return {"ok": False, "errors": parsed.errors}
    classes = _class_index(parsed.classes)

    unknown_concepts = [c for c in instances if c not in classes]
    populated = {c for c, items in instances.items() if isinstance(items, list) and items}
    missing_concepts = [name for name in classes if name not in populated]

    field_issues: list[str] = []
    for concept, items in instances.items():
        cls = classes.get(concept)
        if cls is None or not isinstance(items, list):
            continue
        allowed = set(_primitive_fields(cls)) | set(_relation_fields(cls)) | _RESERVED_KEYS
        for item in items:
            if not isinstance(item, dict):
                continue
            for key in item:
                if key not in allowed:
                    field_issues.append(f"{concept}.{key}")
    return {
        "ok": not unknown_concepts and not field_issues,
        "schema_concepts": list(classes.keys()),
        "unknown_concepts": unknown_concepts,
        "missing_concepts": missing_concepts,
        "unknown_fields": sorted(set(field_issues)),
    }


def persist_extraction(
    instances: dict[str, list[dict[str, Any]]],
    schema_path: str | Path,
    run_dir: str | Path,
) -> dict[str, Any]:
    """Write instances.json and derive facts/relations/report from any schema."""
    schema_text = Path(schema_path).read_text(encoding="utf-8")
    parsed = parse_schema(schema_text)
    if not parsed.valid:
        return {"ok": False, "errors": parsed.errors}

    classes = _class_index(parsed.classes)

    run = Path(run_dir)
    data_dir = run / "data"
    intermediate_dir = run / "intermediate"
    data_dir.mkdir(parents=True, exist_ok=True)
    intermediate_dir.mkdir(parents=True, exist_ok=True)

    facts: list[dict[str, Any]] = []
    relations: list[dict[str, Any]] = []
    confidences: list[float] = []

    for concept, items in instances.items():
        cls = classes.get(concept)
        if cls is None or not isinstance(items, list):
            continue
        primitives = _primitive_fields(cls)
        rels = _relation_fields(cls)
        for item in items:
            if not isinstance(item, dict):
                continue
            subject = str(item.get("_id", ""))
            source_refs = _as_source_refs(item.get("source_refs"))
            confidence = item.get("confidence")
            if isinstance(confidence, (int, float)):
                confidences.append(float(confidence))

            for attr, value_type in primitives.items():
                if attr not in item or item[attr] in (None, ""):
                    continue
                facts.append({
                    "subject": subject,
                    "concept": concept,
                    "attribute": attr,
                    "value": item[attr],
                    "value_type": value_type,
                    "source_refs": source_refs,
                    "confidence": confidence if confidence is not None else "",
                })

            for rel_name, meta in rels.items():
                if rel_name not in item or item[rel_name] in (None, "", []):
                    continue
                targets = item[rel_name]
                if not isinstance(targets, list):
                    targets = [targets]
                for target_id in targets:
                    relations.append({
                        "subject": subject,
                        "subject_concept": concept,
                        "relation": rel_name,
                        "object": str(target_id),
                        "object_concept": meta.get("target") or "",
                        "source_refs": source_refs,
                        "confidence": confidence if confidence is not None else "",
                    })

    instances_path = data_dir / "instances.json"
    facts_path = data_dir / "facts.csv"
    relations_path = data_dir / "relations.csv"
    report_path = intermediate_dir / "extraction_report.json"

    instances_path.write_text(json.dumps(instances, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_csv(facts_path, facts, _FACT_FIELDS)
    _write_csv(relations_path, relations, _RELATION_FIELDS)

    total_instances = sum(len(items) for items in instances.values() if isinstance(items, list))
    report = {
        "total_instances": total_instances,
        "total_facts": len(facts),
        "total_relations": len(relations),
        "instances_by_concept": {
            concept: len(items) for concept, items in instances.items() if isinstance(items, list)
        },
        "relation_types_used": sorted({row["relation"] for row in relations}),
        "avg_confidence": round(sum(confidences) / len(confidences), 3) if confidences else None,
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    return {
        "ok": True,
        "instances_path": str(instances_path),
        "facts_path": str(facts_path),
        "relations_path": str(relations_path),
        "extraction_report_path": str(report_path),
        "report": report,
    }


def _write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
