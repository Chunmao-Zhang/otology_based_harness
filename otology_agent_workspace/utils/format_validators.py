"""Format validation for model-produced schema and instances.

The schema_builder and data_extractor subagents must emit output in a fixed
format. This module is the single place that checks that format and returns a
clear, model-facing report so a non-conforming output can be rejected and
rewritten by the model.

- ``validate_schema_text`` validates the Python-class schema source.
- ``validate_instances_object`` validates the two-section ``instances.json``
  object against a confirmed schema.

Both return ``{"ok": bool, "errors": [...], "format": "<spec text>"}``. When
``ok`` is false, the caller tells the model it does not conform, shows the
``errors``, and re-shows the ``format`` spec so the model can repair and resend.
The ``*_FORMAT_SPEC`` constants are the same format text quoted in the agent
prompts, so the rules the model is told to follow and the rules enforced here
never drift apart.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from harness.ontology.data_extractor import validate_instances
from harness.ontology.schema_utils import parse_schema, read_schema_text

SCHEMA_FORMAT_SPEC = """\
SCHEMA FORMAT (Python class source; the file is never executed):

    from typing import List, Optional

    class Person:
        _id: str                       # entity_data_type: str or int (schema only)
        name: str                      # attribute, attribute_data_type str
        born_year: int                 # attribute, attribute_data_type int
        works_at: List["Organization"] # relation_type works_at -> tail Organization

    class Organization:
        _id: str
        name: str

Rules:
  1. One class per entity_type. Class names are PascalCase and unique; the class
     name IS the entity_type (do NOT add `# entity_type:` comments).
  2. Every class declares exactly one `_id: str` or `_id: int`. This is the
     entity_data_type and lives ONLY in the schema (never in instances/CSVs).
  3. Attributes are primitive fields typed str / int / float / bool (or
     Optional[...]). The primitive is the attribute_data_type.
  4. A relation is a field typed `List["Tail"]` whose name is the relation_type
     and whose Tail is a class declared in the SAME schema. Unknown targets are
     rejected. Relations carry NO data type and NO cardinality.
  5. There is NO reverse relation and NO cardinality. Each relation is one
     directed edge head_entity_type -> relation_type -> tail_entity_type. To make
     an edge traversable in one direction only, declare it once on the head class.
  6. A class cannot declare the same field name twice, so one entity_type cannot
     reuse one relation_type for two different tails.
  7. A literal value (year, count, date, rating, flag) is an attribute, never a
     relation."""

INSTANCES_FORMAT_SPEC = """\
INSTANCES FORMAT (data/instances.json is ONE object with two sections):

    {
      "entities": [
        {
          "entity_name": "Geoffrey Hinton",
          "entity_type": "Person",
          "attributes": {"name": "Geoffrey Hinton", "born_year": 1947},
          "source_refs": ["web_001"],
          "confidence": 0.95
        }
      ],
      "relations": [
        {
          "head_entity_name": "Geoffrey Hinton",
          "head_entity_type": "Person",
          "relation_type": "works_at",
          "tail_entity_name": "University of Toronto",
          "tail_entity_type": "Organization",
          "source_refs": ["web_004"],
          "confidence": 0.9
        }
      ]
    }

Rules:
  1. Top level is an object with an `entities` list and a `relations` list (never
     keyed by class name, never a bare list).
  2. Each entity has entity_name, entity_type (a declared class), and an
     `attributes` object whose keys are declared attributes of that class. Do NOT
     put `_id` on instances. Attribute values must match the declared
     attribute_data_type (e.g. an `int` attribute gets a number, not prose).
  3. (entity_name, entity_type) is the composite key and must be unique across
     `entities`.
  4. Each relation uses head_entity_(name,type) / relation_type /
     tail_entity_(name,type) declared in the schema. Both endpoints must already
     exist in `entities`.
  5. Put source_refs (registered evidence ids) and confidence on every record."""


def _format_report(ok: bool, errors: list[str], spec: str) -> dict[str, Any]:
    return {"ok": ok, "errors": errors, "format": spec}


def validate_schema_text(
    schema_text: str | None = None,
    schema_path: str | Path | None = None,
) -> dict[str, Any]:
    """Validate schema source; return a model-facing ``{ok, errors, format}``."""
    text = read_schema_text(schema_text=schema_text, schema_path=schema_path)
    parsed = parse_schema(text)
    return _format_report(parsed.valid, parsed.errors, SCHEMA_FORMAT_SPEC)


def validate_instances_object(
    instances: Any,
    schema_text: str | None = None,
    schema_path: str | Path | None = None,
) -> dict[str, Any]:
    """Validate the two-section instances object; ``{ok, errors, format}``."""
    if schema_path is None and schema_text is not None:
        # validate_instances reads a schema file, so persist the text once.
        raise ValueError("validate_instances_object requires schema_path")
    result = validate_instances(instances, schema_path)
    return _format_report(bool(result.get("ok")), list(result.get("errors", [])), INSTANCES_FORMAT_SPEC)
