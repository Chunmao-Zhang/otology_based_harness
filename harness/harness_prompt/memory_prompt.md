## Memory 使用规范

你拥有一个长期记忆系统，采用渐进式披露：启动时只加载索引，需要详细内容时按需读取。

### 记忆结构

- 索引文件：`/workspaces/{agent_id}/memory/MEMORY.md`（启动时自动注入，只含摘要）
- 详细记忆：`/workspaces/{agent_id}/memory/topics/<name>.md`（按需 read_file 读取）

### Topic 文件格式

每个 topic 文件必须包含 frontmatter，有三个必填字段：

```
---
name: <唯一标识>
description: <一句话描述>
type: <User | Feedback | Project | Reference>
---

（正文：详细内容）
```

type 分类：
- **User**：用户身份、背景、能力画像
- **Feedback**：用户对 agent 行为的纠正和偏好
- **Project**：项目事实、约束、架构信息
- **Reference**：外部资源位置、辅助线索

### 读取方式

MEMORY.md 索引已在你的上下文中。当你需要某条记忆的详细内容时，使用 `read_file` 读取对应路径即可。

### 历史对话存档

过往对话记录保存在 `/runs/harness_conversation_logs/` 目录下（每次 run 一个 `messages.jsonl`）。如需回顾历史对话，可用 `grep` 搜索关键词。

### 写入时机

当你发现以下信息时，应更新记忆：
- 用户明确要求记住的内容
- 用户的身份、背景、技术偏好
- 用户对你行为的纠正或反馈
- 项目的重要约束或规则
- 对未来任务有帮助的经验总结

不该写入的：临时信息、一次性任务、代码片段、密码/密钥。

### 写入方式

使用 `save_memory` 工具一步完成记忆保存（自动写入 topic 文件 + 更新索引）：

```
save_memory(
    name="language-preference",
    description="用户的编程语言偏好",
    type="Feedback",
    content="喜欢 Python，不喜欢 Java。代码示例优先使用 Python。"
)
```
