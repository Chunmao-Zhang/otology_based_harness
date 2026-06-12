"""Utilities for parsing and validating ontology schema files."""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PRIMITIVES = {"str", "int", "float", "bool"}


@dataclass
class FieldInfo:
    name: str
    raw_type: str
    kind: str
    value_type: str | None = None
    target: str | None = None
    container: str | None = None
    reverse: bool = False
    lineno: int = 0


@dataclass
class ClassInfo:
    name: str
    entity_type: str
    lineno: int
    fields: list[FieldInfo] = field(default_factory=list)

    @property
    def value_type(self) -> str:
        for item in self.fields:
            if item.name == "name" and item.value_type:
                return item.value_type
        for item in self.fields:
            if item.name == "_id" and item.value_type:
                return item.value_type
        return "str"


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

    lines = schema_text.splitlines()
    classes: list[ClassInfo] = []
    class_names: set[str] = set()

    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        entity_type = _entity_type_from_line(lines, node.lineno) or node.name
        class_info = ClassInfo(name=node.name, entity_type=entity_type, lineno=node.lineno)
        for stmt in node.body:
            if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                field_name = stmt.target.id
                reverse = _line_has_reverse(lines, stmt.lineno)
                field_info = _parse_field(field_name, stmt.annotation, reverse, stmt.lineno)
                class_info.fields.append(field_info)
        classes.append(class_info)
        class_names.add(node.name)

    for class_info in classes:
        if not re.match(r"^[A-Z][A-Za-z0-9]*$", class_info.name):
            errors.append(f"{class_info.name}: class name must be PascalCase")

        id_field = next((item for item in class_info.fields if item.name == "_id"), None)
        if id_field is None:
            errors.append(f"{class_info.name}: missing _id field")
        elif id_field.kind != "primitive" or id_field.value_type not in {"str", "int"}:
            errors.append(f"{class_info.name}._id: must be str or int")

        for item in class_info.fields:
            if item.kind == "unknown":
                errors.append(f"{class_info.name}.{item.name}: unsupported type {item.raw_type}")
            if item.kind == "relation" and item.target not in class_names:
                errors.append(f"{class_info.name}.{item.name}: unknown relation target {item.target}")
            if item.reverse and (item.kind != "relation" or item.container != "List"):
                errors.append(f"{class_info.name}.{item.name}: reverse fields must be List[\"Class\"] relations")
            if item.name.endswith("_r") and not item.reverse:
                errors.append(f"{class_info.name}.{item.name}: reverse-looking field must include '# reverse'")

    if not classes:
        errors.append("schema must define at least one class")

    return ParsedSchema(classes=classes, errors=errors, warnings=warnings)


def parsed_schema_to_dict(parsed: ParsedSchema) -> dict[str, Any]:
    classes = []
    relations = []
    for class_info in parsed.classes:
        fields = []
        for item in class_info.fields:
            fields.append({
                "name": item.name,
                "raw_type": item.raw_type,
                "kind": item.kind,
                "value_type": item.value_type,
                "target": item.target,
                "container": item.container,
                "reverse": item.reverse,
                "lineno": item.lineno,
            })
            if item.kind == "relation":
                relations.append({
                    "head": class_info.name,
                    "relation": item.name,
                    "tail": item.target,
                    "container": item.container,
                    "reverse": item.reverse,
                    "relation_type": infer_relation_type(item),
                })
        classes.append({
            "name": class_info.name,
            "entity_type": class_info.entity_type,
            "value_type": class_info.value_type,
            "lineno": class_info.lineno,
            "fields": fields,
        })
    return {
        "valid": parsed.valid,
        "errors": parsed.errors,
        "warnings": parsed.warnings,
        "classes": classes,
        "relations": relations,
    }


def infer_relation_type(field_info: FieldInfo) -> str:
    if field_info.container == "Optional":
        return "many_to_one"
    return "many_to_many"


def _parse_field(name: str, annotation: ast.AST, reverse: bool, lineno: int) -> FieldInfo:
    raw_type = ast.unparse(annotation)
    primitive = _primitive_name(annotation)
    if primitive:
        return FieldInfo(name=name, raw_type=raw_type, kind="primitive", value_type=primitive, reverse=reverse, lineno=lineno)

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
                reverse=reverse,
                lineno=lineno,
            )
        if target_inner:
            return FieldInfo(
                name=name,
                raw_type=raw_type,
                kind="relation",
                target=target_inner,
                container=outer,
                reverse=reverse,
                lineno=lineno,
            )

    return FieldInfo(name=name, raw_type=raw_type, kind="unknown", reverse=reverse, lineno=lineno)


def _primitive_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name) and node.id in PRIMITIVES:
        return node.id
    if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in PRIMITIVES:
        return node.value
    return None


def _class_ref(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name) and node.id not in PRIMITIVES:
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


def _entity_type_from_line(lines: list[str], lineno: int) -> str | None:
    line = lines[lineno - 1] if 0 < lineno <= len(lines) else ""
    marker = "# entity_type:"
    if marker not in line:
        return None
    return line.split(marker, 1)[1].strip() or None


def _line_has_reverse(lines: list[str], lineno: int) -> bool:
    line = lines[lineno - 1] if 0 < lineno <= len(lines) else ""
    return "# reverse" in line
