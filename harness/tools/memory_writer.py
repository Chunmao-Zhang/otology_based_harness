"""save_memory 工具

将记忆保存到 workspace 的 memory 目录。
自动完成：写入 topic file（含 frontmatter）+ 更新 MEMORY.md 索引。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from langchain_core.tools import tool

_HARNESS_ROOT_ENV = "HARNESS_ROOT"

# 合法的 type 值
VALID_TYPES = {"User", "Feedback", "Project", "Reference"}


def _resolve_path(virtual_path: str) -> Path:
    """虚拟路径 -> 真实路径"""
    harness_root = os.environ.get(_HARNESS_ROOT_ENV, os.getcwd())
    return Path(harness_root) / virtual_path.lstrip("/")


@tool
def save_memory(name: str, description: str, type: str, content: str) -> str:
    """Save a piece of long-term memory for future sessions.

    This tool writes a topic file and updates the MEMORY.md index atomically.
    Use this when you learn something worth remembering across sessions:
    user preferences, project facts, feedback corrections, or reference info.

    Args:
        name: Unique identifier for this memory (e.g. "language-preference", "project-architecture").
        description: One-line summary of what this memory contains.
        type: Category - must be one of: User, Feedback, Project, Reference.
        content: The detailed memory content to save.
    """
    # 验证 type
    if type not in VALID_TYPES:
        return json.dumps(
            {"status": "error", "error": f"Invalid type '{type}'. Must be one of: {', '.join(sorted(VALID_TYPES))}"},
            ensure_ascii=False,
        )

    # 获取当前 agent_id（从环境变量 HARNESS_AGENT_ID）
    agent_id = os.environ.get("HARNESS_AGENT_ID", "main")

    # 路径
    memory_dir = f"/workspaces/{agent_id}/memory"
    topics_dir = f"{memory_dir}/topics"
    memory_index = f"{memory_dir}/MEMORY.md"

    # 文件名：type_name.md
    filename = f"{type.lower()}_{name.replace(' ', '_')}.md"
    topic_path = f"{topics_dir}/{filename}"

    # 1. 写入 topic file
    topic_content = f"""---
name: {name}
description: {description}
type: {type}
---

{content}
"""
    real_topic_path = _resolve_path(topic_path)
    real_topic_path.parent.mkdir(parents=True, exist_ok=True)
    real_topic_path.write_text(topic_content, encoding="utf-8")

    # 2. 更新 MEMORY.md 索引
    real_index_path = _resolve_path(memory_index)
    if real_index_path.exists():
        index_content = real_index_path.read_text(encoding="utf-8")
    else:
        index_content = _create_default_index()

    # 构造索引条目
    entry = f"- **{name}**: {description} → `{topic_path}`"

    # 找到对应 type 的 section，插入条目
    section_header = f"## {type}"
    if section_header in index_content:
        # 检查是否已存在同名条目（更新场景）
        lines = index_content.split("\n")
        new_lines = []
        inserted = False
        in_section = False

        for line in lines:
            if line.strip().startswith("## "):
                if in_section and not inserted:
                    # section 结束前插入
                    new_lines.append(entry)
                    new_lines.append("")
                    inserted = True
                in_section = line.strip() == section_header

            # 如果是同名条目，替换
            if in_section and f"**{name}**" in line:
                new_lines.append(entry)
                inserted = True
                continue

            new_lines.append(line)

        # 如果到文件末尾还没插入（section 是最后一个）
        if in_section and not inserted:
            new_lines.append(entry)

        index_content = "\n".join(new_lines)
    else:
        # section 不存在，追加
        index_content += f"\n\n{section_header}\n\n{entry}\n"

    real_index_path.write_text(index_content, encoding="utf-8")

    return json.dumps(
        {
            "status": "success",
            "topic_file": topic_path,
            "index_updated": memory_index,
        },
        ensure_ascii=False,
    )


def _create_default_index() -> str:
    """创建默认的 MEMORY.md 索引"""
    return """# Memory 索引

以下是你的长期记忆索引。需要详细内容时，使用 read_file 读取对应路径。

## User

## Feedback

## Project

## Reference
"""
