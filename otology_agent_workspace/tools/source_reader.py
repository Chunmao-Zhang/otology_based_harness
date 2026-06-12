"""Read local evidence sources for ontology harness agents."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from langchain_core.tools import tool

from .path_utils import resolve_path

MAX_TEXT_CHARS = 20000
CHUNK_SIZE = 1800
MAX_ROWS = 20


@tool
def source_reader(file_paths: list[str], question: str = "") -> str:
    """Read upload files and return structured source summaries as JSON."""
    sources = []
    errors = []
    for file_path in file_paths:
        try:
            path = resolve_path(file_path)
            if not path.exists():
                errors.append({"path": file_path, "error": "file not found"})
                continue
            suffix = path.suffix.lower().lstrip(".") or "unknown"
            if suffix == "csv":
                sources.append(_read_csv(path, file_path, question))
            elif suffix in {"txt", "md"}:
                sources.append(_read_text(path, file_path, suffix, question))
            else:
                errors.append({"path": file_path, "error": f"unsupported file type: {suffix}"})
        except Exception as exc:
            errors.append({"path": file_path, "error": str(exc)})
    return json.dumps({"sources": sources, "errors": errors}, ensure_ascii=False)

def _read_csv(path: Path, original: str, question: str) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        rows = []
        for index, row in enumerate(reader):
            if index >= MAX_ROWS:
                break
            rows.append(dict(row))
        columns = reader.fieldnames or []
    return {
        "source_id": path.name,
        "source_kind": "upload",
        "file_path": original,
        "file_type": "csv",
        "columns": columns,
        "sample_rows": rows,
        "chunks": [],
        "metadata": {
            "size_bytes": path.stat().st_size,
            "question": question,
            "sample_row_count": len(rows),
        },
    }


def _read_text(path: Path, original: str, file_type: str, question: str) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8", errors="replace")[:MAX_TEXT_CHARS]
    chunks = []
    for index in range(0, len(text), CHUNK_SIZE):
        chunk_text = text[index:index + CHUNK_SIZE]
        chunks.append({
            "chunk_id": f"{path.stem}#chunk_{len(chunks) + 1:03d}",
            "text": chunk_text,
        })
    return {
        "source_id": path.name,
        "source_kind": "upload",
        "file_path": original,
        "file_type": file_type,
        "columns": [],
        "sample_rows": [],
        "chunks": chunks,
        "metadata": {
            "size_bytes": path.stat().st_size,
            "question": question,
            "char_count": len(text),
        },
    }
