"""Agent Loop

核心模块：根据 AgentConfig 构建 DeepAgents 实例并执行。
职责：
1. 从 config 构建 ChatOpenAI model
2. 从 workspace 读取 AGENT.md 作为 system_prompt
3. 调用 create_deep_agent() 生成 agent
4. invoke 执行并返回结果
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI

from deepagents import create_deep_agent
from deepagents.backends.local_shell import LocalShellBackend
from deepagents.backends.protocol import ExecuteResponse
from deepagents.middleware.summarization import create_summarization_tool_middleware
from deepagents.profiles.harness.harness_profiles import (
    GeneralPurposeSubagentProfile,
    HarnessProfile,
    register_harness_profile,
)
from langgraph.checkpoint.sqlite import SqliteSaver

from harness.config.schema import AgentConfig, HarnessConfig, ModelConfig
from harness.agents.registry import AgentRegistry
from harness.agents.deepseek_model import DeepSeekChatOpenAI
from harness.agents.friday_model import FridayChatOpenAI
from harness.agents.model101_model import Model101ChatOpenAI
from harness.runtime.context import RuntimeContext
from harness.tools.registry import get_tools_for_agent
from harness.harness_prompt import load_harness_prompt
from harness.middleware import MicroCompactMiddleware
from harness.middleware.tool_filter import ToolExecutionFilterMiddleware, ToolFilterMiddleware

logger = logging.getLogger(__name__)


class HarnessShellBackend(LocalShellBackend):
    """LocalShellBackend subclass that resolves /workspaces/... virtual paths in execute commands.

    This ensures models can use the same absolute virtual path format (/workspaces/...)
    in both file tools (read_file, write_file, ls) and shell commands (execute).
    """

    allow_execute: bool = True

    def execute(self, command: str, *, timeout: int | None = None):
        if not self.allow_execute:
            return ExecuteResponse(
                output="Error: shell execution is disabled for this agent. Use KBQA tools and execute_code.",
                exit_code=1,
                truncated=False,
            )
        # Replace common virtual paths with real absolute paths.
        resolved_command = command
        for workspace_name in ("ontology_harness", "otology_agent_workspace"):
            resolved_command = resolved_command.replace(f"/workspaces/{workspace_name}/runs/", f"{self.cwd}/runs/ontology_workspace_runs/")
            resolved_command = resolved_command.replace(f"/workspaces/{workspace_name}/fixtures/schemas/", f"{self.cwd}/otology_agent_workspace/utils/")
            resolved_command = resolved_command.replace(f"/workspaces/{workspace_name}/utils/schemas/", f"{self.cwd}/otology_agent_workspace/utils/")
            resolved_command = resolved_command.replace(f"/workspaces/{workspace_name}/utils/", f"{self.cwd}/otology_agent_workspace/utils/")
            resolved_command = resolved_command.replace(f"/workspaces/{workspace_name}/fixtures/", f"{self.cwd}/test_data/ontology/")
            resolved_command = resolved_command.replace(f"/workspaces/{workspace_name}/test_data/", f"{self.cwd}/test_data/ontology/")
            resolved_command = resolved_command.replace(f"/workspaces/{workspace_name}/evals/", f"{self.cwd}/evals/ontology/")
        resolved_command = resolved_command.replace("/runs/ontology_workspace_runs/", f"{self.cwd}/runs/ontology_workspace_runs/")
        resolved_command = resolved_command.replace("/runs/ontology/", f"{self.cwd}/runs/ontology_workspace_runs/")
        resolved_command = resolved_command.replace("/runs/harness/", f"{self.cwd}/runs/harness_conversation_logs/")
        resolved_command = resolved_command.replace("/test_data/ontology/", f"{self.cwd}/test_data/ontology/")
        resolved_command = resolved_command.replace("/otology_agent_workspace/utils/schemas/", f"{self.cwd}/otology_agent_workspace/utils/")
        resolved_command = resolved_command.replace("/otology_agent_workspace/utils/", f"{self.cwd}/otology_agent_workspace/utils/")
        resolved_command = resolved_command.replace("/evals/ontology/", f"{self.cwd}/evals/ontology/")
        resolved_command = resolved_command.replace("/workspaces/ontology_harness/", f"{self.cwd}/otology_agent_workspace/")
        resolved_command = resolved_command.replace("/workspaces/otology_agent_workspace/", f"{self.cwd}/otology_agent_workspace/")
        resolved_command = resolved_command.replace("/workspaces/", f"{self.cwd}/")
        return super().execute(resolved_command, timeout=timeout)


def _resolve_path(path: str, base: str) -> Path:
    """解析路径：绝对路径直接使用，相对路径基于 base 拼接"""
    p = Path(path)
    if p.is_absolute():
        return p
    return Path(base) / path


def _resolve_skills_paths(
    workspace_dir: str,
    skills_filter: list[str] | None,
) -> list[str] | None:
    """解析 skills 路径

    规则：
    - skills_filter 为空列表 [] 或 None：加载 workspace/skills/ 下所有 skill
    - skills_filter 非空：只加载指定的 skill 子目录

    每个 skill 条目可以是：
    - 纯名称（如 "crm-monitor"）：解析为 workspace/skills/<name>/
    - 绝对路径（如 "/path/to/skill/"）：直接使用
    """
    skills_dir = Path(workspace_dir) / "skills"

    if not skills_filter:
        # 未指定或空列表：加载整个 skills 目录
        if skills_dir.exists():
            return [str(skills_dir)]
        return None

    # 指定了具体 skill：逐个解析路径
    paths = []
    for skill in skills_filter:
        skill_path = Path(skill)
        if skill_path.is_absolute():
            # 绝对路径直接使用
            if skill_path.exists():
                paths.append(str(skill_path))
            else:
                logger.warning("Skill path not found: %s", skill_path)
        else:
            # 相对名称：在 workspace/skills/ 下查找
            resolved = skills_dir / skill
            if resolved.exists():
                paths.append(str(resolved))
            else:
                logger.warning("Skill '%s' not found in %s", skill, skills_dir)

    return paths if paths else None


MODEL_WRAPPERS = {
    "deepseek": DeepSeekChatOpenAI,
    "friday": FridayChatOpenAI,
    "model101": Model101ChatOpenAI,
}

_KBQA_GENERAL_EXCLUDED_DEEPAGENT_TOOLS = frozenset({
    "ls",
    "glob",
    "grep",
    "execute",
    "write_file",
    "edit_file",
    "write_todos",
})
_KBQA_GENERAL_PROFILE_REGISTERED = False
_ONTOLOGY_DEEPSEEK_PROFILE_REGISTERED: set[str] = set()


def _register_ontology_model_profile(model_cfg: ModelConfig) -> None:
    """Apply harness-level runtime defaults for ontology DeepSeek agents.

    DeepAgents auto-adds a `general-purpose` subagent unless a harness profile
    disables it. Ontology harnesses need only the explicitly declared
    subagents from harness.json; the extra default agent can bypass the
    coordinator's workflow gates and tool contracts.
    """

    if (model_cfg.provider or "").lower() != "deepseek" or not model_cfg.model_id:
        return

    # DeepSeek uses an OpenAI-compatible ChatOpenAI client, so DeepAgents sees
    # the resolved provider as `openai`. Register both keys to cover direct
    # string specs and pre-built ChatOpenAI model instances.
    keys = {
        f"deepseek:{model_cfg.model_id}",
        f"openai:{model_cfg.model_id}",
    }
    profile = HarnessProfile(
        general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False),
    )
    for key in keys:
        if key in _ONTOLOGY_DEEPSEEK_PROFILE_REGISTERED:
            continue
        register_harness_profile(key, profile)
        _ONTOLOGY_DEEPSEEK_PROFILE_REGISTERED.add(key)


def _model_wrapper(model_cfg: ModelConfig) -> type[ChatOpenAI]:
    """Return a provider-specific wrapper only when one is explicitly registered."""

    provider = (model_cfg.provider or "").lower()
    return MODEL_WRAPPERS.get(provider, ChatOpenAI)


def _build_model(model_cfg: ModelConfig) -> ChatOpenAI:
    """根据 ModelConfig 构建 LLM 实例

    Provider-specific quirks must live in a matching wrapper. Do not infer
    special behavior from model names or base URLs because that applies one
    provider's workaround to unrelated OpenAI-compatible endpoints.
    """
    model_cls = _model_wrapper(model_cfg)
    timeout = float(os.environ.get("HARNESS_MODEL_TIMEOUT_SECONDS", "180"))
    kwargs = {}
    if model_cfg.response_format:
        kwargs["model_kwargs"] = {"response_format": model_cfg.response_format}
    return model_cls(
        api_key=model_cfg.api_key,
        base_url=model_cfg.base_url,
        model=model_cfg.model_id,
        temperature=model_cfg.temperature,
        max_tokens=model_cfg.max_tokens,
        timeout=timeout,
        stream_chunk_timeout=timeout,
        **kwargs,
    )


def _load_prompt(agent_cfg: AgentConfig, workspace_dir: str, harness_root: str) -> str | None:
    """加载 agent 的 system prompt

    规则：
    - agent_cfg.prompt 非空：从指定路径加载文件内容作为完整 prompt（替代默认）
    - agent_cfg.prompt 为空：使用 AGENT.md + harness prompt（默认行为）

    prompt 路径支持：
    - 绝对路径：直接读取
    - 相对路径：基于 harness_root 解析

    继承逻辑：
    - defaults.prompt 设置了全局 prompt → 所有 agent 默认使用
    - agent 级别的 prompt 可覆盖 defaults
    - 都不设置 → 使用 AGENT.md + harness prompt
    """
    prompt_path_str = agent_cfg.prompt or ""

    if prompt_path_str:
        # 自定义 prompt 文件：替代默认的 AGENT.md + harness prompt
        prompt_path = _resolve_path(prompt_path_str, harness_root)
        if prompt_path.exists():
            content = prompt_path.read_text(encoding="utf-8").strip()
            # 支持 {agent_id} 变量替换
            content = content.replace("{agent_id}", agent_cfg.id)
            return content if content else None
        else:
            logger.warning("Prompt file not found: %s", prompt_path)
            return None

    # 默认行为：AGENT.md + harness prompt
    agent_prompt = _load_agent_md(workspace_dir)
    harness_tools_prompt = load_harness_prompt(agent_id=agent_cfg.id)

    prompt_parts = []
    if agent_prompt:
        prompt_parts.append(agent_prompt)
    if harness_tools_prompt:
        prompt_parts.append(harness_tools_prompt)

    return "\n\n".join(prompt_parts) if prompt_parts else None


def _load_agent_md(workspace_dir: str) -> str | None:
    """从 workspace 读取 AGENT.md"""
    agent_path = Path(workspace_dir) / "AGENT.md"
    if agent_path.exists():
        content = agent_path.read_text(encoding="utf-8").strip()
        return content if content else None
    return None


def _build_subagent_specs(
    agent_cfg: AgentConfig,
    harness_root: str,
    registry: AgentRegistry | None,
) -> list[dict]:
    """将 agent_cfg.subagents 转换为 DeepAgents SubAgent spec 列表"""
    if not agent_cfg.subagents or registry is None:
        return []

    specs = []
    for sub_id in agent_cfg.subagents:
        sub_cfg = registry.get(sub_id)
        sub_workspace = str(_resolve_path(sub_cfg.workspace, harness_root))

        # 子 agent 的 system prompt
        sub_prompt = _load_prompt(sub_cfg, sub_workspace, harness_root)
        if not sub_prompt:
            sub_prompt = f"You are {sub_cfg.name}."

        # 子 agent 的 tools
        sub_tools = get_tools_for_agent(
            sub_cfg,
            workspace_dir=sub_workspace,
            harness_root=harness_root,
        )

        # 子 agent 的 skills 路径（支持过滤）
        sub_skills = _resolve_skills_paths(sub_workspace, sub_cfg.skills or None)

        spec = {
            "name": sub_cfg.id,
            "description": sub_cfg.description or sub_cfg.name,
            "system_prompt": sub_prompt,
            "tools": sub_tools,
            "middleware": [
                ToolFilterMiddleware(
                    allow=sub_cfg.tools.allow if sub_cfg.tools else ["*"],
                    deny=sub_cfg.tools.deny if sub_cfg.tools else [],
                ),
                ToolExecutionFilterMiddleware(
                    allow=sub_cfg.tools.allow if sub_cfg.tools else ["*"],
                    deny=sub_cfg.tools.deny if sub_cfg.tools else [],
                ),
            ],
        }
        if sub_skills:
            spec["skills"] = sub_skills

        # 如果子 agent 配置了不同的模型
        if sub_cfg.model and sub_cfg.model.model_id:
            spec["model"] = _build_model(sub_cfg.model)

        specs.append(spec)

    return specs


def build_agent(
    agent_cfg: AgentConfig,
    harness_root: str,
    registry: AgentRegistry | None = None,
    tools: list | None = None,
):
    """根据 AgentConfig 构建一个可执行的 DeepAgents 实例

    Args:
        agent_cfg: 已解析（继承 defaults 后）的 agent 配置
        harness_root: harness 项目根目录
        registry: AgentRegistry，用于查找子 agent 配置（有 subagents 时必传）
        tools: 额外的自定义工具列表（追加到 registry 工具之后）

    Returns:
        CompiledStateGraph（可 invoke 的 agent）
    """
    workspace_dir = str(_resolve_path(agent_cfg.workspace, harness_root))

    # 1. Model
    _register_ontology_model_profile(agent_cfg.model)
    model = _build_model(agent_cfg.model)
    if agent_cfg.id == "deepagents_kbqa_general":
        global _KBQA_GENERAL_PROFILE_REGISTERED
        if not _KBQA_GENERAL_PROFILE_REGISTERED:
            register_harness_profile(
                f"openai:{agent_cfg.model.model_id}",
                HarnessProfile(excluded_tools=_KBQA_GENERAL_EXCLUDED_DEEPAGENT_TOOLS),
            )
            _KBQA_GENERAL_PROFILE_REGISTERED = True

    # 2. System prompt
    final_prompt = _load_prompt(agent_cfg, workspace_dir, harness_root)

    # 3. Backend（HarnessShellBackend 支持 execute 工具，virtual_mode=True 让 /workspaces/... 映射到 harness_root 下的相对路径）
    abs_root = str(Path(harness_root).resolve())
    backend = HarnessShellBackend(root_dir=abs_root, virtual_mode=True, inherit_env=True)
    if agent_cfg.id == "deepagents_kbqa_general":
        backend.allow_execute = False

    # 4. Tools: registry 过滤 + 额外工具
    agent_tools = get_tools_for_agent(
        agent_cfg,
        workspace_dir=workspace_dir,
        harness_root=harness_root,
    )
    if tools:
        agent_tools.extend(tools)

    # 5. Skills 路径（支持过滤：空列表加载全部，非空只加载指定的）
    skills_paths = _resolve_skills_paths(workspace_dir, agent_cfg.skills or None)

    # 6. Memory 路径
    memory_file = Path(workspace_dir) / "memory" / "MEMORY.md"
    memory_paths = [str(memory_file)] if memory_file.exists() else None

    # 7. SubAgents
    subagents = _build_subagent_specs(agent_cfg, harness_root, registry)

    # 8. Middleware: Tool-Filter + Tool-Execution-Filter + Micro-Compact + Manual-Compact
    tool_filter = ToolFilterMiddleware(
        allow=agent_cfg.tools.allow if agent_cfg.tools else ["*"],
        deny=agent_cfg.tools.deny if agent_cfg.tools else [],
    )
    tool_exec_filter = ToolExecutionFilterMiddleware(
        allow=agent_cfg.tools.allow if agent_cfg.tools else ["*"],
        deny=agent_cfg.tools.deny if agent_cfg.tools else [],
    )
    micro_compact = MicroCompactMiddleware(
        keep_turns=agent_cfg.context.keep_turns if agent_cfg.context else 3,
    )
    manual_compact = create_summarization_tool_middleware(model, backend)

    # 9. Checkpointer（sqlite 持久化，支持中断恢复）
    import sqlite3
    checkpoints_dir = Path(harness_root) / "checkpoints"
    checkpoints_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(checkpoints_dir / "state.sqlite"), check_same_thread=False)
    checkpointer = SqliteSaver(conn)

    # 10. 构建 agent
    agent = create_deep_agent(
        model=model,
        tools=agent_tools,
        system_prompt=final_prompt,
        backend=backend,
        skills=skills_paths,
        memory=memory_paths,
        subagents=subagents if subagents else None,
        middleware=[tool_filter, tool_exec_filter, micro_compact, manual_compact],
        checkpointer=checkpointer,
        name=agent_cfg.id,
    )

    logger.info(
        "Built agent '%s' (model=%s, tools=%d, subagents=%d)",
        agent_cfg.id, agent_cfg.model.model_id, len(agent_tools), len(subagents),
    )
    return agent


def run_agent(
    agent_cfg: AgentConfig,
    harness_root: str,
    message: str,
    registry: AgentRegistry | None = None,
    tools: list | None = None,
    run_dir: str | None = None,
    thread_id: str | None = None,
) -> dict[str, Any]:
    """构建 agent 并执行一次对话

    Args:
        agent_cfg: agent 配置
        harness_root: 项目根目录
        message: 用户输入
        registry: AgentRegistry（有 subagents 时需要）
        tools: 额外工具
        run_dir: 当前 run 的输出目录（用于工具结果落盘）
        thread_id: 多轮对话 thread ID（相同 ID 会恢复之前的上下文）

    Returns:
        agent invoke 的完整结果 dict
    """
    import os
    os.environ["HARNESS_ROOT"] = str(Path(harness_root).resolve())
    os.environ["HARNESS_AGENT_ID"] = agent_cfg.id

    if run_dir:
        os.environ["HARNESS_RUN_DIR"] = str(Path(run_dir).resolve())

    agent = build_agent(agent_cfg, harness_root, registry=registry, tools=tools)
    result = agent.invoke(
        {"messages": [HumanMessage(content=message)]},
        config={"configurable": {"thread_id": thread_id or "default"}},
    )
    return result


def stream_agent(
    agent_cfg: AgentConfig,
    harness_root: str,
    message: str,
    registry: AgentRegistry | None = None,
    tools: list | None = None,
    run_dir: str | None = None,
    thread_id: str | None = None,
    on_message=None,
    on_subagent_event=None,
) -> dict[str, Any]:
    """构建 agent 并以流式方式执行，实时输出主/子 agent 的每条消息和工具调用。

    使用 stream_events（而非 stream(values)）以便捕获嵌套 subagent 内部的
    LLM 调用、工具调用等事件，不必等到 subagent 完成才输出。

    Args:
        agent_cfg: agent 配置
        harness_root: 项目根目录
        message: 用户输入
        registry: AgentRegistry（有 subagents 时需要）
        tools: 额外工具
        run_dir: 当前 run 的输出目录
        thread_id: 多轮对话 thread ID
        on_message: 回调函数 fn(agent_name, msg)，每条新 AI/Tool 消息时调用
        on_subagent_event: 回调函数 fn(event_type, data)，LLM/工具 原始事件时调用

    Returns:
        最终完整结果 dict（含 messages 列表）
    """
    import os
    os.environ["HARNESS_ROOT"] = str(Path(harness_root).resolve())
    os.environ["HARNESS_AGENT_ID"] = agent_cfg.id

    if run_dir:
        os.environ["HARNESS_RUN_DIR"] = str(Path(run_dir).resolve())

    agent = build_agent(agent_cfg, harness_root, registry=registry, tools=tools)

    config = {"configurable": {"thread_id": thread_id or "default"}}

    # ── stream_mode=["messages","values"] + subgraphs=True ─────────────────────
    # "messages": 每个 token chunk 按到来顺序立即推送，meta 含 lc_agent_name
    # "values":   图状态快照，用于获取工具调用结果和最终 messages list
    #
    # 结构（subgraphs=True 时）:
    #   (namespace_tuple, "messages", (chunk, meta))
    #   (namespace_tuple, "values",   state_dict)
    # namespace=() 是顶层图，namespace=('tools:uuid',) 是 subagent 子图
    #
    # 输出策略：
    #   - 每个 model 节点的 AI chunk → 按 (agent_name, msg_id) 聚合
    #   - 当同 agent 的新 msg_id 开始 → flush 上一条消息（触发 on_message）
    #   - values(ns=root) 更新 → flush 所有 pending；检查新增 ToolMessage → on_subagent_event
    last_values: dict[str, Any] = {}
    _pending: dict[tuple[str, str], Any] = {}  # (agent_name, msg_id) → accumulated chunk
    # prev_msg_count[0] = None: 未初始化; int: 已知基准（含历史消息数）
    _prev: list[int | None] = [None]

    def _flush_one(key: tuple[str, str]) -> None:
        accumulated = _pending.pop(key, None)
        if accumulated is None or not on_message:
            return
        aname = key[0]
        content = getattr(accumulated, "content", "") or ""
        tc = getattr(accumulated, "tool_calls", None) or []
        if not content and not tc:
            return
        on_message(aname, accumulated)

    def _flush_all_except(except_key: tuple[str, str] | None = None) -> None:
        for k in list(_pending.keys()):
            if k != except_key:
                _flush_one(k)

    for event in agent.stream(
        {"messages": [HumanMessage(content=message)]},
        config=config,
        stream_mode=["messages", "values"],
        subgraphs=True,
    ):
        if not isinstance(event, tuple) or len(event) != 3:
            continue
        ns, mode, data = event
        is_root = ns == ()

        if mode == "messages":
            chunk, meta = data
            node: str = meta.get("langgraph_node", "")
            if node != "model":
                continue
            agent_name: str = meta.get("lc_agent_name") or meta.get("name", "agent")
            msg_id: str = getattr(chunk, "id", "") or ""
            key = (agent_name, msg_id)

            # 若当前 pending 里有该 agent 的不同消息 → 先 flush 那条
            for other_key in list(_pending.keys()):
                if other_key[0] == agent_name and other_key != key:
                    _flush_one(other_key)

            # 累积 chunk
            if key in _pending:
                try:
                    _pending[key] = _pending[key] + chunk
                except Exception:
                    _pending[key] = chunk
            else:
                _pending[key] = chunk

        elif mode == "values":
            if is_root:
                # 顶层图状态更新：flush 所有当前 pending，更新最终状态
                _flush_all_except()
                last_values = data

                # 触发新出现的工具结果（task 工具返回）
                msgs = data.get("messages", []) if isinstance(data, dict) else []
                if _prev[0] is None:
                    # 第一次 root values 事件：设置基准（含历史消息），不触发 tool_end
                    _prev[0] = len(msgs)
                else:
                    if on_subagent_event:
                        for msg in msgs[_prev[0]:]:
                            if getattr(msg, "type", "") == "tool":
                                tool_name = getattr(msg, "name", "?") or "?"
                                content = getattr(msg, "content", "") or ""
                                on_subagent_event("tool_end", {
                                    "agent": tool_name,
                                    "tool": tool_name,
                                    "output": content[:500],
                                })
                    _prev[0] = len(msgs)
            else:
                # subagent 子图状态更新：flush 该子图 agent 的 pending chunks
                # data 是 subagent 的状态，包含 messages
                sub_msgs = data.get("messages", []) if isinstance(data, dict) else []
                if sub_msgs:
                    # 找到最新的 AI 消息对应的 agent_name，flush 它的 pending
                    for msg in reversed(sub_msgs):
                        if getattr(msg, "type", "") == "ai":
                            # flush 这条 AI 消息对应的 agent pending
                            msg_id = getattr(msg, "id", "")
                            # 找到 pending 里同 msg_id 的条目
                            for k in list(_pending.keys()):
                                if k[1] == msg_id:
                                    _flush_one(k)
                            break

    # flush 最后残留
    _flush_all_except()

    # 兜底
    if not last_values.get("messages"):
        try:
            state = agent.get_state(config)
            if state:
                last_values = dict(state.values)
        except Exception:
            pass

    return last_values if last_values else {"messages": []}
