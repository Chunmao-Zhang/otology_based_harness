# Ontology QA Agent Frontend

与 `frontend/`（KC-Agent）风格一致的本体问答前端，面向 `ontology_coordinator` 的 7 步工作流。

## 启动

```bash
# 真实智能体模式（需要 deepagents 依赖与模型 API key）
PYTHONPATH=. python3 otology_agent_workspace/frontend/app.py

# 本地 UI 演示模式（无需模型，走确定性的流程演练）
ONTOLOGY_UI_MOCK=1 PYTHONPATH=. python3 otology_agent_workspace/frontend/app.py
```

默认地址：http://127.0.0.1:8095 （可用 `PORT` 环境变量修改）。

## 业务侧边栏（右下角 FAB 唤出）

- **文件与证据**：上传 CSV / TXT / MD 文件，查看本轮证据清单（上传文件 / 网络来源）。
- **Schema 工作台**：以「实体 + 关系表格 + Python 代码」双视图展示 draft / confirmed schema，可直接编辑实体名、语义类型与关系名，应用修改后一键确认 Schema（对应流程中的 schema 确认 gate）。
- **运行与结果**：8 步流程进度可视化（含两个用户确认 gate），以及数据抽取统计摘要与答案数据来源。

## 设计约定

- 完全复用 KC-Agent 的视觉体系（同一 `style.css` 基底、蓝色渐变主色、亮/暗主题、Inter 字体）。
- 聊天区不暴露原始 tool call JSON 或内部路径，只展示友好的阶段提示。
- 会话持久化在 `outputs/ontology_coordinator/frontend_sessions/`，上传文件保存在 `otology_agent_workspace/data/uploads/`。
