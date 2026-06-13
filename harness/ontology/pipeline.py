"""Backend orchestrator for the ontology QA pipeline.

This runs the full design-doc workflow directly against the backend (no UI),
driving every step with the declared LLM subagents and persisting artifacts with
the domain-agnostic ontology backend. There is no deterministic Company/Industry
fallback anywhere in this module.

Steps (per docs/ontology_harness_design.md):

1. problem_clarifier  -> {problem, steps}
2. evidence_collector -> sources + needs_web_search + schema_plan -> evidence_manifest.json
3. schema_builder     -> schema_text -> concepts/draft_schema.py
4. schema_judger      -> {answerable, coverage_score, missing_requirements, ...}
   (patch via schema_builder once if not answerable)
5. confirm schema     -> concepts/confirmed_schema.py            (backend)
6. data_extractor     -> data/instances.json -> facts.csv/relations.csv/report (backend derives)
7. build workspace    -> concepts/*, src/main.py, workspace_manifest.json       (backend)
8. workspace_solver   -> src/solve.py + execute -> intermediate/solver_result.json
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from langchain_core.messages import HumanMessage

from harness.agents.agent_loop import build_agent
from harness.agents.registry import AgentRegistry
from harness.config import load_config
from harness.ontology.data_extractor import persist_extraction, schema_outline, validate_instances
from harness.ontology.json_contract import extract_json_object
from harness.ontology.schema_builder import write_draft_schema
from harness.ontology.schema_judge import mechanical_schema_check
from harness.ontology.schema_service import confirm_schema, schema_to_form
from harness.ontology.solver import read_solver_result
from harness.ontology.workspace_builder import build_workspace

LogFn = Callable[[str], None]


class PipelineError(RuntimeError):
    """Raised when a pipeline step cannot satisfy its contract."""


class OntologyPipeline:
    def __init__(
        self,
        harness_root: str | Path,
        run_id: str | None = None,
        config_path: str | Path | None = None,
        log: LogFn | None = None,
        recursion_limit: int = 120,
    ) -> None:
        self.root = Path(harness_root).resolve()
        self.run_id = run_id or datetime.now().strftime("run_%Y%m%d_%H%M%S")
        self.run_dir = self.root / "runs" / "ontology_workspace_runs" / self.run_id
        # Virtual (root-relative) run path passed to subagents so their write_file /
        # execute_code / ontology tools all resolve under the harness root.
        self.vrun = f"/runs/ontology_workspace_runs/{self.run_id}"
        self.config_path = Path(config_path) if config_path else self.root / "harness.json"
        self.recursion_limit = recursion_limit
        self._log = log or (lambda msg: print(msg, flush=True))

        cfg = load_config(str(self.config_path))
        self.registry = AgentRegistry(cfg)
        self._agents: dict[str, Any] = {}

        os.environ["HARNESS_ROOT"] = str(self.root)
        os.environ["HARNESS_RUN_DIR"] = str(self.run_dir)
        for sub in ("concepts", "data", "src", "intermediate", "intermediate/web_evidence"):
            (self.run_dir / sub).mkdir(parents=True, exist_ok=True)

    # ── infrastructure ────────────────────────────────────────────────────
    def _agent(self, agent_id: str):
        if agent_id not in self._agents:
            agent_cfg = self.registry.get(agent_id)
            self._agents[agent_id] = (build_agent(agent_cfg, str(self.root), registry=self.registry), agent_cfg)
        return self._agents[agent_id]

    def _run_subagent(
        self,
        agent_id: str,
        payload: dict[str, Any],
        require: list[str] | None = None,
    ) -> dict[str, Any]:
        """Run one subagent with a JSON payload and return its parsed JSON output.

        If the agent's final message is not valid JSON (or is missing the
        ``require``-d keys), re-invoke it once with a repair instruction, matching
        the coordinator's "repair output once" rule.
        """
        agent, _ = self._agent(agent_id)
        os.environ["HARNESS_AGENT_ID"] = agent_id
        os.environ["HARNESS_RUN_DIR"] = str(self.run_dir)

        message = json.dumps(payload, ensure_ascii=False)
        self._log(f"\n--- {agent_id} INPUT ---\n{message}")
        parsed, raw, tools = self._invoke_once(agent, agent_id, message)

        if not self._contract_ok(parsed, require):
            repair = json.dumps({
                "repair_instruction": (
                    "Your previous reply was not a single valid JSON object. Return ONLY "
                    "one minified JSON object that satisfies your AGENT.md output contract. "
                    "No markdown, no ```json fences, no text outside the JSON, and no "
                    "unescaped newlines inside string values."
                ),
                "required_keys": require or [],
                "previous_output": raw[:1500],
                "original_input": payload,
            }, ensure_ascii=False)
            self._log(f"--- {agent_id} contract not satisfied; requesting repair once ---")
            parsed, raw, tools = self._invoke_once(agent, agent_id, repair)

        parsed["_raw"] = raw
        parsed["_tools"] = tools
        return parsed

    def _invoke_once(self, agent: Any, agent_id: str, message: str) -> tuple[dict[str, Any], str, list[str]]:
        result = agent.invoke(
            {"messages": [HumanMessage(content=message)]},
            config={
                "configurable": {"thread_id": f"{self.run_id}:{agent_id}"},
                "recursion_limit": self.recursion_limit,
            },
        )
        messages = result.get("messages", []) if isinstance(result, dict) else []
        final_text = self._last_ai_text(messages)
        tool_names = [
            call.get("name", "")
            for msg in messages
            for call in (getattr(msg, "tool_calls", None) or [])
        ]
        self._log(f"--- {agent_id} TOOLS: {tool_names}")
        self._log(f"--- {agent_id} OUTPUT ---\n{final_text}")
        try:
            parsed = extract_json_object(final_text)
        except Exception:
            parsed = {}
        return parsed, final_text, tool_names

    @staticmethod
    def _contract_ok(parsed: dict[str, Any], require: list[str] | None) -> bool:
        # Only steps that declare required keys are held to a JSON contract.
        # data_extractor / workspace_solver are validated via their output files,
        # so their (possibly prose) messages must not trigger a repair retry.
        if not require:
            return True
        if not isinstance(parsed, dict) or not parsed:
            return False
        return all(key in parsed for key in require)

    @staticmethod
    def _last_ai_text(messages: list[Any]) -> str:
        for msg in reversed(messages):
            if getattr(msg, "type", "") != "ai":
                continue
            if getattr(msg, "tool_calls", None):
                continue
            content = getattr(msg, "content", "") or ""
            if isinstance(content, list):
                content = "".join(
                    part.get("text", "") for part in content if isinstance(part, dict)
                )
            if content.strip():
                return content
        return ""

    def _write_json(self, rel_path: str, data: Any) -> Path:
        path = self.run_dir / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    # ── web evidence helpers ──────────────────────────────────────────────
    def _web_evidence_sources(self) -> list[dict[str, Any]]:
        web_dir = self.run_dir / "intermediate" / "web_evidence"
        sources: list[dict[str, Any]] = []
        if not web_dir.exists():
            return sources
        for path in sorted(web_dir.glob("web_*.json")):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            source_id = record.get("source_id", path.stem)
            snippet = record.get("snippet", "") or ""
            title = record.get("title", "") or ""
            sources.append({
                "source_id": source_id,
                "source_kind": "web",
                "url": record.get("url", ""),
                "title": title,
                "evidence_path": f"{self.vrun}/intermediate/web_evidence/{path.name}",
                "reason": snippet[:220],
                "chunks": [{
                    "chunk_id": f"{source_id}#0",
                    "text": f"{title}. {snippet}".strip(),
                }],
            })
        return sources

    @staticmethod
    def _sources_brief(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Drop embedded chunks for agent payloads; agents fetch text via evidence_retriever."""
        return [{k: v for k, v in s.items() if k != "chunks"} for s in sources]

    @staticmethod
    def _merge_sources(*groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
        merged: list[dict[str, Any]] = []
        seen: set[str] = set()
        for group in groups:
            for source in group:
                source_id = str(source.get("source_id", "")).strip()
                if not source_id or source_id in seen:
                    continue
                seen.add(source_id)
                merged.append(source)
        return merged

    # ── steps ─────────────────────────────────────────────────────────────
    def step_clarify(self, question: str, upload_paths: list[str]) -> dict[str, Any]:
        out = self._run_subagent("problem_clarifier", {
            "question": question,
            "upload_paths": upload_paths,
        }, require=["problem", "steps"])
        problem = str(out.get("problem", "")).strip()
        steps = out.get("steps")
        if not problem or not isinstance(steps, list) or not steps:
            raise PipelineError(f"problem_clarifier returned an invalid contract: {out.get('_raw', '')[:400]}")
        return {"problem": problem, "steps": steps}

    def step_collect_evidence(self, question: str, upload_paths: list[str]) -> dict[str, Any]:
        out = self._run_subagent("evidence_collector", {
            "question": question,
            "upload_paths": upload_paths,
        }, require=["schema_plan"])
        agent_sources = out.get("sources") if isinstance(out.get("sources"), list) else []
        upload_sources = [
            {
                "source_id": Path(p).name,
                "source_kind": "upload",
                "file_type": Path(p).suffix.lstrip(".") or "unknown",
                "file_path": p,
                "reason": "User-uploaded evidence.",
            }
            for p in upload_paths
        ]
        sources = self._merge_sources(agent_sources, upload_sources, self._web_evidence_sources())
        schema_plan = out.get("schema_plan")
        if not isinstance(schema_plan, list) or not schema_plan:
            raise PipelineError("evidence_collector did not return a schema_plan.")
        needs_web_search = bool(out.get("needs_web_search")) or any(
            s.get("source_kind") == "web" for s in sources
        )
        manifest = {
            "question": question,
            "sources": sources,
            "needs_web_search": needs_web_search,
            "handler": "schema_builder",
            "schema_plan": schema_plan,
        }
        self._write_json("intermediate/evidence_manifest.json", manifest)
        manifest_path = f"{self.vrun}/intermediate/evidence_manifest.json"
        return {"manifest_path": manifest_path, "manifest": manifest}

    def step_build_schema(
        self,
        question: str,
        manifest_path: str,
        sources: list[dict[str, Any]],
        patch: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "question": question,
            "sources": sources,
            "evidence_manifest_path": manifest_path,
        }
        if patch:
            payload.update(patch)
        out = self._run_subagent("schema_builder", payload, require=["schema_text"])
        schema_text = out.get("schema_text")
        if not isinstance(schema_text, str) or not schema_text.strip():
            raise PipelineError("schema_builder did not return schema_text.")
        result = write_draft_schema(schema_text, self.run_dir / "concepts" / "draft_schema.py")
        if not result.get("valid"):
            raise PipelineError(f"draft schema failed validation: {result.get('errors')}")
        return result

    def step_judge_schema(self, question: str, schema_path: str) -> dict[str, Any]:
        out = self._run_subagent("schema_judger", {
            "question": question,
            "schema_path": schema_path,
        }, require=["answerable"])
        if "answerable" not in out:
            raise PipelineError(f"schema_judger returned an invalid contract: {out.get('_raw', '')[:400]}")
        return out

    def step_confirm_schema(self) -> dict[str, Any]:
        draft = self.run_dir / "concepts" / "draft_schema.py"
        confirmed = self.run_dir / "concepts" / "confirmed_schema.py"
        result = confirm_schema(draft, confirmed)
        if not result.get("valid"):
            raise PipelineError(f"schema confirmation failed: {result.get('errors')}")
        return result

    def step_extract_data(self, confirmed_path: str, manifest_path: str, sources: list[dict[str, Any]]) -> dict[str, Any]:
        instances_path = self.run_dir / "data" / "instances.json"
        confirmed_local = self.run_dir / "concepts" / "confirmed_schema.py"
        outline = schema_outline(confirmed_local)
        base_payload: dict[str, Any] = {
            "schema_path": confirmed_path,
            "instances_path": f"{self.vrun}/data/instances.json",
            "schema_outline": outline,
            "sources": sources,
            "evidence_manifest_path": manifest_path,
        }
        instances = self._run_extractor_once(base_payload, instances_path)
        validation = validate_instances(instances, confirmed_local)
        if not validation.get("ok"):
            self._log(f"--- data_extractor schema mismatch: {json.dumps(validation, ensure_ascii=False)}")
            self._log("--- retrying data_extractor once with explicit correction ---")
            correction = dict(base_payload)
            correction["correction"] = {
                "message": (
                    "Your previous instances.json did not match the confirmed schema. "
                    "Use ONLY these exact entity class names as top-level keys and ONLY "
                    "their declared fields. Rewrite data/instances.json accordingly."
                ),
                "required_concepts": validation.get("schema_concepts", []),
                "unknown_concepts": validation.get("unknown_concepts", []),
                "unknown_fields": validation.get("unknown_fields", []),
                "schema_outline": outline,
            }
            instances = self._run_extractor_once(correction, instances_path)
            validation = validate_instances(instances, confirmed_local)
            if not validation.get("ok"):
                raise PipelineError(
                    "data_extractor instances.json does not conform to the confirmed schema: "
                    f"{json.dumps(validation, ensure_ascii=False)}"
                )

        result = persist_extraction(instances, confirmed_local, self.run_dir)
        if not result.get("ok"):
            raise PipelineError(f"extraction persistence failed: {result.get('errors')}")
        result["validation"] = validation
        return result

    def _run_extractor_once(self, payload: dict[str, Any], instances_path: Path) -> dict[str, Any]:
        out = self._run_subagent("data_extractor", payload)
        if not instances_path.exists():
            raise PipelineError(
                "data_extractor did not write data/instances.json. "
                f"Agent output: {out.get('_raw', '')[:400]}"
            )
        try:
            instances = json.loads(instances_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise PipelineError(f"instances.json is not valid JSON: {exc}") from exc
        if not isinstance(instances, dict) or not instances:
            raise PipelineError("instances.json must be a non-empty object keyed by entity class.")
        return instances

    def step_build_workspace(self) -> dict[str, Any]:
        data_dir = self.run_dir / "data"
        result = build_workspace(
            self.run_dir,
            self.run_dir / "concepts" / "confirmed_schema.py",
            instances_path=data_dir / "instances.json",
            facts_path=data_dir / "facts.csv",
            relations_path=data_dir / "relations.csv",
        )
        if not result.get("ok"):
            raise PipelineError(f"workspace build failed: {result.get('errors')}")
        return result

    def step_solve(self, question: str) -> dict[str, Any]:
        out = self._run_subagent("workspace_solver", {
            "question": question,
            "schema_path": f"{self.vrun}/concepts/confirmed_schema.py",
            "workspace_dir": self.vrun,
        })
        result = read_solver_result(self.run_dir)
        result["agent_answer"] = out.get("_raw", "")
        if not result.get("ok"):
            raise PipelineError(
                "workspace_solver did not produce a successful solver_result.json. "
                f"Result: {json.dumps(result, ensure_ascii=False)[:400]}"
            )
        return result

    # ── orchestration ─────────────────────────────────────────────────────
    def run(self, question: str, upload_paths: list[str] | None = None) -> dict[str, Any]:
        upload_paths = upload_paths or []
        self._log(f"=== ontology pipeline run_id={self.run_id} ===")
        self._log(f"=== question: {question}")

        report: dict[str, Any] = {"run_id": self.run_id, "run_dir": str(self.run_dir), "question": question}

        self._log("\n========== STEP 1: clarify ==========")
        clarified = self.step_clarify(question, upload_paths)
        report["clarified"] = clarified
        problem = clarified["problem"]

        self._log("\n========== STEP 2: collect evidence ==========")
        evidence = self.step_collect_evidence(problem, upload_paths)
        report["evidence"] = {"manifest_path": evidence["manifest_path"], "schema_plan": evidence["manifest"]["schema_plan"]}
        manifest_path = evidence["manifest_path"]
        sources = self._sources_brief(evidence["manifest"]["sources"])
        draft_v = f"{self.vrun}/concepts/draft_schema.py"

        self._log("\n========== STEP 3: build draft schema ==========")
        schema = self.step_build_schema(problem, manifest_path, sources)
        report["draft_schema"] = schema

        self._log("\n========== STEP 4: judge schema ==========")
        judgment = self.step_judge_schema(problem, draft_v)
        if not judgment.get("answerable"):
            self._log("\n--- schema not answerable; patching once ---")
            schema = self.step_build_schema(
                problem,
                manifest_path,
                sources,
                patch={
                    "schema_path": draft_v,
                    "missing_requirements": judgment.get("missing_requirements", []),
                },
            )
            judgment = self.step_judge_schema(problem, draft_v)
        report["judgment"] = {k: v for k, v in judgment.items() if not k.startswith("_")}

        self._log("\n========== STEP 5: confirm schema ==========")
        confirmed = self.step_confirm_schema()
        confirmed_v = f"{self.vrun}/concepts/confirmed_schema.py"
        report["confirmed_schema_path"] = confirmed["confirmed_path"]
        report["schema_form"] = schema_to_form(schema_path=confirmed["confirmed_path"])

        self._log("\n========== STEP 6: extract data ==========")
        extraction = self.step_extract_data(confirmed_v, manifest_path, sources)
        report["extraction"] = {k: v for k, v in extraction.items() if k != "_raw"}

        self._log("\n========== STEP 7: build workspace ==========")
        workspace = self.step_build_workspace()
        report["workspace"] = {"workspace_dir": workspace.get("workspace_dir"), "files": workspace.get("files")}

        self._log("\n========== STEP 8: solve ==========")
        solver = self.step_solve(problem)
        report["solver"] = solver

        self._write_json("intermediate/pipeline_report.json", report)
        self._log("\n=== pipeline complete ===")
        self._log(f"Answer: {solver.get('answer', '')}")
        return report


def run_pipeline(
    question: str,
    upload_paths: list[str] | None = None,
    harness_root: str | Path | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    root = harness_root or os.environ.get("HARNESS_ROOT") or os.getcwd()
    pipeline = OntologyPipeline(root, run_id=run_id)
    return pipeline.run(question, upload_paths or [])


def _main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Run the ontology QA pipeline directly against the backend.")
    parser.add_argument("--question", "-q", required=True, help="User question.")
    parser.add_argument("--upload", "-u", action="append", default=[], help="Upload file path (repeatable).")
    parser.add_argument("--run-id", default=None, help="Explicit run id (default: timestamped).")
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[2]), help="Harness root directory.")
    args = parser.parse_args()

    try:
        run_pipeline(args.question, args.upload, harness_root=args.root, run_id=args.run_id)
    except PipelineError as exc:
        print(f"\nPIPELINE FAILED: {exc}", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
