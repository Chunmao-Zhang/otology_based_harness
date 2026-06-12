"""web_search 工具

通用网页搜索，调用 Serper API。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
from langchain_core.tools import tool


@tool
def web_search(query: str, num_results: int = 3) -> str:
    """Search the web and return top results with title, link, and snippet.

    Args:
        query: Search query string.
        num_results: Number of results to return (default 3).
    """
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
    return json.dumps({"query": query, "results": results}, ensure_ascii=False)


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
