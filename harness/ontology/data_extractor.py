"""Generic extraction persistence for the ontology harness.

The ``data_extractor`` LLM subagent decides *what* instances exist for the
confirmed schema and writes ``data/instances.json``. This module is the
domain-agnostic backend that validates those instances against the schema and
derives ``facts.csv`` / ``relations.csv`` and an extraction report.

``instances.json`` is a two-section object::

    {
      "entities": [
        {"entity_name": "Geoffrey Hinton", "entity_type": "Person",
         "attributes": {"name": "Geoffrey Hinton", "born_year": 1947},
         "source_refs": ["web_001"], "confidence": 0.95}
      ],
      "relations": [
        {"head_entity_name": "Geoffrey Hinton", "head_entity_type": "Person",
         "relation_type": "works_at",
         "tail_entity_name": "University of Toronto", "tail_entity_type": "Organization",
         "source_refs": ["web_004"], "confidence": 0.9}
      ]
    }

``(entity_name, entity_type)`` is the composite key relation endpoints point to.
``attribute_data_type`` is looked up from the schema (never stored on the
instance); relations carry only ``relation_type``.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from harness.ontology.schema_utils import (
    ClassInfo,
    attribute_fields,
    parse_schema,
    relation_fields,
)

_FACT_FIELDS = [
    "entity_name",
    "entity_type",
    "attribute",
    "value",
    "attribute_data_type",
    "source_refs",
    "confidence",
]
_RELATION_FIELDS = [
    "head_entity_name",
    "head_entity_type",
    "relation_type",
    "tail_entity_name",
    "tail_entity_type",
    "source_refs",
    "confidence",
]


def _class_index(classes: list[ClassInfo]) -> dict[str, ClassInfo]:
    return {c.name: c for c in classes}


def _attribute_types(cls: ClassInfo) -> dict[str, str]:
    return {f.name: (f.value_type or "str") for f in attribute_fields(cls)}


def _relation_targets(cls: ClassInfo) -> dict[str, str | None]:
    return {f.name: f.target for f in relation_fields(cls)}


def _as_source_refs(value: Any) -> str:
    if isinstance(value, list):
        return ";".join(str(item) for item in value)
    return str(value or "")


def schema_outline(schema_path: str | Path) -> dict[str, Any]:
    """Return the exact entity types / attributes / relations of the schema.

    Fed to the data_extractor so its instances.json uses the schema's real
    ``entity_type`` / ``attribute`` / ``relation_type`` names verbatim. Shape::

        {
          "entity_types": [
            {"entity_type": "Person", "entity_data_type": "str",
             "attributes": [{"attribute": "name", "attribute_data_type": "str"}]}
          ],
          "relations": [
            {"head_entity_type": "Person", "relation_type": "works_at",
             "tail_entity_type": "Organization"}
          ]
        }
    """
    parsed = parse_schema(Path(schema_path).read_text(encoding="utf-8"))
    entity_types: list[dict[str, Any]] = []
    relations: list[dict[str, Any]] = []
    for cls in parsed.classes:
        entity_types.append({
            "entity_type": cls.entity_type,
            "entity_data_type": cls.entity_data_type,
            "attributes": [
                {"attribute": f.name, "attribute_data_type": f.value_type or "str"}
                for f in attribute_fields(cls)
            ],
        })
        for f in relation_fields(cls):
            relations.append({
                "head_entity_type": cls.entity_type,
                "relation_type": f.name,
                "tail_entity_type": f.target,
            })
    return {"entity_types": entity_types, "relations": relations}


def _split_sections(instances: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not isinstance(instances, dict):
        return [], []
    entities = instances.get("entities")
    relations = instances.get("relations")
    entities = entities if isinstance(entities, list) else []
    relations = relations if isinstance(relations, list) else []
    return entities, relations


def _conforms(value: Any, data_type: str) -> bool:
    """Whether a value conforms to a declared attribute_data_type (lenient)."""
    if value is None:
        return True
    if data_type == "str":
        return isinstance(value, str)
    if data_type == "bool":
        if isinstance(value, bool):
            return True
        return isinstance(value, str) and value.strip().lower() in {"true", "false"}
    if data_type == "int":
        if isinstance(value, bool):
            return False
        if isinstance(value, int):
            return True
        if isinstance(value, float):
            return value.is_integer()
        if isinstance(value, str):
            return value.strip().lstrip("-").isdigit()
        return False
    if data_type == "float":
        if isinstance(value, bool):
            return False
        if isinstance(value, (int, float)):
            return True
        if isinstance(value, str):
            try:
                float(value)
                return True
            except ValueError:
                return False
        return False
    return True


def validate_instances(
    instances: Any,
    schema_path: str | Path,
) -> dict[str, Any]:
    """Validate the two-section instances object against the confirmed schema.

    Enforces the instance-level rules: declared types/attributes/relation_types,
    composite-key uniqueness, relation-endpoint existence, and attribute value
    conformance. Returns a structured ``{ok, valid, errors[], ...}`` report so the
    extractor can repair and re-run.
    """
    parsed = parse_schema(Path(schema_path).read_text(encoding="utf-8"))
    if not parsed.valid:
        return {"ok": False, "valid": False, "errors": parsed.errors}

    classes = _class_index(parsed.classes)
    errors: list[str] = []

    if not isinstance(instances, dict) or "entities" not in instances:
        return {
            "ok": False,
            "valid": False,
            "errors": [
                "instances.json must be an object with an `entities` list and a "
                "`relations` list."
            ],
        }

    entities, relations = _split_sections(instances)
    if not entities:
        errors.append("`entities` must be a non-empty list of entity objects.")

    # Composite-key uniqueness + per-entity attribute checks (rules 7, 8, 10).
    seen_keys: set[tuple[str, str]] = set()
    entity_keys: set[tuple[str, str]] = set()
    for idx, ent in enumerate(entities):
        if not isinstance(ent, dict):
            errors.append(f"entities[{idx}]: must be an object")
            continue
        name = ent.get("entity_name")
        etype = ent.get("entity_type")
        if not name or not isinstance(name, str):
            errors.append(f"entities[{idx}]: missing entity_name")
        if not etype or not isinstance(etype, str):
            errors.append(f"entities[{idx}]: missing entity_type")
            continue
        cls = classes.get(etype)
        if cls is None:
            errors.append(f"entities[{idx}]: unknown entity_type '{etype}' (not declared in schema)")
            continue
        key = (str(name), str(etype))
        if key in seen_keys:
            errors.append(f"duplicate (entity_name, entity_type): {key}")
        seen_keys.add(key)
        entity_keys.add(key)

        attrs = ent.get("attributes", {})
        if attrs and not isinstance(attrs, dict):
            errors.append(f"entities[{idx}] ({name}): `attributes` must be an object")
            continue
        declared = _attribute_types(cls)
        for attr, value in (attrs or {}).items():
            if attr not in declared:
                errors.append(
                    f"{etype} '{name}': attribute '{attr}' is not declared in the schema"
                )
                continue
            if not _conforms(value, declared[attr]):
                errors.append(
                    f"{etype} '{name}': attribute '{attr}'={value!r} is not a "
                    f"{declared[attr]} (attribute_data_type mismatch)"
                )

    # Relation checks (rules 7, 9).
    for idx, rel in enumerate(relations):
        if not isinstance(rel, dict):
            errors.append(f"relations[{idx}]: must be an object")
            continue
        h_name = rel.get("head_entity_name")
        h_type = rel.get("head_entity_type")
        rtype = rel.get("relation_type")
        t_name = rel.get("tail_entity_name")
        t_type = rel.get("tail_entity_type")
        if not all(isinstance(v, str) and v for v in (h_name, h_type, rtype, t_name, t_type)):
            errors.append(
                f"relations[{idx}]: needs head_entity_name/head_entity_type/"
                f"relation_type/tail_entity_name/tail_entity_type (all non-empty strings)"
            )
            continue
        head_cls = classes.get(h_type)
        if head_cls is None:
            errors.append(f"relations[{idx}]: unknown head_entity_type '{h_type}'")
            continue
        targets = _relation_targets(head_cls)
        if rtype not in targets:
            errors.append(
                f"relations[{idx}]: relation_type '{rtype}' is not declared on '{h_type}'"
            )
        elif targets[rtype] != t_type:
            errors.append(
                f"relations[{idx}]: relation_type '{rtype}' on '{h_type}' targets "
                f"'{targets[rtype]}', not '{t_type}'"
            )
        if (str(h_name), str(h_type)) not in entity_keys:
            errors.append(
                f"relations[{idx}]: head ({h_name}, {h_type}) is not in entities[]"
            )
        if (str(t_name), str(t_type)) not in entity_keys:
            errors.append(
                f"relations[{idx}]: tail ({t_name}, {t_type}) is not in entities[]"
            )

    return {
        "ok": not errors,
        "valid": not errors,
        "errors": errors,
        "schema_entity_types": list(classes.keys()),
        "entity_count": len(entities),
        "relation_count": len(relations),
    }


def persist_extraction(
    instances: Any,
    schema_path: str | Path,
    run_dir: str | Path,
) -> dict[str, Any]:
    """Write instances.json and derive facts/relations/report from any schema."""
    schema_text = Path(schema_path).read_text(encoding="utf-8")
    parsed = parse_schema(schema_text)
    if not parsed.valid:
        return {"ok": False, "errors": parsed.errors}

    classes = _class_index(parsed.classes)
    entities, relation_rows = _split_sections(instances)

    run = Path(run_dir)
    data_dir = run / "data"
    intermediate_dir = run / "intermediate"
    data_dir.mkdir(parents=True, exist_ok=True)
    intermediate_dir.mkdir(parents=True, exist_ok=True)

    facts: list[dict[str, Any]] = []
    relations: list[dict[str, Any]] = []
    confidences: list[float] = []
    entities_by_type: dict[str, int] = {}

    for ent in entities:
        if not isinstance(ent, dict):
            continue
        entity_name = str(ent.get("entity_name", ""))
        entity_type = str(ent.get("entity_type", ""))
        cls = classes.get(entity_type)
        if cls is None:
            continue
        entities_by_type[entity_type] = entities_by_type.get(entity_type, 0) + 1
        source_refs = _as_source_refs(ent.get("source_refs"))
        confidence = ent.get("confidence")
        if isinstance(confidence, (int, float)) and not isinstance(confidence, bool):
            confidences.append(float(confidence))

        declared = _attribute_types(cls)
        attrs = ent.get("attributes") or {}
        if isinstance(attrs, dict):
            for attr, value in attrs.items():
                if attr not in declared or value in (None, ""):
                    continue
                facts.append({
                    "entity_name": entity_name,
                    "entity_type": entity_type,
                    "attribute": attr,
                    "value": value,
                    "attribute_data_type": declared[attr],
                    "source_refs": source_refs,
                    "confidence": confidence if confidence is not None else "",
                })

    for rel in relation_rows:
        if not isinstance(rel, dict):
            continue
        confidence = rel.get("confidence")
        if isinstance(confidence, (int, float)) and not isinstance(confidence, bool):
            confidences.append(float(confidence))
        relations.append({
            "head_entity_name": str(rel.get("head_entity_name", "")),
            "head_entity_type": str(rel.get("head_entity_type", "")),
            "relation_type": str(rel.get("relation_type", "")),
            "tail_entity_name": str(rel.get("tail_entity_name", "")),
            "tail_entity_type": str(rel.get("tail_entity_type", "")),
            "source_refs": _as_source_refs(rel.get("source_refs")),
            "confidence": confidence if confidence is not None else "",
        })

    instances_path = data_dir / "instances.json"
    facts_path = data_dir / "facts.csv"
    relations_path = data_dir / "relations.csv"
    report_path = intermediate_dir / "extraction_report.json"

    normalized = {"entities": entities, "relations": relation_rows}
    instances_path.write_text(json.dumps(normalized, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_csv(facts_path, facts, _FACT_FIELDS)
    _write_csv(relations_path, relations, _RELATION_FIELDS)

    report = {
        "total_entities": len(entities),
        "total_relations": len(relations),
        "total_facts": len(facts),
        "entities_by_type": entities_by_type,
        "relation_types_used": sorted({row["relation_type"] for row in relations}),
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
