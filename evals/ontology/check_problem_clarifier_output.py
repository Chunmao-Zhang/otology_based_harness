from __future__ import annotations

import json
import re
import sys
from pathlib import Path


def main(path: str) -> int:
    text = Path(path).read_text(encoding="utf-8").strip()
    data = json.loads(_extract_json(text))
    assert set(data) == {"problem", "steps"}, data
    assert isinstance(data["problem"], str) and data["problem"].strip()
    assert isinstance(data["steps"], list) and data["steps"]
    assert all(isinstance(item, str) and item.strip() for item in data["steps"])
    forbidden = {"web_search", "source_reader", "schema_builder", "schema_judger", "data_extractor"}
    joined = "\n".join(data["steps"])
    assert not any(item in joined for item in forbidden), joined
    print("PASS problem_clarifier_output")
    return 0


def _extract_json(text: str) -> str:
    if text.startswith("{"):
        return text
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced:
        return fenced.group(1)
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        return text[start:end + 1]
    return text


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
