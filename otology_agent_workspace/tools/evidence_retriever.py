"""Retrieve relevant chunks from saved evidence manifests."""

from __future__ import annotations

import json
import re
from typing import Any

from langchain_core.tools import tool

from .path_utils import resolve_path


@tool
def evidence_retriever(query: str, manifest_path: str, source_ids: list[str] | None = None, top_k: int = 5) -> str:
    """Retrieve relevant evidence chunks from an evidence manifest."""
    path = resolve_path(manifest_path)
    if not path.exists():
        return json.dumps({"chunks": [], "error": f"manifest not found: {manifest_path}"}, ensure_ascii=False)
    if not path.is_file():
        return json.dumps(
            {"chunks": [], "error": f"manifest_path is not a file (expected evidence_manifest.json): {manifest_path}"},
            ensure_ascii=False,
        )

    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return json.dumps({"chunks": [], "error": f"failed to read manifest {manifest_path}: {exc}"}, ensure_ascii=False)
    if not isinstance(manifest, dict):
        return json.dumps({"chunks": [], "error": f"manifest is not a JSON object: {manifest_path}"}, ensure_ascii=False)
    allowed = set(source_ids or [])
    candidates = []
    for source in manifest.get("sources", []):
        source_id = source.get("source_id", "")
        if allowed and source_id not in allowed:
            continue
        for chunk in _source_chunks(source):
            text = chunk.get("text", "")
            candidates.append({
                "evidence_id": chunk.get("chunk_id", f"{source_id}#chunk"),
                "source_id": source_id,
                "text": text,
                "score": _score(query, text),
            })
    candidates.sort(key=lambda item: item["score"], reverse=True)
    return json.dumps({"chunks": candidates[:max(1, top_k)]}, ensure_ascii=False)

def _source_chunks(source: dict[str, Any]) -> list[dict[str, str]]:
    chunks = list(source.get("chunks") or [])
    if chunks:
        return chunks
    if source.get("sample_rows"):
        text = json.dumps(source.get("sample_rows"), ensure_ascii=False)
        return [{"chunk_id": f"{source.get('source_id')}#sample_rows", "text": text}]
    return []


def _score(query: str, text: str) -> float:
    query_terms = set(re.findall(r"[\w\u4e00-\u9fff]+", query.lower()))
    text_terms = set(re.findall(r"[\w\u4e00-\u9fff]+", text.lower()))
    if not query_terms:
        return 0.0
    return len(query_terms & text_terms) / len(query_terms)
