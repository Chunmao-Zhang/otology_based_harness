"""Path helpers for ontology workspace tools."""

from __future__ import annotations

import os
from pathlib import Path

ONTOLOGY_WORKSPACE = Path("otology_agent_workspace")
ONTOLOGY_RUNS = Path("runs/ontology_workspace_runs")
ONTOLOGY_FIXTURES = Path("test_data/ontology")
ONTOLOGY_EVALS = Path("evals/ontology")
SCHEMA_UTILS = Path("otology_agent_workspace/utils")


def harness_root() -> Path:
    return Path(os.environ.get("HARNESS_ROOT", os.getcwd())).resolve()


def ontology_run_dir() -> Path:
    run_env = os.environ.get("HARNESS_RUN_DIR", "")
    run_id = Path(run_env).name if run_env else "manual"
    return harness_root() / ONTOLOGY_RUNS / run_id


def _canonical_relative(value: str) -> str:
    """Map old ontology workspace paths to the current root-level layout."""
    text = value
    root = str(harness_root())
    if text == root:
        return ""
    if text.startswith(root + "/"):
        text = text[len(root) + 1:]
    if text.startswith("runs/ontology/"):
        return "runs/ontology_workspace_runs/" + text.removeprefix("runs/ontology/")
    if text.startswith("runs/harness/"):
        return "runs/harness_conversation_logs/" + text.removeprefix("runs/harness/")
    if text.startswith("/workspaces/"):
        text = text.removeprefix("/workspaces/")
    text = text.lstrip("/")

    for workspace_name in ("ontology_harness", "otology_agent_workspace"):
        prefix = f"{workspace_name}/runs/"
        if text.startswith(prefix):
            return "runs/ontology_workspace_runs/" + text.removeprefix(prefix)
        prefix = f"{workspace_name}/fixtures/schemas/"
        if text.startswith(prefix):
            return "otology_agent_workspace/utils/" + text.removeprefix(prefix)
        prefix = f"{workspace_name}/utils/schemas/"
        if text.startswith(prefix):
            return "otology_agent_workspace/utils/" + text.removeprefix(prefix)
        prefix = f"{workspace_name}/utils/"
        if text.startswith(prefix):
            return "otology_agent_workspace/utils/" + text.removeprefix(prefix)
        prefix = f"{workspace_name}/fixtures/"
        if text.startswith(prefix):
            return "test_data/ontology/" + text.removeprefix(prefix)
        prefix = f"{workspace_name}/test_data/"
        if text.startswith(prefix):
            return "test_data/ontology/" + text.removeprefix(prefix)
        prefix = f"{workspace_name}/evals/"
        if text.startswith(prefix):
            return "evals/ontology/" + text.removeprefix(prefix)

    if text.startswith("runs/ontology/"):
        return "runs/ontology_workspace_runs/" + text.removeprefix("runs/ontology/")
    if text.startswith("runs/harness/"):
        return "runs/harness_conversation_logs/" + text.removeprefix("runs/harness/")
    if text.startswith("otology_agent_workspace/utils/schemas/"):
        return "otology_agent_workspace/utils/" + text.removeprefix("otology_agent_workspace/utils/schemas/")
    if (
        text.startswith("runs/ontology_workspace_runs/")
        or text.startswith("test_data/ontology/")
        or text.startswith("evals/ontology/")
        or text.startswith("otology_agent_workspace/utils/")
    ):
        return text

    return text


def resolve_path(value: str | Path) -> Path:
    text = str(value)
    path = Path(text)
    canonical_relative = _canonical_relative(text)
    if canonical_relative != text.lstrip("/"):
        return harness_root() / canonical_relative
    if path.is_absolute() and not text.startswith("/workspaces/"):
        return path
    return harness_root() / canonical_relative


def normalize_output_path(value: str | Path, default_relative: str) -> Path:
    text = str(value or "").strip()
    if not text:
        return ontology_run_dir() / default_relative
    resolved = resolve_path(text)
    runs_root = harness_root() / ONTOLOGY_RUNS
    try:
        rel = resolved.relative_to(runs_root)
        parts = rel.parts
        if default_relative == "":
            if len(parts) == 1 and "." not in parts[0]:
                return resolved
            return ontology_run_dir()
        if len(parts) == 1 and "." not in parts[0]:
            return resolved / default_relative
        if (
            len(parts) < 2
            or parts[0] == "manual"
            or parts[0] in {"concepts", "data", "intermediate", "src"}
            or "." in parts[0]
            or parts[1] not in {"concepts", "data", "intermediate", "src"}
        ):
            return ontology_run_dir() / default_relative
        return resolved
    except ValueError:
        return ontology_run_dir() / default_relative
