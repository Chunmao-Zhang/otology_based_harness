"""配置加载器

读取 harness.json -> HarnessConfig。
支持 ${ENV_VAR} 环境变量替换。
支持 "provider/model_id" 简写引用顶层 providers 配置。
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from harness.config.schema import (
    AgentConfig,
    ContextConfig,
    DefaultsConfig,
    HarnessConfig,
    MemoryConfig,
    ModelConfig,
    ProviderConfig,
    SFTConfig,
    ToolsConfig,
)


def _resolve_env(value: str) -> str:
    """替换 ${ENV_VAR} 占位符为环境变量值"""
    if not isinstance(value, str):
        return value
    return re.sub(
        r"\$\{([^}]+)\}",
        lambda m: os.environ.get(m.group(1), m.group(0)),
        value,
    )


def _parse_providers(data: dict[str, Any] | None) -> dict[str, ProviderConfig]:
    """解析顶层 providers 配置"""
    if not data:
        return {}
    providers = {}
    for name, cfg in data.items():
        providers[name] = ProviderConfig(
            base_url=_resolve_env(cfg.get("base_url", "")),
            api_key=_resolve_env(cfg.get("api_key", "")),
            models=cfg.get("models", {}),
        )
    return providers


def _resolve_model_ref(
    model_value: Any,
    providers: dict[str, ProviderConfig],
    temperature: float = 0.0,
) -> ModelConfig | None:
    """解析 model 字段

    支持两种格式：
    - 字符串简写: "provider/model_id" → 从 providers 中查找
    - 完整 dict: { "provider": ..., "model_id": ..., ... } → 直接解析
    - None → 返回 None（继承 defaults）
    """
    if model_value is None:
        return None

    if isinstance(model_value, str):
        # 简写格式: "siliconflow/Qwen/Qwen3.5-27B"
        # 第一个 / 之前是 provider name，之后是 model_id
        parts = model_value.split("/", 1)
        if len(parts) != 2:
            return None

        provider_name, model_id = parts
        provider = providers.get(provider_name)
        if not provider:
            return ModelConfig(
                provider=provider_name,
                model_id=model_id,
                temperature=temperature,
            )

        # 从 provider 中获取模型参数
        model_params = provider.models.get(model_id, {})
        return ModelConfig(
            provider=provider_name,
            model_id=model_id,
            base_url=provider.base_url,
            api_key=provider.api_key,
            temperature=temperature,
            max_tokens=model_params.get("max_tokens", 8192),
            context_window=model_params.get("context_window", 128000),
            response_format=model_params.get("response_format"),
        )

    if isinstance(model_value, dict):
        # 完整 dict 格式（向后兼容）
        return _parse_model_dict(model_value)

    return None


def _parse_model_dict(data: dict[str, Any] | None) -> ModelConfig | None:
    """解析完整的 model dict（向后兼容）"""
    if not data:
        return None
    return ModelConfig(
        provider=data.get("provider", ""),
        model_id=data.get("model_id", ""),
        base_url=_resolve_env(data.get("base_url", "")),
        api_key=_resolve_env(data.get("api_key", "")),
        temperature=data.get("temperature", 0.0),
        max_tokens=data.get("max_tokens", 8192),
        context_window=data.get("context_window", 128000),
        response_format=data.get("response_format"),
    )


def _parse_tools(data: dict[str, Any] | None) -> ToolsConfig | None:
    if not data:
        return None
    return ToolsConfig(
        allow=data.get("allow", ["*"]),
        deny=data.get("deny", []),
    )


def _parse_context(data: dict[str, Any] | None) -> ContextConfig | None:
    if not data:
        return None
    return ContextConfig(
        keep_turns=data.get("keep_turns", 4),
        max_input_tokens=data.get("max_input_tokens", 128000),
        summary_trigger_fraction=data.get("summary_trigger_fraction", 0.85),
        offload_threshold=data.get("offload_threshold", 20000),
    )


def _parse_memory(data: dict[str, Any] | None) -> MemoryConfig | None:
    if not data:
        return None
    return MemoryConfig(
        auto_load=data.get("auto_load", True),
        max_index_lines=data.get("max_index_lines", 200),
    )


def _parse_agent(data: dict[str, Any], providers: dict[str, ProviderConfig], temperature: float) -> AgentConfig:
    """解析单个 agent 配置"""
    return AgentConfig(
        id=data["id"],
        name=data.get("name", data["id"]),
        type=data.get("type", "worker"),
        workspace=data.get("workspace", f"workspaces/{data['id']}"),
        model=_resolve_model_ref(data.get("model"), providers, temperature),
        skills=data.get("skills", []),
        subagents=data.get("subagents", []),
        tools=_parse_tools(data.get("tools")),
        context=_parse_context(data.get("context")),
        memory=_parse_memory(data.get("memory")),
        max_steps=data.get("max_steps"),
        description=data.get("description", ""),
        prompt=data.get("prompt"),  # None = 继承 defaults
        default=data.get("default", False),
    )


def load_config(config_path: str | Path) -> HarnessConfig:
    """加载 harness.json 并解析为 HarnessConfig"""
    config_path = Path(config_path)
    if not config_path.exists():
        raise FileNotFoundError(f"Config not found: {config_path}")

    with open(config_path, "r", encoding="utf-8") as f:
        raw = json.load(f)

    # providers（顶层）
    providers = _parse_providers(raw.get("providers"))

    # defaults
    d = raw.get("defaults", {})
    temperature = d.get("temperature", 0.0)
    default_model = _resolve_model_ref(d.get("model"), providers, temperature) or ModelConfig()

    defaults = DefaultsConfig(
        model=default_model,
        tools=_parse_tools(d.get("tools")) or ToolsConfig(),
        context=_parse_context(d.get("context")) or ContextConfig(),
        memory=_parse_memory(d.get("memory")) or MemoryConfig(),
        max_steps=d.get("max_steps", 50),
        max_concurrent_subagents=d.get("max_concurrent_subagents", 4),
        prompt=d.get("prompt", ""),
    )

    # agents
    agents = [_parse_agent(a, providers, temperature) for a in raw.get("agents", [])]

    # sft
    s = raw.get("sft", {})
    sft = SFTConfig(
        enabled=s.get("enabled", True),
        output_dir=s.get("output_dir", "runs"),
        format=s.get("format", "openai_messages"),
        include_tool_calls=s.get("include_tool_calls", True),
        include_subagent_traces=s.get("include_subagent_traces", True),
    )

    return HarnessConfig(
        providers=providers,
        defaults=defaults,
        agents=agents,
        shared_skills_dirs=raw.get("shared_skills_dirs", []),
        sft=sft,
        checkpoints_dir=raw.get("checkpoints_dir", "checkpoints"),
        runs_dir=raw.get("runs_dir", "runs"),
    )
