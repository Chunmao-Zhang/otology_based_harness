## 工作流规范

### 代码执行工作流

如果你需要通过编写并执行代码来完成任务，请按以下步骤操作：

1. 优先遵守当前 workspace 的 `AGENT.md` 中规定的代码输出目录。
2. 如果当前任务没有规定目录，再使用 `/workspaces/{agent_id}/code/<filename>.py`。
3. 使用 `execute` 工具执行该脚本，如：`execute(command="python3 /workspaces/{agent_id}/code/<filename>.py")`
4. 根据执行结果决定下一步操作（修改代码重试或返回结果）

如果需要执行已有的脚本（如 skills 中的 execute.py），直接使用 `execute` 工具：
```
execute(command="python3 /workspaces/.../skills/.../execute.py --input /workspaces/.../input.json --output /workspaces/.../output.json")
```

默认情况下，工具文件路径可以使用虚拟绝对路径格式（以 `/workspaces/` 开头）。如果当前 workspace 的 `AGENT.md`、skill 或前端 case prompt 给出了更具体的路径规则，必须优先遵守 workspace 规则，不要把 repo-relative 输出路径改写成 `/workspaces/...`。

路径约定：
- 默认代码文件保存到 `/workspaces/{agent_id}/code/` 目录下
- ontology harness 的 `workspace_solver` 必须把分析代码写到 `runs/ontology_workspace_runs/<run_id>/src/`
- 文件名应有意义，如 `fetch_data.py`、`calculate.py`

### 压缩内容查看

如果你在历史消息中看到 `[内容已压缩 - 使用 read_file 查看: <路径>]`，说明该工具的返回结果已被压缩保存。如需查看原始内容，使用 `read_file` 读取对应路径即可。
