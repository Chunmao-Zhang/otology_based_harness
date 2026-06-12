"""execute_code 工具

执行指定路径的 Python 脚本，支持传入命令行参数，返回执行结果。
Agent 先按当前 AGENT.md 的路径规则写入代码，再用本工具执行。
也可以直接执行 skills 目录下已有的脚本，通过 args 传入参数。

路径约定：与 deepagents 的 FilesystemBackend(virtual_mode=True) 一致，
输入的 file_path 是虚拟绝对路径（如 /workspaces/main/code/fib.py），
实际映射到 harness_root 下的对应相对路径。
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from pathlib import Path

from langchain_core.tools import tool

MAX_OUTPUT_LENGTH = 10000

# harness_root 在运行时通过环境变量注入
_HARNESS_ROOT_ENV = "HARNESS_ROOT"


def _canonical_relative(value: str) -> str:
    relative = value.removeprefix("/workspaces/").lstrip("/")
    for workspace_name in ("ontology_harness", "otology_agent_workspace"):
        prefix = f"{workspace_name}/runs/"
        if relative.startswith(prefix):
            return "runs/ontology_workspace_runs/" + relative.removeprefix(prefix)
        prefix = f"{workspace_name}/fixtures/schemas/"
        if relative.startswith(prefix):
            return "otology_agent_workspace/utils/" + relative.removeprefix(prefix)
        prefix = f"{workspace_name}/utils/schemas/"
        if relative.startswith(prefix):
            return "otology_agent_workspace/utils/" + relative.removeprefix(prefix)
        prefix = f"{workspace_name}/utils/"
        if relative.startswith(prefix):
            return "otology_agent_workspace/utils/" + relative.removeprefix(prefix)
        prefix = f"{workspace_name}/fixtures/"
        if relative.startswith(prefix):
            return "test_data/ontology/" + relative.removeprefix(prefix)
        prefix = f"{workspace_name}/test_data/"
        if relative.startswith(prefix):
            return "test_data/ontology/" + relative.removeprefix(prefix)
    if relative.startswith("runs/ontology/"):
        return "runs/ontology_workspace_runs/" + relative.removeprefix("runs/ontology/")
    if relative.startswith("runs/harness/"):
        return "runs/harness_conversation_logs/" + relative.removeprefix("runs/harness/")
    if relative.startswith("otology_agent_workspace/utils/schemas/"):
        return "otology_agent_workspace/utils/" + relative.removeprefix("otology_agent_workspace/utils/schemas/")
    return relative


def _resolve_virtual_path(virtual_path: str) -> Path:
    """将虚拟绝对路径解析为真实文件系统路径"""
    harness_root = os.environ.get(_HARNESS_ROOT_ENV, os.getcwd())
    return Path(harness_root) / _canonical_relative(virtual_path)


@tool
def execute_code(file_path: str, script_args: str = "", timeout: int = 120) -> str:
    """Execute a Python script at the given path and return stdout/stderr.

    Use write_file to save your code first, then call this tool to run it.
    The file_path should be an absolute virtual path (e.g. /workspaces/main/code/script.py).

    Args:
        file_path: Absolute path to the Python script (e.g. /workspaces/main/code/my_script.py).
        script_args: Command-line arguments to pass to the script (e.g. "--input /path/a.json --output /path/b.json").
        timeout: Maximum execution time in seconds (default 120).
    """
    if not file_path.startswith("/"):
        return json.dumps(
            {"status": "error", "error": "file_path must start with /"},
            ensure_ascii=False,
        )

    real_path = _resolve_virtual_path(file_path)

    if not real_path.exists():
        return json.dumps(
            {"status": "error", "error": f"File not found: {file_path} (resolved to {real_path})"},
            ensure_ascii=False,
        )

    if real_path.suffix != ".py":
        return json.dumps(
            {"status": "error", "error": "Only .py files are supported"},
            ensure_ascii=False,
        )

    # Build command: python3 <script> [args...]
    cmd = ["python3", str(real_path)]
    if script_args:
        # Resolve virtual paths in args to real paths
        resolved_args = _resolve_args(script_args)
        cmd.extend(shlex.split(resolved_args))

    try:
        result = subprocess.run(
            cmd,
            cwd=str(real_path.parent),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return json.dumps(
            {"status": "error", "error": f"Execution timed out ({timeout}s)"},
            ensure_ascii=False,
        )
    except Exception as e:
        return json.dumps(
            {"status": "error", "error": f"Execution failed: {e}"},
            ensure_ascii=False,
        )

    stdout = result.stdout
    if len(stdout) > MAX_OUTPUT_LENGTH:
        stdout = stdout[:MAX_OUTPUT_LENGTH] + "\n... [truncated]"

    if result.returncode != 0:
        stderr = result.stderr[:3000] if result.stderr else ""
        return json.dumps(
            {
                "status": "error",
                "returncode": result.returncode,
                "stdout": stdout,
                "stderr": stderr,
            },
            ensure_ascii=False,
        )

    return json.dumps({"status": "success", "output": stdout}, ensure_ascii=False)


def _resolve_args(args: str) -> str:
    """Resolve virtual paths in args string to real filesystem paths.

    Any token that starts with /workspaces/ will be converted to the
    real path under harness_root.
    """
    harness_root = os.environ.get(_HARNESS_ROOT_ENV, os.getcwd())
    tokens = shlex.split(args)
    resolved = []
    for token in tokens:
        if token.startswith("/workspaces/"):
            resolved.append(str(Path(harness_root) / _canonical_relative(token)))
        elif (
            token.startswith("/runs/ontology_workspace_runs/")
            or token.startswith("/runs/ontology/")
            or token.startswith("/test_data/ontology/")
            or token.startswith("/otology_agent_workspace/utils/")
        ):
            resolved.append(str(Path(harness_root) / _canonical_relative(token)))
        else:
            resolved.append(token)
    # Re-quote tokens that contain spaces
    return " ".join(shlex.quote(t) for t in resolved)
