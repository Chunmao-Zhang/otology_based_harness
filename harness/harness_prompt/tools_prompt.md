## Harness 工具说明

你拥有以下额外工具：

### web_search

搜索互联网信息，返回相关网页的标题、链接和摘要。

用法：`web_search(query="搜索关键词", num_results=5)`

### execute

执行 shell 命令，返回 stdout/stderr 和退出码。可用于运行 Python 脚本、安装依赖、执行任意命令。

示例用法：`execute(command="python3 /workspaces/jingping-sub-01/skills/01_选定待攻克商家/execute.py --input /workspaces/jingping-coordinator/data/00_raw_shops.json --output /workspaces/jingping-coordinator/data/01_target_shops.json")`

常见场景：
- 执行 Python 脚本：`execute(command="python3 /workspaces/.../execute.py --input /workspaces/.../input.json --output /workspaces/.../output.json")`
- 安装依赖：`execute(command="pip install pandas")`
- 查看 Python 版本：`execute(command="python3 --version")`

注意：
- 默认可以使用以 `/workspaces/` 开头的虚拟绝对路径（与 read_file、write_file、ls 等工具一致）。
- 如果当前 workspace 的 `AGENT.md`、skill 或前端 case prompt 指定了 repo-relative 路径规则，优先遵守 workspace 规则，不要自行改写路径。
- 默认超时 120 秒，可通过 timeout 参数覆盖

### save_memory

将一条长期记忆保存到 memory 系统。自动写入 topic 文件并更新索引。

用法：`save_memory(name="preference-name", description="一句话描述", type="Feedback", content="详细内容")`

type 必须是以下之一：User、Feedback、Project、Reference。
