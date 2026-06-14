"""Schema confirmation and form rendering service.

The editable form mirrors the two glossary tables:

- Entity Definitions: ``entity_type`` | ``entity_data_type`` | ``attribute`` |
  ``attribute_data_type``
- Relation Schema: ``head_entity_type`` | ``relation_type`` | ``tail_entity_type``

There is no cardinality column and relations carry no data type.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from harness.ontology.schema_utils import (
    attribute_fields,
    parse_schema,
    parsed_schema_to_dict,
    read_schema_text,
    relation_fields,
)

_ID_TYPES = {"str", "int"}
_ATTR_TYPES = {"str", "int", "float", "bool"}


def schema_to_form(schema_text: str | None = None, schema_path: str | Path | None = None) -> list[dict[str, Any]]:
    text = read_schema_text(schema_text=schema_text, schema_path=schema_path)
    parsed = parse_schema(text)
    if not parsed.valid:
        raise ValueError(json.dumps({"errors": parsed.errors}, ensure_ascii=False))

    form: list[dict[str, Any]] = []
    for class_info in parsed.classes:
        attributes = [
            {
                "attribute": item.name,
                "attribute_data_type": item.value_type or "str",
                "optional": item.kind == "optional_primitive" or item.optional,
            }
            for item in attribute_fields(class_info)
        ]
        form.append({
            "type": "entity",
            "name": class_info.entity_type,
            "entity_type": class_info.entity_type,
            "entity_data_type": class_info.entity_data_type,
            "attributes": attributes,
        })

    for class_info in parsed.classes:
        for item in relation_fields(class_info):
            form.append({
                "type": "relation",
                "head_entity_type": class_info.entity_type,
                "relation_type": item.name,
                "tail_entity_type": item.target,
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


def _entity_type_of(entity: dict[str, Any]) -> str:
    return str(entity.get("entity_type") or entity.get("name") or "").strip()


def _entity_data_type_of(entity: dict[str, Any]) -> str:
    value = entity.get("entity_data_type") or entity.get("id_type") or "str"
    return value if value in _ID_TYPES else "str"


def _entity_base_fields(entity: dict[str, Any]) -> list[str]:
    """Reconstruct an entity's `_id` + primitive attribute lines from the form.

    Preserving these is what stops a Schema Studio edit from silently dropping
    every attribute (which previously left each class as just `_id`/`name`).
    """
    lines = [f"    _id: {_entity_data_type_of(entity)}"]
    attributes = entity.get("attributes")
    if attributes is None:
        # Backward-compatible default for forms that carry no attribute detail.
        lines.append("    name: str")
        return lines
    for attr in attributes:
        aname = str(attr.get("attribute") or attr.get("name") or "").strip()
        if not aname or aname == "_id":
            continue
        vtype = attr.get("attribute_data_type") or attr.get("value_type") or "str"
        if vtype not in _ATTR_TYPES:
            vtype = "str"
        if attr.get("optional"):
            lines.append(f"    {aname}: Optional[{vtype}]")
        else:
            lines.append(f"    {aname}: {vtype}")
    return lines


def generate_schema_from_form(form: list[dict[str, Any]], output_path: str | Path | None = None) -> str:
    entities = [item for item in form if item.get("type") == "entity"]
    relations = [item for item in form if item.get("type") == "relation"]

    fields_by_entity: dict[str, list[str]] = {}
    ordered_names: list[str] = []
    for item in entities:
        name = _entity_type_of(item)
        if not name:
            continue
        fields_by_entity[name] = _entity_base_fields(item)
        ordered_names.append(name)

    for rel in relations:
        head = str(rel.get("head_entity_type") or rel.get("head_entity") or "").strip()
        tail = str(rel.get("tail_entity_type") or rel.get("tail_entity") or "").strip()
        rel_name = str(rel.get("relation_type") or rel.get("relation") or "").strip()
        if not head or not tail or not rel_name:
            continue
        if head not in fields_by_entity:
            fields_by_entity[head] = ["    _id: str", "    name: str"]
            ordered_names.append(head)
        fields_by_entity[head].append(f'    {rel_name}: List["{tail}"]')

    lines = ["from typing import List, Optional", ""]
    for name in dict.fromkeys(ordered_names):
        lines.append(f"class {name}:")
        for field_line in dict.fromkeys(fields_by_entity.get(name, ["    _id: str", "    name: str"])):
            lines.append(field_line)
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
