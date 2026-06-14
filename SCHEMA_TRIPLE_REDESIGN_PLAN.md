# Schema Redesign Handoff — Typed-Triple Schema (Attributes vs Relations)

> Status: **PLAN ONLY. No code has been changed.** This is the handoff spec for
> the next engineer/AI. Read it fully before touching code. All decisions in
> §0–§3 are **FINAL** (confirmed by the user). §8 lists every file to change.

---

## 0. Decisions locked with the user (FINAL)

1. **Relations carry NO data type.** A relation only has a *relation type*.
   Entities carry a data type; whenever a relation needs the head/tail data type,
   it is **looked up from the Entity Definitions**, never stored on the relation
   (in schema, in `instances.json`, or in `relations.csv`).
2. **Entity name and entity type are merged into ONE identifier at the schema
   level.** At the schema (type) level there is only `entity_type` (the class
   identifier). `entity_name` exists **only at the instance level**.
3. **`(entity_name, entity_type)` is the composite key** that relation endpoints
   point to, and it **must be unique**. (No separate `_id` is exposed in the new
   model; `entity_name` + `entity_type` together identify an individual. If the
   model can produce duplicates, disambiguate the name.)
4. **One consistent predicate term: `relation_type`** — the same string at the
   schema level and the instance level. Do **not** call it "relation name" in one
   place and "relation type" in another.
5. **`attribute_data_type` has a single source: the schema.** The value-type
   column in `facts.csv` is taken from the schema's declared `attribute_data_type`
   for that attribute; the extractor does not invent it.
6. **Schema is stored as Python class source** (the user accepts this; the file
   is never executed, it is just a declaration format). See §2 for the exact
   shape and the one known limitation.
7. **Cardinality (many-to-one / many-to-many) is removed entirely**, and so is
   the **reverse-relation** concept (`# reverse`). Each relation is one directed
   edge `head_entity_type → relation_type → tail_entity_type`; the solver scans
   `relations.csv` to traverse in either direction.
8. **Solve = the model writes and executes code** (`src/solve.py`) over the three
   data files; it never answers by "eyeballing" the data. Unchanged in principle;
   just told the new file structures.

---

## 1. Canonical glossary (use these names EVERYWHERE — UI, schema, instances, csv)

Three different concepts; never conflate them or rename them between layers:

| Concept (中文) | Field name | Meaning | Lives at |
|---|---|---|---|
| 实体类型 | `entity_type` | the entity class's unique identifier (name+type merged) | schema + instance |
| 实体数据类型 | `entity_data_type` | data type of the entity's identifier (`str`/`int`) | schema only |
| 属性 | `attribute` | a primitive attribute's name | schema + instance |
| 属性数据类型 | `attribute_data_type` | the attribute's value type (`str`/`int`/`float`/`bool`) | schema only (single source) |
| 关系类型 | `relation_type` | the relation predicate (same string both layers) | schema + instance |
| 实体名称 | `entity_name` | a concrete individual's name (e.g. `Geoffrey Hinton`) | instance only |

---

## 2. Schema (type) level — what `schema_builder` produces and how it is stored

Stored as **Python class source** in `concepts/draft_schema.py` /
`concepts/confirmed_schema.py`. New encoding (drops cardinality + reverse +
the redundant `# entity_type` comment, because the **class name *is* the
`entity_type`**):

```python
from typing import List

class Person:                 # entity_type = class name = "Person"
    _id: str                  # entity_data_type (str | int)
    name: str                 # attribute "name", attribute_data_type = str
    born_year: int            # attribute "born_year", attribute_data_type = int
    located_in: List["City"]  # relation_type "located_in", tail_entity_type = City
    works_at: List["Organization"]

class City:                   # entity_type = "City"
    _id: str
    name: str

class Organization:           # entity_type = "Organization"
    _id: str
    name: str
```

Projection to the two UI tables (`schema_to_form`):

- **Entity Definitions** → columns `entity_type | entity_data_type | attribute | attribute_data_type`
  (one entity_type may list several attributes; render attributes grouped under
  the entity, each with its own `attribute_data_type`).
- **Relation Schema** → columns `head_entity_type | relation_type | tail_entity_type`
  (head = owning class, relation_type = field name, tail = target class). **No
  Cardinality column. No data-type columns.**

These are what the model outputs and the user can edit (both modes: edit the
table directly, or ask the model to revise).

### Known limitation of the Python-class form (accepted)
A class cannot declare the **same field name twice**, so a single head type
cannot use the **same `relation_type` to two different tail types**
(`Person.located_in -> City` AND `Person.located_in -> Organization` cannot both
be fields). The user accepts this. If such a case ever arises, the implementer
should either rename one predicate or (future) move the schema to a JSON triple
list. The `parse_schema` checker should surface a clear error rather than
silently dropping the duplicate.

---

## 3. Instance (data) level — `instances.json` and the two CSVs

### 3.1 `instances.json` — the model-authored source of truth, split into TWO sections
`data_extractor` writes one JSON object with two arrays. Every record carries
`source_refs` + `confidence`.

```json
{
  "entities": [
    {
      "entity_name": "Geoffrey Hinton",
      "entity_type": "Person",
      "attributes": { "name": "Geoffrey Hinton", "born_year": 1947 },
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
```

- **Relations carry NO data type** (decision §0-1).
- Relation endpoints reference entities by the composite key
  `(entity_name, entity_type)`, which **must exist in `entities[]`** and be
  unique (decision §0-3). `build_dataset` validates this.

### 3.2 Derived CSVs (column names match the glossary exactly)
`build_dataset` derives these from `instances.json`:

- **`facts.csv`** (attribute triples):
  `entity_name, entity_type, attribute, value, attribute_data_type, source_refs, confidence`
  — `attribute_data_type` is filled from the **schema** (decision §0-5).
- **`relations.csv`** (relation triples):
  `head_entity_name, head_entity_type, relation_type, tail_entity_name, tail_entity_type, source_refs, confidence`
  — no data type.

This replaces today's inconsistent columns (`subject/concept/value_type` and
`subject/subject_concept/object/object_concept`).

### 3.3 Solve
`workspace_solver` is told the structure of `instances.json`, `facts.csv`,
`relations.csv`, writes `src/solve.py`, executes it once (one fix allowed),
filters/joins over those files, and writes `intermediate/solver_result.json`
with top-level keys exactly `ok` / `answer` / `result`. Grounding rules
unchanged (no evidence/web/memory, no hardcoded answers).

---

## 4. Validation rules the checker MUST enforce
("模型的输出必须要符合这些字段" — the model output must conform.) The checker is
`harness/ontology/schema_utils.py` (`parse_schema` / `parsed_schema_to_dict`),
surfaced via the `schema_validator` tool and enforced in `save_schema`.

**Schema level:**
1. Class names (= `entity_type`) are PascalCase and unique.
2. Every class declares `_id: str` or `_id: int` (the `entity_data_type`).
3. Every primitive attribute has a type in `{str, int, float, bool}` (the
   `attribute_data_type`).
4. Every relation field's target (`tail_entity_type`) is a **declared class**.
   Reject unknown targets.
5. No two fields on one class share a name (surface the §2 limitation as an
   error, do not silently drop).
6. **Remove** the cardinality inference and the `# reverse` rules.

**Instance level** (`validate_instances` + `build_dataset`):
7. Every `entity_type` / `head_entity_type` / `tail_entity_type` /
   `relation_type` / `attribute` used is **declared in the schema**.
8. `(entity_name, entity_type)` is unique across `entities[]`.
9. Every relation endpoint `(entity_name, entity_type)` exists in `entities[]`.
10. Each attribute value conforms to its declared `attribute_data_type`.

Keep returning structured `{valid, errors[], ...}` so the gate / `schema_judger`
can repair, exactly like today.

---

## 5. Prior art (why attribute vs relation is split — keep it)
- **This repo (closest):** `build_dataset` already split `facts.csv` (attributes,
  with value type) from `relations.csv` (relations, with head/tail types). The
  redesign promotes this split into the schema + UI + model output and unifies
  the column names.
- **RDF/OWL:** `owl:DatatypeProperty` (subject → typed literal) vs
  `owl:ObjectProperty` (subject → entity). The property type is the marker.
- **Labeled property graphs (Neo4j):** attributes are node properties, relations
  are edges — a structural separation.
- **Wikidata:** each property has a datatype (`wikibase-item` vs literal types).

Conclusion kept: attributes and relations are **two separate kinds** (here: two
`instances.json` sections → two CSVs). Do not emit one undifferentiated list.

---

## 6. End-to-end flow after the change (sanity check — no logic gaps)
1. `problem_clarifier` → `{problem, steps}`.
2. `evidence_collector` → proposes a `schema_plan` (entities with typed
   attributes + relations as `head_type/relation_type/tail_type`), verifies the
   fields are obtainable (probe only), saves the manifest.
3. `schema_builder` → emits the Python-class schema (§2), validates (§4 schema
   rules), `save_schema`, returns `schema_outline`.
4. `schema_judger` → answerable? else one patch cycle.
5. `data_extractor` → writes `instances.json` two-section (§3.1), full data
   collection here, `build_dataset` validates (§4 instance rules) + derives the
   two CSVs (§3.2).
6. `workspace_solver` → writes/executes `src/solve.py`, writes
   `solver_result.json`.
7. coordinator → final concise Chinese answer.

This is a standard typed TBox (schema) / ABox (instances) split; no scientific
gap. The only accepted compromise is the §2 same-predicate-two-tails limitation
of the Python-class form.

---

## 7. (removed — superseded by §0; no open questions remain)

All previously-open questions are now decided in §0.

---

## 8. Files to change (exhaustive)

**Schema core / validation**
- `harness/ontology/schema_utils.py` — keep Python-class source; update
  `parse_schema` (drop cardinality + reverse rules, add §4 rules), and
  `parsed_schema_to_dict` (emit `entity_type`, `entity_data_type`, attributes
  with `attribute_data_type`, relations as `head_entity_type / relation_type /
  tail_entity_type`). Remove `infer_relation_type`.
- `harness/ontology/schema_service.py` — `schema_to_form` (two tables with the
  glossary columns; drop Cardinality + ID-Type-as-separate-concept confusion —
  `entity_data_type` stays as a column), `generate_schema_from_form` (form →
  Python class round-trip), `confirm_schema`.
- `otology_agent_workspace/tools/schema_validator.py` — delegate to the new
  `schema_utils` output.
- `otology_agent_workspace/utils/schema_service_tool.py` — `schema_to_form`
  passthrough.

**Persistence / dataset**
- `otology_agent_workspace/tools/ontology_backend.py` — `save_schema`,
  `get_schema_outline`, `build_dataset` shapes (outline carries
  attributes+`attribute_data_type` and relations as the triple).
- `harness/ontology/data_extractor.py` — biggest change:
  - `instances.json` is now the two-section object (`entities[]` + `relations[]`)
    in §3.1, **not** the class-keyed dict.
  - rewrite `validate_instances` to the §4 instance rules (composite-key
    uniqueness, endpoint existence, declared types, value-type conformance).
  - `_FACT_FIELDS` = `entity_name, entity_type, attribute, value,
    attribute_data_type, source_refs, confidence`.
  - `_RELATION_FIELDS` = `head_entity_name, head_entity_type, relation_type,
    tail_entity_name, tail_entity_type, source_refs, confidence`.
  - derivation: facts from `entities[].attributes` (value_type from schema),
    relations straight from `relations[]`.
- `harness/ontology/workspace_builder.py` — concept stubs + manifest if the
  instance/column shapes change; the dataclass stub `attributes/relations` dicts
  still fit.

**Prompts (model must emit the conforming shape)**
- `.../schema_builder/AGENT.md` — new schema encoding + §4 schema rules; drop
  cardinality/reverse guidance.
- `.../evidence_collector/AGENT.md` — `schema_plan` blueprint must match
  (entities with typed attributes; relations as head/relation/tail types).
- `.../schema_judger/AGENT.md` — judge against the new schema.
- `.../data_extractor/AGENT.md` — rewrite the **Instance Object Shape** and flow
  to the two-section `instances.json` (§3.1) and the composite-key rules.
- `otology_agent_workspace/AGENT.md` (coordinator) — only if the threaded
  `schema_outline` shape changes. Keep pure-LLM orchestration; **no state
  machine / no fallbacks** (see §9).

**Frontend**
- `otology_agent_workspace/frontend/static/app.js` — `state.schemaForm` model;
  the three schema-table renderers (`schemaPreviewTablesHtml` confirmation card;
  sidebar Ontology Schema; Schema Studio editor); add/remove-row + apply-changes
  handlers; two-mode editor. New headers: Entity Definitions
  `Entity Type | Entity Data Type | Attribute | Attribute Data Type`; Relation
  Schema `Head Entity Type | Relation Type | Tail Entity Type`.
- `otology_agent_workspace/frontend/static/index.html` — bump the `?v=` cache
  string; adjust Schema Studio markup if needed.
- `otology_agent_workspace/frontend/static/style.css` — styles for the new
  columns.

**Examples & tests (do not weaken)**
- `otology_agent_workspace/utils/*_schema.py` + `invalid_*` fixtures — update to
  the new encoding.
- `evals/ontology/run_contract_tests.py` (14 tests) — update expectations to the
  new shapes; **keep them strict** (they guard the no-fallback contract).

---

## 9. Hard constraints (must not break)
- **No Python state machine / no hardcoded routing / no fallbacks / no mock
  mode.** Orchestration stays pure-LLM; the coordinator uses only the `task`
  tool and threads paths.
- **Keep generality** — ordinary conversation must still bypass the pipeline.
- **Do not weaken the 14 contract tests** or delete `_ui_backup_v1/`.
- All fixed frontend text in **English**.
- Keep the autonomous CLI (`python -m harness.ontology.pipeline`) working.
