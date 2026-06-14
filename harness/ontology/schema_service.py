"""Schema confirmation and form rendering service."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from harness.ontology.schema_utils import infer_relation_type, parse_schema, parsed_schema_to_dict, read_schema_text


def schema_to_form(schema_text: str | None = None, schema_path: str | Path | None = None) -> list[dict[str, Any]]:
    text = read_schema_text(schema_text=schema_text, schema_path=schema_path)
    parsed = parse_schema(text)
    if not parsed.valid:
        raise ValueError(json.dumps({"errors": parsed.errors}, ensure_ascii=False))

    form: list[dict[str, Any]] = []
    for class_info in parsed.classes:
        id_type = "str"
        attributes: list[dict[str, Any]] = []
        for item in class_info.fields:
            if item.name == "_id":
                id_type = item.value_type or "str"
                continue
            if item.kind in ("primitive", "optional_primitive"):
                attributes.append({
                    "name": item.name,
                    "value_type": item.value_type or "str",
                    "optional": item.kind == "optional_primitive",
                })
        form.append({
            "type": "entity",
            "name": class_info.name,
            "entity_type": class_info.entity_type,
            "value_type": class_info.value_type,
            "id_type": id_type,
            "attributes": attributes,
        })

    for class_info in parsed.classes:
        for item in class_info.fields:
            if item.kind == "relation" and not item.reverse:
                form.append({
                    "type": "relation",
                    "head_entity": class_info.name,
                    "relation": item.name,
                    "relation_type": infer_relation_type(item),
                    "tail_entity": item.target,
                })
    return form


def confirm_schema(draft_path: str | Path, confirmed_path: str | Path) -> dict[str, Any]:
    draft = Path(draft_path)
    confirmed = Path(confirmed_path)
    confirmed.parent.mkdir(parents=True, exist_ok=True)
    text = draft.read_text(encoding="utf-8")
    parsed = parse_schema(text)
    if not parsed.valid:
        return {"valid": False, "errors": parsed.errors, "confirmed_path": str(confirmed)}
    shutil.copyfile(draft, confirmed)
    return {
        "valid": True,
        "errors": [],
        "confirmed_path": str(confirmed),
        "form": schema_to_form(schema_text=text),
    }


def _entity_base_fields(entity: dict[str, Any]) -> list[str]:
    """Reconstruct an entity's `_id` + primitive attribute lines from the form.

    Preserving these is what stops a Schema Studio edit from silently dropping
    every attribute (which previously left each class as just `_id`/`name`).
    """
    id_type = entity.get("id_type") or "str"
    if id_type not in ("str", "int"):
        id_type = "str"
    lines = [f"    _id: {id_type}"]
    attributes = entity.get("attributes")
    if attributes is None:
        # Backward-compatible default for forms that carry no attribute detail.
        return [f"    _id: {id_type}", "    name: str"]
    for attr in attributes:
        aname = (attr.get("name") or "").strip()
        if not aname or aname == "_id":
            continue
        vtype = attr.get("value_type") or "str"
        if vtype not in ("str", "int", "float", "bool"):
            vtype = "str"
        if attr.get("optional"):
            lines.append(f"    {aname}: Optional[{vtype}]")
        else:
            lines.append(f"    {aname}: {vtype}")
    return lines


def generate_schema_from_form(form: list[dict[str, Any]], output_path: str | Path | None = None) -> str:
    entities = [item for item in form if item.get("type") == "entity"]
    relations = [item for item in form if item.get("type") == "relation"]

    fields_by_entity: dict[str, list[str]] = {item["name"]: _entity_base_fields(item) for item in entities}
    entity_types = {item["name"]: item.get("entity_type", item["name"]) for item in entities}

    for rel in relations:
        head = rel["head_entity"]
        tail = rel["tail_entity"]
        name = rel["relation"]
        rel_type = rel.get("relation_type", "many_to_many")
        if rel_type == "many_to_one":
            fields_by_entity.setdefault(head, ["    _id: str", "    name: str"]).append(f"    {name}: Optional[\"{tail}\"]")
        else:
            fields_by_entity.setdefault(head, ["    _id: str", "    name: str"]).append(f"    {name}: List[\"{tail}\"]")
        fields_by_entity.setdefault(tail, ["    _id: str", "    name: str"]).append(
            f"    {name}_r: List[\"{head}\"]  # reverse"
        )

    lines = ["from typing import List, Optional", ""]
    for entity in entities:
        name = entity["name"]
        lines.append(f"class {name}:  # entity_type: {entity_types[name]}")
        for field in dict.fromkeys(fields_by_entity.get(name, ["    _id: str", "    name: str"])):
            lines.append(field)
        lines.append("")

    schema_text = "\n".join(lines).rstrip() + "\n"
    if output_path:
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(schema_text, encoding="utf-8")
    return schema_text


def schema_summary(schema_text: str | None = None, schema_path: str | Path | None = None) -> dict[str, Any]:
    text = read_schema_text(schema_text=schema_text, schema_path=schema_path)
    return parsed_schema_to_dict(parse_schema(text))
