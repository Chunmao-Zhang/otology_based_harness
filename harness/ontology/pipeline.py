"""Pure-LLM driver for the ontology QA workflow.

The whole workflow is orchestrated by the `ontology_coordinator` LLM, which
delegates to the six subagent LLMs with the deepagents `task` tool. There is no
Python state machine, no hardcoded step routing, no fallback/repair retries and
no auto-chaining here. This module only:

1. sets up the run directory + environment, then
2. invokes the coordinator once with the user question, then
3. reads back whatever the agents wrote (``intermediate/solver_result.json`` and
   the other run artifacts) to assemble a report.

The deterministic, domain-agnostic backend operations the workflow needs (schema
validation/persistence, dataset derivation, workspace build) are exposed to the
LLM agents as ontology tools (see ``otology_agent_workspace/tools/ontology_backend.py``)
and are invoked *by the agents*, never auto-routed by this driver.
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
from harness.ontology.solver import read_solver_result

LogFn = Callable[[str], None]

COORDINATOR_ID = "ontology_coordinator"


class OntologyPipeline:
    """Thin driver that runs the coordinator-LLM-orchestrated ontology workflow."""

    def __init__(
        self,
        harness_root: str | Path,
        run_id: str | None = None,
        config_path: str | Path | None = None,
        log: LogFn | None = None,
        recursion_limit: int = 300,
    ) -> None:
        self.root = Path(harness_root).resolve()
        self.run_id = run_id or datetime.now().strftime("run_%Y%m%d_%H%M%S")
        self.run_dir = self.root / "runs" / "ontology_workspace_runs" / self.run_id
        # Virtual (root-relative) run path passed to the coordinator so subagent
        # write_file / execute_code / ontology tools all resolve under harness root.
        self.vrun = f"/runs/ontology_workspace_runs/{self.run_id}"
        self.config_path = Path(config_path) if config_path else self.root / "harness.json"
        self.recursion_limit = recursion_limit
        self._log = log or (lambda msg: print(msg, flush=True))

        cfg = load_config(str(self.config_path))
        self.registry = AgentRegistry(cfg)

        os.environ["HARNESS_ROOT"] = str(self.root)
        os.environ["HARNESS_RUN_DIR"] = str(self.run_dir)
        os.environ["HARNESS_AGENT_ID"] = COORDINATOR_ID
        for sub in ("concepts", "data", "src", "intermediate", "intermediate/web_evidence"):
            (self.run_dir / sub).mkdir(parents=True, exist_ok=True)

    # ── infrastructure ────────────────────────────────────────────────────
    def _initial_message(self, question: str, upload_paths: list[str]) -> str:
        inputs = {
            "question": question,
            "upload_paths": upload_paths,
            "workspace_dir": self.vrun,
            "run_id": self.run_id,
            "autonomous": True,
        }
        return (
            "You are running in autonomous backend mode: no human is available to "
            "confirm the gates, so you must drive the COMPLETE ontology workflow end "
            "to end yourself by delegating to your subagents with the `task` tool, "
            "and only then give the final answer.\n\n"
            "Follow your Required Workflow in order. Do not pause to ask the user to "
            "confirm the clarified problem or the schema; proceed automatically through "
            "every step. Make sure `workspace_solver` has written "
            f"`{self.vrun}/intermediate/solver_result.json` before you produce the final "
            "answer.\n\nInputs:\n" + json.dumps(inputs, ensure_ascii=False, indent=2)
        )

    @staticmethod
    def _tool_call_names(msg: Any) -> list[dict[str, Any]]:
        calls = getattr(msg, "tool_calls", None) or []
        out = []
        for call in calls:
            out.append({"name": call.get("name", ""), "args": call.get("args", {})})
        return out

    def _stream(self, agent: Any, message: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Run the coordinator and stream a readable trace of agent/tool activity."""
        config = {
            "configurable": {"thread_id": f"{self.run_id}:{COORDINATOR_ID}"},
            "recursion_limit": self.recursion_limit,
        }
        trace: list[dict[str, Any]] = []
        last_values: dict[str, Any] = {}
        seen_ids: set[str] = set()

        for event in agent.stream(
            {"messages": [HumanMessage(content=message)]},
            config=config,
            stream_mode=["values"],
            subgraphs=True,
        ):
            if not isinstance(event, tuple) or len(event) != 3:
                continue
            ns, mode, data = event
            depth = "coordinator" if ns == () else f"sub:{'/'.join(str(p) for p in ns)}"
            if mode != "values" or not isinstance(data, dict):
                continue
            if ns == ():
                last_values = data
            for msg in data.get("messages", []):
                mid = getattr(msg, "id", "") or ""
                if mid and mid in seen_ids:
                    continue
                if mid:
                    seen_ids.add(mid)
                mtype = getattr(msg, "type", "")
                name = getattr(msg, "name", "") or ""
                if mtype == "ai":
                    for call in self._tool_call_names(msg):
                        if call["name"] == "task":
                            sub = call["args"].get("subagent_type", "?")
                            self._log(f"  [{depth}] task -> {sub}")
                            trace.append({"scope": depth, "delegate": sub})
                        else:
                            self._log(f"  [{depth}/{name}] tool: {call['name']}")
                            trace.append({"scope": depth, "agent": name, "tool": call["name"]})
                    text = self._text(msg)
                    if text:
                        self._log(f"  [{depth}/{name}] says: {text[:300]}")
                elif mtype == "tool":
                    content = self._text(msg)
                    self._log(f"  [{depth}] <- {name}: {content[:300]}")
        return last_values, trace

    @staticmethod
    def _text(msg: Any) -> str:
        content = getattr(msg, "content", "") or ""
        if isinstance(content, list):
            content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
        return content.strip() if isinstance(content, str) else ""

    @staticmethod
    def _last_ai_text(messages: list[Any]) -> str:
        for msg in reversed(messages):
            if getattr(msg, "type", "") != "ai":
                continue
            if getattr(msg, "tool_calls", None):
                continue
            content = getattr(msg, "content", "") or ""
            if isinstance(content, list):
                content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
            if isinstance(content, str) and content.strip():
                return content.strip()
        return ""

    def _write_json(self, rel_path: str, data: Any) -> Path:
        path = self.run_dir / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def _read_json(self, rel_path: str) -> Any:
        path = self.run_dir / rel_path
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None

    # ── orchestration ─────────────────────────────────────────────────────
    def run(self, question: str, upload_paths: list[str] | None = None) -> dict[str, Any]:
        upload_paths = upload_paths or []
        self._log(f"=== ontology coordinator run_id={self.run_id} ===")
        self._log(f"=== question: {question}")

        agent_cfg = self.registry.get(COORDINATOR_ID)
        agent = build_agent(agent_cfg, str(self.root), registry=self.registry)

        message = self._initial_message(question, upload_paths)
        self._log("\n========== coordinator (autonomous) ==========")
        values, trace = self._stream(agent, message)

        messages = values.get("messages", []) if isinstance(values, dict) else []
        final_answer = self._last_ai_text(messages)
        delegated = [entry["delegate"] for entry in trace if entry.get("delegate")]

        solver = read_solver_result(self.run_dir)

        report: dict[str, Any] = {
            "run_id": self.run_id,
            "run_dir": str(self.run_dir),
            "question": question,
            "delegated_subagents": delegated,
            "coordinator_final_answer": final_answer,
            "evidence_manifest": self._read_json("intermediate/evidence_manifest.json"),
            "extraction_report": self._read_json("intermediate/extraction_report.json"),
            "solver_result": solver,
        }
        self._write_json("intermediate/coordinator_trace.json", trace)
        self._write_json("intermediate/pipeline_report.json", report)

        self._log("\n=== coordinator run complete ===")
        self._log(f"Delegated subagents (in order): {delegated}")
        self._log(f"Solver ok: {solver.get('ok')}")
        self._log(f"Final answer:\n{final_answer}")
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

    parser = argparse.ArgumentParser(
        description="Run the ontology QA workflow via the pure-LLM coordinator (no state machine)."
    )
    parser.add_argument("--question", "-q", required=True, help="User question.")
    parser.add_argument("--upload", "-u", action="append", default=[], help="Upload file path (repeatable).")
    parser.add_argument("--run-id", default=None, help="Explicit run id (default: timestamped).")
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[2]), help="Harness root directory.")
    args = parser.parse_args()

    report = run_pipeline(args.question, args.upload, harness_root=args.root, run_id=args.run_id)
    solver = report.get("solver_result", {})
    return 0 if solver.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(_main())
