"""Deterministic workspace solver helpers for ontology MVP runs."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any


def solve_company_workspace(question: str, workspace_dir: str | Path) -> dict[str, Any]:
    """Write and execute a small solver script inside a run workspace."""

    run = Path(workspace_dir)
    src_dir = run / "src"
    intermediate_dir = run / "intermediate"
    data_dir = run / "data"
    src_dir.mkdir(parents=True, exist_ok=True)
    intermediate_dir.mkdir(parents=True, exist_ok=True)

    script_path = src_dir / "solve.py"
    script_path.write_text(_solve_py(), encoding="utf-8")

    result = subprocess.run(
        ["python3", str(script_path)],
        cwd=str(run),
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        solver_result = {
            "ok": False,
            "question": question,
            "executed_script": str(script_path),
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
    else:
        payload = json.loads(result.stdout or "{}")
        solver_result = {
            "ok": True,
            "question": question,
            "answer": payload.get("answer", ""),
            "companies": payload.get("companies", []),
            "schema_path": str(run / "concepts" / "confirmed_schema.py"),
            "source_files": [
                str(data_dir / "instances.json"),
                str(data_dir / "facts.csv"),
                str(data_dir / "relations.csv"),
            ],
            "executed_script": str(script_path),
            "stdout": result.stdout,
        }

    result_path = intermediate_dir / "solver_result.json"
    result_path.write_text(json.dumps(solver_result, ensure_ascii=False, indent=2), encoding="utf-8")
    solver_result["solver_result_path"] = str(result_path)
    return solver_result


def _solve_py() -> str:
    return '''from __future__ import annotations

import csv
import json
from pathlib import Path


RUN_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = RUN_DIR / "data"


def load_instances() -> dict:
    with open(DATA_DIR / "instances.json", "r", encoding="utf-8") as f:
        return json.load(f)


def load_relations() -> list[dict]:
    with open(DATA_DIR / "relations.csv", "r", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def main() -> None:
    instances = load_instances()
    relations = load_relations()
    industries = {
        item["_id"]: item.get("name", "")
        for item in instances.get("Industry", [])
    }
    industry_by_company = {
        row["subject"]: industries.get(row["object"], row["object"])
        for row in relations
        if row.get("relation") == "operates_in_industry"
    }
    analytics_terms = {
        "data analytics",
        "analytics software",
        "cloud data platform",
    }
    companies = []
    for company in instances.get("Company", []):
        industry = industry_by_company.get(company["_id"], "")
        if company.get("country") == "United States" and industry.lower() in analytics_terms:
            companies.append({
                "name": company.get("name", ""),
                "industry": industry,
                "source_refs": company.get("source_refs", []),
            })

    answer = "共找到 {} 家美国数据分析相关公司：{}。".format(
        len(companies),
        "、".join("{} ({})".format(item["name"], item["industry"]) for item in companies),
    )
    print(json.dumps({"answer": answer, "companies": companies}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
'''
