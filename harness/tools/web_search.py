"""web_search 工具

通用网页搜索，调用 Serper API。

搜索结果会自动落盘到当前 run 的 `intermediate/web_evidence/` 目录，并维护一个
按查询索引的缓存：后续轮次（如 data_extractor 的补充搜索）遇到相同查询时直接复用
已持久化的证据，而不会重复调用搜索接口。
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

import httpx
from langchain_core.tools import tool

_STAGE_BY_AGENT = {
    "evidence_collector": "evidence",
    "data_extractor": "extract",
}


@tool
def web_search(query: str, num_results: int = 3) -> str:
    """Search the web and return top results with title, link, and snippet.

    Results are persisted under the current run's intermediate/web_evidence/ so that
    later stages can reuse them without searching again. A repeated query returns the
    previously persisted evidence instead of issuing a new search.

    Args:
        query: Search query string.
        num_results: Number of results to return (default 3).
    """
    cached = _cached_results(query)
    if cached is not None:
        return json.dumps(
            {
                "query": query,
                "results": cached,
                "cached": True,
                "note": "Reused persisted web evidence from an earlier search in this run; no new search was issued.",
            },
            ensure_ascii=False,
        )

    service_cfg = _load_serper_config()
    api_key = os.environ.get("SERPER_API_KEY", "") or service_cfg.get("api_key", "")
    if not api_key:
        return json.dumps({"error": "SERPER_API_KEY not set"}, ensure_ascii=False)

    max_results = int(service_cfg.get("max_results_per_call", 3) or 3)
    default_results = int(service_cfg.get("default_num_results", 3) or 3)
    if not num_results:
        num_results = default_results
    num_results = max(1, min(int(num_results), max_results))

    payload = {"q": query, "num": num_results}
    headers = {"X-API-KEY": api_key, "Content-Type": "application/json"}

    try:
        resp = httpx.post(
            "https://google.serper.dev/search",
            json=payload,
            headers=headers,
            timeout=30,
        )
        resp.raise_for_status()
    except httpx.HTTPError as e:
        return json.dumps({"error": f"Search failed: {e}"}, ensure_ascii=False)

    data = resp.json()
    organic = data.get("organic", [])[:num_results]
    results = [
        {
            "title": item.get("title", ""),
            "link": item.get("link", ""),
            "snippet": item.get("snippet", ""),
        }
        for item in organic
    ]

    persisted = _persist_results(query, results)
    response: dict[str, object] = {"query": query, "results": results}
    if persisted:
        response["persisted"] = [
            {"source_id": rec["source_id"], "url": rec["url"], "title": rec["title"]}
            for rec in persisted
        ]
        response["note"] = (
            "Results persisted to intermediate/web_evidence/ and registered for reuse. "
            "Register them in the evidence manifest sources using these source_ids."
        )
    return json.dumps(response, ensure_ascii=False)


def _normalize_query(query: str) -> str:
    return " ".join(str(query or "").strip().lower().split())


def _web_evidence_dir() -> Path | None:
    run_env = os.environ.get("HARNESS_RUN_DIR", "")
    if not run_env:
        return None
    return Path(run_env) / "intermediate" / "web_evidence"


def _read_cache(web_dir: Path) -> dict:
    cache_path = web_dir / "_cache.json"
    if not cache_path.exists():
        return {"queries": {}, "next_id": 1}
    try:
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
    except Exception:
        return {"queries": {}, "next_id": 1}
    cache.setdefault("queries", {})
    cache.setdefault("next_id", 1)
    return cache


def _cached_results(query: str) -> list[dict] | None:
    web_dir = _web_evidence_dir()
    if web_dir is None or not web_dir.exists():
        return None
    cache = _read_cache(web_dir)
    entry = cache.get("queries", {}).get(_normalize_query(query))
    if not entry:
        return None
    results = []
    for source_id in entry.get("source_ids", []):
        path = web_dir / f"{source_id}.json"
        if not path.exists():
            continue
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        results.append(
            {
                "title": record.get("title", ""),
                "link": record.get("url", ""),
                "snippet": record.get("snippet", ""),
            }
        )
    return results or None


def _persist_results(query: str, results: list[dict]) -> list[dict]:
    web_dir = _web_evidence_dir()
    if web_dir is None:
        return []
    try:
        web_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        return []
    cache = _read_cache(web_dir)
    next_id = int(cache.get("next_id", 1) or 1)
    stage = _STAGE_BY_AGENT.get(os.environ.get("HARNESS_AGENT_ID", ""), "evidence")
    retrieved_at = datetime.now().isoformat(timespec="seconds")
    saved: list[dict] = []
    for item in results:
        source_id = f"web_{next_id:03d}"
        next_id += 1
        record = {
            "source_id": source_id,
            "query": query,
            "url": item.get("link", ""),
            "title": item.get("title", ""),
            "snippet": item.get("snippet", ""),
            "retrieved_at": retrieved_at,
            "collected_stage": stage,
        }
        try:
            (web_dir / f"{source_id}.json").write_text(
                json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception:
            continue
        saved.append(record)
    if not saved:
        return []
    cache["queries"][_normalize_query(query)] = {"source_ids": [rec["source_id"] for rec in saved]}
    cache["next_id"] = next_id
    try:
        (web_dir / "_cache.json").write_text(
            json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        pass
    return saved


def _load_serper_config() -> dict:
    root = Path(os.environ.get("HARNESS_ROOT", os.getcwd()))
    config_path = root / "harness.json"
    if not config_path.exists():
        return {}
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    services = raw.get("services", {})
    serper = services.get("serper", {})
    return serper if isinstance(serper, dict) else {}
