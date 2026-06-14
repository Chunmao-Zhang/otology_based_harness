from __future__ import annotations

import csv
import importlib.util
import json
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKSPACE = ROOT / "otology_agent_workspace"
TEST_DATA = ROOT / "test_data" / "ontology"
SCHEMA_UTILS = WORKSPACE / "utils"
EVALS = ROOT / "evals" / "ontology"
ONTOLOGY_RUNS = ROOT / "runs" / "ontology_workspace_runs"
RUN_DIR = ONTOLOGY_RUNS / "contract"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("HARNESS_ROOT", str(ROOT))

from harness.agents.registry import AgentRegistry
from harness.agents.agent_loop import _build_model, _register_ontology_model_profile
from harness.config import load_config
from harness.ontology.data_extractor import (
    persist_extraction,
    schema_outline,
    validate_instances,
)
from harness.ontology.schema_builder import write_draft_schema
from harness.ontology.schema_judge import mechanical_schema_check
from harness.ontology.schema_service import confirm_schema, schema_to_form
from harness.ontology.schema_utils import parse_schema
from harness.ontology.solver import read_solver_result
from harness.ontology.workspace_builder import build_workspace
from harness.tools.registry import get_tools_for_agent
from harness.tools.web_search import _load_serper_config, web_search
from deepagents.profiles.harness.harness_profiles import _harness_profile_for_model
from otology_agent_workspace.tools.evidence_retriever import evidence_retriever
from otology_agent_workspace.utils.evidence_manifest_writer import evidence_manifest_writer
from otology_agent_workspace.utils.problem_clarifier_contract import validate_problem_clarifier_output, validate_problem_clarifier_output_json
from otology_agent_workspace.tools.schema_validator import schema_validator
from otology_agent_workspace.utils.schema_service_tool import schema_confirm as schema_confirm_tool, schema_to_form as schema_to_form_tool
from otology_agent_workspace.tools.source_reader import source_reader
from otology_agent_workspace.utils.workspace_builder_tool import workspace_builder_tool


def main() -> int:
    failures: list[str] = []
    checks = [
        ("config_and_agents", check_config_and_agents),
        ("agent_prompts", check_agent_prompts),
        ("workspace_tools", check_workspace_tools),
        ("problem_clarifier_contract", check_problem_clarifier_contract),
        ("source_reader", check_source_reader),
        ("schema_validator", check_schema_validator),
        ("evidence_retriever", check_evidence_retriever),
        ("schema_builder_and_judger", check_schema_builder_and_judger),
        ("schema_service", check_schema_service),
        ("data_workspace_solver_files", check_data_workspace_solver_files),
        ("eval_jsonl_sets", check_eval_jsonl_sets),
        ("new_workspace_tools", check_new_workspace_tools),
        ("web_search_scenarios", check_web_search_scenarios),
        ("web_search_cost_config", check_web_search_cost_config),
    ]
    for name, fn in checks:
        try:
            fn()
            print(f"PASS {name}")
        except Exception as exc:
            failures.append(f"{name}: {exc}")
            print(f"FAIL {name}: {exc}")

    if failures:
        print("\nFailures:")
        for failure in failures:
            print(f"- {failure}")
        return 1
    return 0


def check_config_and_agents() -> None:
    cfg = load_config(ROOT / "harness.json")
    registry = AgentRegistry(cfg)
    expected = {
        "ontology_coordinator",
        "problem_clarifier",
        "evidence_collector",
        "schema_builder",
        "schema_judger",
        "data_extractor",
        "workspace_solver",
    }
    actual = {agent.id for agent in registry.list_all()}
    assert expected <= actual, actual
    assert registry.get_default().id == "ontology_coordinator"
    assert cfg.defaults.model.model_id == "deepseek-v4-flash"
    _register_ontology_model_profile(registry.get("ontology_coordinator").model)
    model = _build_model(registry.get("ontology_coordinator").model)
    profile = _harness_profile_for_model(model, None)
    assert profile.general_purpose_subagent is not None
    assert profile.general_purpose_subagent.enabled is False

    coordinator_text = (WORKSPACE / "AGENT.md").read_text(encoding="utf-8")
    for marker in ["problem_clarifier", "evidence_collector", "schema_builder", "schema_judger", "data_extractor", "workspace_solver"]:
        assert marker in coordinator_text
    # Pure-LLM orchestration: the coordinator runs autonomously (no human gates)
    # and orchestrates only through the `task` tool, delegating every concrete
    # action to a subagent. It must never answer before the solver runs.
    assert "Autonomous Mode" in coordinator_text
    assert "only `task`" in coordinator_text
    assert "solver_result.json" in coordinator_text


def check_agent_prompts() -> None:
    worker_dirs = [
        "problem_clarifier",
        "evidence_collector",
        "schema_builder",
        "schema_judger",
        "data_extractor",
    ]
    for name in worker_dirs:
        text = (WORKSPACE / "subagent_worksapce" / name / "AGENT.md").read_text(encoding="utf-8")
        assert "Critical Response Contract" in text
        assert "The first character must be `{`" in text
        assert "Fenced code blocks are contract failures" in text
        assert "Do not ask the user follow-up questions" in text


def check_workspace_tools() -> None:
    cfg = load_config(ROOT / "harness.json")
    registry = AgentRegistry(cfg)
    schema_judger = registry.get("schema_judger")
    names = {tool.name for tool in get_tools_for_agent(schema_judger, schema_judger.workspace, str(ROOT))}
    assert "schema_validator" in names
    assert "execute_code" not in names
    problem = registry.get("problem_clarifier")
    problem_tools = {tool.name for tool in get_tools_for_agent(problem, problem.workspace, str(ROOT))}
    assert "source_reader" in problem_tools
    assert "schema_validator" not in problem_tools
    coordinator = registry.get("ontology_coordinator")
    coordinator_tools = {tool.name for tool in get_tools_for_agent(coordinator, coordinator.workspace, str(ROOT))}
    assert "problem_clarifier_contract" not in coordinator_tools
    assert "source_reader" not in coordinator_tools
    # Pure-LLM orchestration: the coordinator has NO worker tools; it can only
    # delegate via the deepagents `task` tool. None of the backend ontology
    # tools may be reachable by the coordinator itself.
    for forbidden in {"save_evidence_manifest", "save_schema", "get_schema_outline", "build_dataset", "schema_validator", "web_search", "execute_code"}:
        assert forbidden not in coordinator_tools, coordinator_tools
    evidence = registry.get("evidence_collector")
    evidence_tools = {tool.name for tool in get_tools_for_agent(evidence, evidence.workspace, str(ROOT))}
    assert "evidence_manifest_writer" not in evidence_tools
    # The deterministic backend ops are agent-callable tools, owned by the agent
    # that performs that step (not auto-routed by Python).
    assert "save_evidence_manifest" in evidence_tools
    data = registry.get("data_extractor")
    data_tools = {tool.name for tool in get_tools_for_agent(data, data.workspace, str(ROOT))}
    assert "data_extract_company_csv" not in data_tools
    assert {"get_schema_outline", "build_dataset"} <= data_tools, data_tools
    # write_file is a built-in execution-layer tool, granted via the allow-list.
    assert "write_file" in data.tools.allow
    builder = registry.get("schema_builder")
    builder_tools = {tool.name for tool in get_tools_for_agent(builder, builder.workspace, str(ROOT))}
    assert "schema_draft_builder" not in builder_tools
    assert "save_schema" in builder_tools
    solver = registry.get("workspace_solver")
    solver_tools = {tool.name for tool in get_tools_for_agent(solver, solver.workspace, str(ROOT))}
    assert "workspace_solver_tool" not in solver_tools
    assert "execute_code" in solver_tools
    assert "write_file" in solver.tools.allow


def check_problem_clarifier_contract() -> None:
    valid_text = json.dumps({
        "problem": "统计上传文件中的美国数据分析公司",
        "steps": ["整理可用证据", "构建 schema", "抽取数据", "回答问题"],
    }, ensure_ascii=False)
    valid = validate_problem_clarifier_output(valid_text)
    assert valid["ok"] is True, valid
    assert valid["problem"]
    assert valid["steps"]

    invalid = json.loads(validate_problem_clarifier_output_json("问题是统计公司，步骤是读取文件。"))
    assert invalid["ok"] is False, invalid
    assert invalid["repair_instruction"]

    extra = json.loads(validate_problem_clarifier_output_json(json.dumps({"problem": "x", "steps": ["y"], "extra": 1}, ensure_ascii=False)))
    assert extra["ok"] is False and any("extra keys" in item for item in extra["errors"]), extra


def check_source_reader() -> None:
    result = json.loads(source_reader.invoke({
        "file_paths": [
            "test_data/ontology/company_sample.csv",
            "test_data/ontology/company_notes.txt",
            "test_data/ontology/company_notes.md",
        ],
        "question": "美国有哪些数据分析公司",
    }))
    assert not result["errors"], result["errors"]
    sources = {item["file_type"]: item for item in result["sources"]}
    assert sources["csv"]["columns"] == ["name", "country", "industry", "description"]
    assert sources["csv"]["sample_rows"]
    assert sources["txt"]["chunks"]
    assert sources["md"]["chunks"]


def check_schema_validator() -> None:
    valid = json.loads(schema_validator.invoke({"schema_path": str(SCHEMA_UTILS / "valid_company_schema.py")}))
    missing = json.loads(schema_validator.invoke({"schema_path": str(SCHEMA_UTILS / "invalid_missing_id.py")}))
    unknown = json.loads(schema_validator.invoke({"schema_path": str(SCHEMA_UTILS / "invalid_unknown_relation.py")}))
    assert valid["valid"] is True, valid
    assert missing["valid"] is False and any("missing _id" in err for err in missing["errors"]), missing
    assert unknown["valid"] is False and any("unknown relation target" in err for err in unknown["errors"]), unknown


def check_evidence_retriever() -> None:
    if RUN_DIR.exists():
        shutil.rmtree(RUN_DIR)
    manifest_path = RUN_DIR / "intermediate" / "evidence_manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    source_result = json.loads(source_reader.invoke({
        "file_paths": ["test_data/ontology/company_sample.csv"],
        "question": "美国有哪些数据分析公司",
    }))
    manifest = {
        "sources": source_result["sources"],
        "needs_web_search": False,
        "handler": "schema_builder",
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    retrieved = json.loads(evidence_retriever.invoke({
        "query": "United States data analytics",
        "manifest_path": str(manifest_path),
        "top_k": 1,
    }))
    assert len(retrieved["chunks"]) == 1
    assert retrieved["chunks"][0]["score"] >= 0


def check_schema_service() -> None:
    schema_path = SCHEMA_UTILS / "valid_company_schema.py"
    form = schema_to_form(schema_path=schema_path)
    assert any(item.get("type") == "entity" and item.get("name") == "Company" for item in form)
    assert any(item.get("type") == "relation" and item.get("relation") == "operates_in_industry" for item in form)
    confirmed = confirm_schema(schema_path, RUN_DIR / "concepts" / "confirmed_schema.py")
    assert confirmed["valid"] is True
    parsed = parse_schema((RUN_DIR / "concepts" / "confirmed_schema.py").read_text(encoding="utf-8"))
    assert parsed.valid


def check_schema_builder_and_judger() -> None:
    # schema_builder backend only persists + validates LLM-produced schema text.
    draft_path = RUN_DIR / "concepts" / "draft_schema.py"
    schema_text = (SCHEMA_UTILS / "valid_company_schema.py").read_text(encoding="utf-8")
    built = write_draft_schema(schema_text, draft_path)
    assert built["valid"] is True, built
    assert draft_path.exists()

    empty = write_draft_schema("", RUN_DIR / "concepts" / "empty_schema.py")
    assert empty["valid"] is False, empty

    # schema_judge backend only reports structural facts (no domain heuristics).
    report = mechanical_schema_check(schema_path=draft_path)
    assert report["valid"] is True, report
    assert "Company" in report["entities"]
    assert report["relation_count"] >= 1

    bad = mechanical_schema_check(schema_path=SCHEMA_UTILS / "invalid_missing_id.py")
    assert bad["valid"] is False, bad


def check_data_workspace_solver_files() -> None:
    schema_path = RUN_DIR / "concepts" / "confirmed_schema.py"
    if not schema_path.exists():
        confirm_schema(SCHEMA_UTILS / "valid_company_schema.py", schema_path)

    # schema_outline exposes the exact class/field names the extractor must use.
    outline = schema_outline(schema_path)
    concepts = {entry["concept"] for entry in outline}
    assert {"Company", "Industry"} <= concepts, outline

    # Instances the data_extractor agent would write, keyed by schema class names.
    instances = {
        "Company": [
            {"_id": "palantir", "name": "Palantir", "country": "United States",
             "operates_in_industry": ["data_analytics"], "source_refs": ["company_sample.csv"], "confidence": 0.9},
        ],
        "Industry": [
            {"_id": "data_analytics", "name": "Data Analytics", "source_refs": ["company_sample.csv"], "confidence": 0.9},
        ],
    }

    # validate_instances must accept schema-conformant data and reject drift.
    assert validate_instances(instances, schema_path)["ok"], validate_instances(instances, schema_path)
    drifted = {"Organization": [{"_id": "x", "name": "X"}]}
    assert validate_instances(drifted, schema_path)["ok"] is False

    extracted = persist_extraction(instances, schema_path, RUN_DIR)
    assert extracted["ok"], extracted

    ids = {item["_id"] for rows in instances.values() for item in rows}
    with open(extracted["relations_path"], "r", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert rows, "expected at least one relation row"
    for row in rows:
        assert row["subject"] in ids
        assert row["object"] in ids

    manifest = build_workspace(
        RUN_DIR,
        schema_path,
        extracted["instances_path"],
        extracted["facts_path"],
        extracted["relations_path"],
    )
    assert manifest["ok"], manifest
    assert (RUN_DIR / "intermediate/workspace_manifest.json").exists()
    assert (RUN_DIR / "src/main.py").exists()

    spec = importlib.util.spec_from_file_location("contract_main", RUN_DIR / "src/main.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    summary = module.summarize_instances(module.load_instances(RUN_DIR))
    assert summary["Company"] >= 1

    # solver backend only reads back the agent-written solver_result.json.
    missing_dir = ONTOLOGY_RUNS / "contract_no_solver"
    if missing_dir.exists():
        shutil.rmtree(missing_dir)
    assert read_solver_result(missing_dir)["ok"] is False
    solver_result = {"ok": True, "answer": "Palantir", "result": ["Palantir"]}
    (RUN_DIR / "intermediate/solver_result.json").write_text(
        json.dumps(solver_result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    read_back = read_solver_result(RUN_DIR)
    assert read_back["ok"] is True and read_back["answer"] == "Palantir", read_back


def check_web_search_cost_config() -> None:
    cfg = _load_serper_config()
    assert cfg.get("default_num_results") == 3
    assert cfg.get("max_results_per_call") == 3
    # Do not make a network call here. This checks config-based cost limits only.
    assert web_search.name == "web_search"


def check_eval_jsonl_sets() -> None:
    names = [
        "problem_clarifier_cases.jsonl",
        "evidence_collector_cases.jsonl",
        "schema_builder_cases.jsonl",
        "schema_judger_cases.jsonl",
        "data_extractor_cases.jsonl",
        "workspace_solver_cases.jsonl",
        "web_search_cases.jsonl",
    ]
    for name in names:
        path = EVALS / name
        assert path.exists(), name
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        assert len(rows) >= 3, name


def check_new_workspace_tools() -> None:
    source_result = json.loads(source_reader.invoke({
        "file_paths": ["test_data/ontology/company_sample.csv"],
        "question": "美国有哪些数据分析公司",
    }))
    manifest_path = RUN_DIR / "intermediate" / "tool_manifest.json"
    written = json.loads(evidence_manifest_writer.invoke({
        "sources_json": json.dumps(source_result["sources"], ensure_ascii=False),
        "output_path": str(manifest_path),
        "needs_web_search": False,
        "handler": "schema_builder",
    }))
    assert written["ok"] is True
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["handler"] == "schema_builder"

    form = json.loads(schema_to_form_tool.invoke({"schema_path": str(SCHEMA_UTILS / "valid_company_schema.py")}))
    assert form["ok"] is True

    confirmed_path = RUN_DIR / "concepts" / "tool_confirmed_schema.py"
    confirmed = json.loads(schema_confirm_tool.invoke({
        "draft_schema_path": str(SCHEMA_UTILS / "valid_company_schema.py"),
        "confirmed_schema_path": str(confirmed_path),
    }))
    assert confirmed["ok"] is True

    tool_run = ONTOLOGY_RUNS / "tool_run"
    if tool_run.exists():
        shutil.rmtree(tool_run)

    # The data_extractor agent writes instances.json; the backend persists facts/relations.
    instances = {
        "Company": [
            {"_id": "palantir", "name": "Palantir", "country": "United States",
             "operates_in_industry": ["data_analytics"], "source_refs": ["company_sample.csv"], "confidence": 0.9},
        ],
        "Industry": [
            {"_id": "data_analytics", "name": "Data Analytics", "source_refs": ["company_sample.csv"], "confidence": 0.9},
        ],
    }
    extracted = persist_extraction(instances, confirmed_path, tool_run)
    assert extracted["ok"] is True

    built = json.loads(workspace_builder_tool.invoke({
        "run_dir": str(tool_run),
        "schema_path": str(confirmed_path),
        "instances_path": extracted["instances_path"],
        "facts_path": extracted["facts_path"],
        "relations_path": extracted["relations_path"],
    }))
    assert built["ok"] is True


def check_web_search_scenarios() -> None:
    cfg = _load_serper_config()
    max_results = cfg.get("max_results_per_call", 3)
    rows = [
        json.loads(line)
        for line in (EVALS / "web_search_cases.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    for row in rows:
        requested = row["num_results"]
        expected = min(requested, max_results)
        assert expected == row["expected_max_results"]


if __name__ == "__main__":
    raise SystemExit(main())
