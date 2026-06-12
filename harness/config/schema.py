"""配置 Schema 定义

用 dataclass 定义 harness.json 的完整数据结构。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ModelConfig:
    """模型配置"""
    provider: str = ""
    model_id: str = ""
    base_url: str = ""
    api_key: str = ""
    temperature: float = 0.0
    max_tokens: int = 8192
    context_window: int = 128000
    response_format: dict[str, Any] | None = None


@dataclass
class ToolsConfig:
    """工具白黑名单"""
    allow: list[str] = field(default_factory=lambda: ["*"])
    deny: list[str] = field(default_factory=list)


@dataclass
class ContextConfig:
    """上下文管理配置"""
    keep_turns: int = 4
    max_input_tokens: int = 128000
    summary_trigger_fraction: float = 0.85
    offload_threshold: int = 20000


@dataclass
class MemoryConfig:
    """Memory 配置"""
    auto_load: bool = True
    max_index_lines: int = 200


@dataclass
class DefaultsConfig:
    """全局默认配置"""
    model: ModelConfig = field(default_factory=ModelConfig)
    tools: ToolsConfig = field(default_factory=ToolsConfig)
    context: ContextConfig = field(default_factory=ContextConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    max_steps: int = 50
    max_concurrent_subagents: int = 4
    prompt: str = ""  # 全局默认 prompt 文件路径，非空时替代 DeepAgents 默认 prompt


@dataclass
class AgentConfig:
    """单个 Agent 的配置"""
    id: str = ""
    name: str = ""
    type: str = "worker"  # coordinator | worker
    workspace: str = ""
    model: ModelConfig | None = None  # None 表示继承 defaults
    skills: list[str] = field(default_factory=list)
    subagents: list[str] = field(default_factory=list)
    tools: ToolsConfig | None = None  # None 表示继承 defaults
    context: ContextConfig | None = None
    memory: MemoryConfig | None = None
    max_steps: int | None = None
    description: str = ""
    prompt: str | None = None  # None 表示继承 defaults，"" 表示使用 DeepAgents 默认 prompt
    default: bool = False  # 是否为默认 agent


@dataclass
class SFTConfig:
    """SFT 数据导出配置"""
    enabled: bool = True
    output_dir: str = "runs"
    format: str = "openai_messages"
    include_tool_calls: bool = True
    include_subagent_traces: bool = True


@dataclass
class ProviderConfig:
    """单个 Provider 的配置"""
    base_url: str = ""
    api_key: str = ""
    models: dict[str, dict[str, Any]] = field(default_factory=dict)
    # models: { "model_id": { "context_window": ..., "max_tokens": ... } }


@dataclass
class HarnessConfig:
    """顶层配置，对应 harness.json"""
    providers: dict[str, ProviderConfig] = field(default_factory=dict)
    defaults: DefaultsConfig = field(default_factory=DefaultsConfig)
    agents: list[AgentConfig] = field(default_factory=list)
    shared_skills_dirs: list[str] = field(default_factory=list)
    sft: SFTConfig = field(default_factory=SFTConfig)
    checkpoints_dir: str = "checkpoints"
    runs_dir: str = "runs"
