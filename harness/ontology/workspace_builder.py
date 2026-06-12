"""Build run workspaces for ontology solving."""

from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path

from harness.ontology.schema_utils import parse_schema


def build_workspace(
    run_dir: str | Path,
    schema_path: str | Path,
    instances_path: str | Path | None = None,
    facts_path: str | Path | None = None,
    relations_path: str | Path | None = None,
) -> dict:
    run = Path(run_dir)
    concepts_dir = run / "concepts"
    data_dir = run / "data"
    src_dir = run / "src"
    intermediate_dir = run / "intermediate"
    for directory in [concepts_dir, data_dir, src_dir, intermediate_dir]:
        directory.mkdir(parents=True, exist_ok=True)

    schema_src = Path(schema_path)
    confirmed_schema = concepts_dir / "confirmed_schema.py"
    if schema_src.resolve() != confirmed_schema.resolve():
        shutil.copyfile(schema_src, confirmed_schema)

    schema_text = confirmed_schema.read_text(encoding="utf-8")
    parsed = parse_schema(schema_text)
    if not parsed.valid:
        return {"ok": False, "errors": parsed.errors, "workspace_dir": str(run)}

    generated_files: list[str] = [str(confirmed_schema)]
    for class_info in parsed.classes:
        concept_path = concepts_dir / f"{class_info.name}.py"
        concept_path.write_text(_concept_file(class_info.name), encoding="utf-8")
        generated_files.append(str(concept_path))

    for source, name in [
        (instances_path, "instances.json"),
        (facts_path, "facts.csv"),
        (relations_path, "relations.csv"),
    ]:
        if source:
            target = data_dir / name
            if Path(source).resolve() != target.resolve():
                shutil.copyfile(source, target)
            generated_files.append(str(target))

    main_path = src_dir / "main.py"
    if not main_path.exists():
        main_path.write_text(_main_py(), encoding="utf-8")
    generated_files.append(str(main_path))

    manifest = {
        "workspace_dir": str(run),
        "created_at": datetime.now().isoformat(),
        "schema_path": str(confirmed_schema),
        "files": sorted(set(generated_files)),
    }
    manifest_path = intermediate_dir / "workspace_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    manifest["manifest_path"] = str(manifest_path)
    manifest["ok"] = True
    return manifest


def _concept_file(class_name: str) -> str:
    return f'''from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class {class_name}:
    _id: str
    attributes: dict[str, Any] = field(default_factory=dict)
    relations: dict[str, list[str] | str | None] = field(default_factory=dict)
    source_refs: list[str] = field(default_factory=list)
    confidence: float | None = None
'''


def _main_py() -> str:
    return '''from __future__ import annotations

import json
from pathlib import Path


def load_instances(workspace_dir: str | Path = ".") -> dict:
    path = Path(workspace_dir) / "data" / "instances.json"
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def summarize_instances(instances: dict) -> dict:
    return {concept: len(items) for concept, items in instances.items()}


if __name__ == "__main__":
    data = load_instances(Path(__file__).resolve().parents[1])
    print(json.dumps(summarize_instances(data), ensure_ascii=False, indent=2))
'''
