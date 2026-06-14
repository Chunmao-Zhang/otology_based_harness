"""Utilities for parsing and validating ontology schema files.

The schema is a typed-triple ontology encoded as Python class source (the file is
never executed — it is only a declaration format). One class is one
``entity_type`` (the class name itself). Inside a class:

- ``_id: str | int`` declares the entity's ``entity_data_type`` (schema only; it
  never appears in instances.json or the CSVs).
- a primitive field (``str`` / ``int`` / ``float`` / ``bool`` or
  ``Optional[...]``) is an ``attribute`` whose ``attribute_data_type`` is that
  primitive.
- a field annotated ``List["Tail"]`` / ``Optional["Tail"]`` / ``"Tail"`` is a
  ``relation`` whose ``relation_type`` is the field name and whose
  ``tail_entity_type`` is ``Tail`` (which must be a declared class).

There is no cardinality and no reverse-relation concept: every relation is one
directed edge ``head_entity_type -> relation_type -> tail_entity_type``.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PRIMITIVES = {"str", "int", "float", "bool"}
ID_TYPES = {"str", "int"}


@dataclass
class FieldInfo:
    name: str
    raw_type: str
    kind: str  # "primitive" | "optional_primitive" | "relation" | "unknown"
    value_type: str | None = None
    target: str | None = None
    container: str | None = None
    optional: bool = False
    lineno: int = 0


@dataclass
class ClassInfo:
    name: str
    lineno: int
    fields: list[FieldInfo] = field(default_factory=list)

    @property
    def entity_type(self) -> str:
        """The class name is the entity_type."""
        return self.name

    @property
    def entity_data_type(self) -> str:
        """The declared data type of the entity identifier (`_id: str | int`)."""
        for item in self.fields:
            if item.name == "_id" and item.value_type in ID_TYPES:
                return item.value_type
        return "str"

    # Backward-compatible alias used by a few callers.
    @property
    def value_type(self) -> str:
        return self.entity_data_type


@dataclass
class ParsedSchema:
    classes: list[ClassInfo]
    errors: list[str]
    warnings: list[str]

    @property
    def valid(self) -> bool:
        return not self.errors


def read_schema_text(schema_text: str | None = None, schema_path: str | Path | None = None) -> str:
    if schema_text:
        return schema_text
    if schema_path:
        return Path(schema_path).read_text(encoding="utf-8")
    raise ValueError("Either schema_text or schema_path is required.")


def parse_schema(schema_text: str) -> ParsedSchema:
    errors: list[str] = []
    warnings: list[str] = []
    try:
        tree = ast.parse(schema_text)
    except SyntaxError as exc:
        return ParsedSchema(classes=[], errors=[f"SyntaxError line {exc.lineno}: {exc.msg}"], warnings=[])

    classes: list[ClassInfo] = []
    class_names: set[str] = set()
    duplicate_classes: list[str] = []

    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        if node.name in class_names:
            duplicate_classes.append(node.name)
        class_info = ClassInfo(name=node.name, lineno=node.lineno)
        for stmt in node.body:
            if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                field_name = stmt.target.id
                field_info = _parse_field(field_name, stmt.annotation, stmt.lineno)
                class_info.fields.append(field_info)
        classes.append(class_info)
        class_names.add(node.name)

    for dup in dict.fromkeys(duplicate_classes):
        errors.append(f"{dup}: duplicate class name (each entity_type must be unique)")

    for class_info in classes:
        # (1) class name = entity_type must be PascalCase.
        if not re.match(r"^[A-Z][A-Za-z0-9]*$", class_info.name):
            errors.append(f"{class_info.name}: class name must be PascalCase")

        # (5) no two fields on one class may share a name.
        seen_fields: set[str] = set()
        for item in class_info.fields:
            if item.name in seen_fields:
                errors.append(
                    f"{class_info.name}.{item.name}: duplicate field name "
                    f"(one entity_type cannot reuse the same attribute/relation_type)"
                )
            seen_fields.add(item.name)

        # (2) every class declares _id: str | int (= entity_data_type).
        id_field = next((item for item in class_info.fields if item.name == "_id"), None)
        if id_field is None:
            errors.append(f"{class_info.name}: missing _id field (declare `_id: str` or `_id: int`)")
        elif id_field.kind != "primitive" or id_field.value_type not in ID_TYPES:
            errors.append(f"{class_info.name}._id: entity_data_type must be str or int")

        for item in class_info.fields:
            if item.name == "_id":
                continue
            # (3) every primitive attribute type in {str,int,float,bool}.
            if item.kind == "unknown":
                errors.append(f"{class_info.name}.{item.name}: unsupported type {item.raw_type}")
            # (4) every relation target (tail_entity_type) is a declared class.
            if item.kind == "relation" and item.target not in class_names:
                errors.append(f"{class_info.name}.{item.name}: unknown relation target {item.target}")

    if not classes:
        errors.append("schema must define at least one class")

    return ParsedSchema(classes=classes, errors=errors, warnings=warnings)


def attribute_fields(cls: ClassInfo) -> list[FieldInfo]:
    """Primitive attribute fields of a class (excludes the `_id` declaration)."""
    return [
        f
        for f in cls.fields
        if f.kind in {"primitive", "optional_primitive"} and f.name != "_id"
    ]


def relation_fields(cls: ClassInfo) -> list[FieldInfo]:
    """Relation fields of a class (one directed edge each)."""
    return [f for f in cls.fields if f.kind == "relation"]


def parsed_schema_to_dict(parsed: ParsedSchema) -> dict[str, Any]:
    """Project a parsed schema to the glossary-named structure used everywhere.

    Entity Definitions carry ``entity_type`` / ``entity_data_type`` and a list of
    ``attribute`` / ``attribute_data_type``. Relation Schema is a flat list of
    ``head_entity_type`` / ``relation_type`` / ``tail_entity_type`` triples (no
    cardinality, no data-type columns).
    """
    classes = []
    relations = []
    for class_info in parsed.classes:
        attributes = [
            {
                "attribute": item.name,
                "attribute_data_type": item.value_type or "str",
                "optional": item.kind == "optional_primitive" or item.optional,
            }
            for item in attribute_fields(class_info)
        ]
        for item in relation_fields(class_info):
            relations.append({
                "head_entity_type": class_info.name,
                "relation_type": item.name,
                "tail_entity_type": item.target,
            })
        classes.append({
            "name": class_info.name,
            "entity_type": class_info.entity_type,
            "entity_data_type": class_info.entity_data_type,
            "attributes": attributes,
            "lineno": class_info.lineno,
        })
    return {
        "valid": parsed.valid,
        "errors": parsed.errors,
        "warnings": parsed.warnings,
        "classes": classes,
        "relations": relations,
    }


def _parse_field(name: str, annotation: ast.AST, lineno: int) -> FieldInfo:
    raw_type = ast.unparse(annotation)
    primitive = _primitive_name(annotation)
    if primitive:
        return FieldInfo(name=name, raw_type=raw_type, kind="primitive", value_type=primitive, lineno=lineno)

    # A bare quoted/unquoted class reference is a relation (single directed edge).
    bare_target = _class_ref(annotation)
    if bare_target:
        return FieldInfo(
            name=name,
            raw_type=raw_type,
            kind="relation",
            target=bare_target,
            container=None,
            optional=False,
            lineno=lineno,
        )

    outer, inner = _subscript(annotation)
    if outer in {"Optional", "List"} and inner is not None:
        primitive_inner = _primitive_name(inner)
        target_inner = _class_ref(inner)
        if outer == "Optional" and primitive_inner:
            return FieldInfo(
                name=name,
                raw_type=raw_type,
                kind="optional_primitive",
                value_type=primitive_inner,
                container=outer,
                optional=True,
                lineno=lineno,
            )
        if target_inner:
            return FieldInfo(
                name=name,
                raw_type=raw_type,
                kind="relation",
                target=target_inner,
                container=outer,
                optional=outer == "Optional",
                lineno=lineno,
            )

    return FieldInfo(name=name, raw_type=raw_type, kind="unknown", lineno=lineno)


def _primitive_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name) and node.id in PRIMITIVES:
        return node.id
    if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in PRIMITIVES:
        return node.value
    return None


def _class_ref(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value not in PRIMITIVES:
        return node.value
    if isinstance(node, ast.Name) and node.id not in PRIMITIVES and node.id not in {"List", "Optional"}:
        return node.id
    return None


def _subscript(node: ast.AST) -> tuple[str | None, ast.AST | None]:
    if not isinstance(node, ast.Subscript):
        return None, None
    outer = None
    if isinstance(node.value, ast.Name):
        outer = node.value.id
    elif isinstance(node.value, ast.Attribute):
        outer = node.value.attr
    return outer, node.slice
