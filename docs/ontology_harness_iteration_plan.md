# Ontology Harness 迭代计划

本文档用于指导 `Ontology Based Harness` 的小步迭代。原则是：每一步都必须产生可交付结果，并且有明确的验证方式，避免系统还没闭环就堆太多能力。

目标不是一次性完成完整系统，而是按顺序构建一组可验证的小闭环：

```text
agent 可启动
-> 单个 subagent 可验证
-> 相邻阶段可串联
-> coordinator 可 gate
-> 数据和 workspace 可落盘
-> solver 可基于 workspace 回答
-> MVP 全链路闭环
```

## 0. 迭代原则

1. 每个 subagent 都必须有自己的 `AGENT.md`。
2. 每个 subagent 都必须定义输入 JSON、输出 JSON 和停止边界。
3. 每一步先用固定 fixture 验证，再接入真实模型自由流程。
4. 任何进入下一阶段的文件都必须落盘，不能只存在聊天上下文里。
5. coordinator 只负责编排、gate 和用户交互，不承担具体抽取、构建、判断任务。
6. 没有用户确认 schema 前，不允许进入数据抽取。
7. 每个阶段都要能独立运行和独立失败，方便定位问题。

建议目录：

```text
otology_agent_workspace/
├── AGENT.md
├── subagent_worksapce/
│   ├── problem_clarifier/AGENT.md
│   ├── evidence_collector/AGENT.md
│   ├── schema_builder/AGENT.md
│   ├── schema_judger/AGENT.md
│   ├── data_extractor/AGENT.md
│   └── workspace_solver/AGENT.md
├── tools/
│   ├── source_reader.py
│   ├── evidence_retriever.py
│   └── schema_validator.py
└── utils/
    ├── problem_clarifier_contract.py
    └── *_company_schema.py

test_data/ontology/
evals/ontology/
runs/ontology_workspace_runs/
```

## 1. 阶段一：Harness 底座可启动

### 目标

让 ontology harness 作为一个独立 agent 系统启动起来，并确认主 agent 和 subagent 都读取自己的 `AGENT.md`。

### 需要做什么

1. 创建根目录 `harness.json`。
2. 写入 `providers.deepseek` 配置，模型使用 `deepseek/deepseek-v4-flash`。
3. 创建 `otology_agent_workspace/AGENT.md`。
4. 创建 6 个 subagent 工作目录及各自 `AGENT.md`。
5. 在 `harness.json` 中配置：
   - `ontology_coordinator`
   - `problem_clarifier`
   - `evidence_collector`
   - `schema_builder`
   - `schema_judger`
   - `data_extractor`
   - `workspace_solver`
6. 确认每个 subagent 配置不同的 `workspace`。

### 可交付结果

```text
harness.json
otology_agent_workspace/AGENT.md
otology_agent_workspace/subagent_worksapce/*/AGENT.md
```

### 验证方式

运行：

```bash
python3 -m harness.run \
  --agent problem_clarifier \
  --message '{"question":"美国有哪些数据分析公司","upload_paths":[]}' \
  --verbose
```

### 通过标准

- 命令可以启动。
- 使用的是 `problem_clarifier` agent。
- 模型看到的是 `problem_clarifier/AGENT.md` 的任务说明。
- 不报 `Agent not found`、`Prompt file not found`、模型配置缺失等错误。

### 常见失败方向

- `harness.json` JSON 格式错误。
- provider/model 名称不匹配。
- subagent `workspace` 路径写错。
- `AGENT.md` 没创建或为空。

## 2. 阶段二：Tool 底座可用

### 目标

实现 ontology harness 必需的 agent-callable workspace tools，并确认 subagent 可以通过父 workspace 继承工具。

### 需要做什么

1. 创建 `otology_agent_workspace/tools/__init__.py`。
2. 实现并暴露：
   - `source_reader`
   - `evidence_retriever`
   - `schema_validator`
3. `execute_code` 作为 harness 内置工具提供给 solver，不放在 `otology_agent_workspace/tools/`。
4. 给不同 subagent 设置工具白名单。

### 可交付结果

```text
otology_agent_workspace/tools/__init__.py
otology_agent_workspace/tools/source_reader.py
otology_agent_workspace/tools/evidence_retriever.py
otology_agent_workspace/tools/schema_validator.py
```

### 验证方式

分别运行：

```bash
python3 -m harness.run \
  --agent schema_judger \
  --message '{"schema_text":"class Company:\\n    _id: str\\n    name: str"}' \
  --verbose
```

观察 verbose 输出中的 tool 列表和调用情况。

### 通过标准

- `schema_judger` 能看到 `schema_validator`。
- `problem_clarifier` 看不到不该用的工具，例如 `schema_validator`、`execute_code`。
- 子目录 workspace 下的 subagent 可以继承 `otology_agent_workspace/tools/`。
- 工具返回 JSON 字符串，失败时也返回结构化错误。

### 常见失败方向

- workspace tool 没有暴露到 `WORKSPACE_TOOLS`。
- tool 名称和 `harness.json` allow 列表不一致。
- LangChain tool 参数 schema 写得太宽或太窄。

## 3. 阶段三：problem_clarifier 单点闭环

### 目标

验证 `problem_clarifier` 只做问题澄清和计划确认，不回答最终问题。

### 需要做什么

1. 编写 `problem_clarifier/AGENT.md`。
2. 明确输入格式：

```json
{
  "question": "...",
  "upload_paths": []
}
```

3. 明确输出格式：

```json
{
  "problem": "...",
  "steps": ["..."]
}
```

4. 禁止输出 schema、实例数据和最终答案。

### 可交付结果

```text
otology_agent_workspace/subagent_worksapce/problem_clarifier/AGENT.md
evals/ontology/problem_clarifier_cases.jsonl
```

### 验证方式

准备测试样例：

```json
{"question":"美国有哪些数据分析公司","upload_paths":[]}
{"question":"根据这个 CSV 找出华东地区销售额最高的产品","upload_paths":["test_data/sales.csv"]}
{"question":"比较 A 公司和 B 公司过去三年的研发投入变化","upload_paths":[]}
```

逐条运行 `problem_clarifier`。

### 通过标准

- 输出是合法 JSON。
- 只包含 `problem` 和 `steps`。
- `problem` 重写了用户真实需求，没有遗漏关键约束。
- `steps` 是面向解决问题的步骤，不发散到无关任务。
- 不直接回答用户问题。

### 常见失败方向

- 模型把“计划”写成了最终答案。
- steps 太泛，例如“收集数据、分析数据、回答问题”。
- 对上传文件的作用描述不清。

## 4. 阶段四：source_reader 与 evidence_collector 闭环

### 目标

验证上传文件和 web 需求能统一整理成 evidence plan。

### 需要做什么

1. 实现 `source_reader`：
   - 支持 `.txt`、`.md`、`.csv`。
   - CSV 输出 columns 和 sample_rows。
   - 文本输出 chunks 和 metadata。
2. 编写 `evidence_collector/AGENT.md`。
3. 准备 fixture 文件：
   - `test_data/ontology/company_sample.csv`
   - `test_data/ontology/company_notes.txt`
4. 输出 `evidence_manifest.json`。

### 可交付结果

```text
otology_agent_workspace/tools/source_reader.py
otology_agent_workspace/subagent_worksapce/evidence_collector/AGENT.md
test_data/ontology/company_sample.csv
test_data/ontology/company_notes.txt
runs/ontology_workspace_runs/<run_id>/intermediate/evidence_manifest.json
```

### 验证方式

运行：

```bash
python3 -m harness.run \
  --agent evidence_collector \
  --message '{"question":"这些文件里有哪些数据分析公司","upload_paths":["test_data/ontology/company_sample.csv"]}' \
  --verbose
```

### 通过标准

- 能识别上传文件类型。
- CSV 有 columns 和 sample_rows。
- 文本有 chunks。
- 输出包含：
  - `sources`
  - `needs_web_search`
  - `evidence_manifest_path`
- 信息不足时能设置 `needs_web_search=true`。
- 不构建 schema，不抽取实例。

### 常见失败方向

- 文件路径虚拟路径和真实路径混用。
- evidence 只在聊天里输出，没有落盘。
- `needs_web_search` 判断过于随意。

## 5. 阶段五：schema_validator 闭环

### 目标

先把 schema 的机械校验做好，给后面的 `schema_builder` 和 `schema_judger` 提供硬约束。

### 需要做什么

1. 实现 `schema_validator`。
2. 校验 Python 语法。
3. 校验 class 和字段约定：
   - 类名 PascalCase。
   - 每个类有 `_id`。
   - `_id` 类型是 `str` 或 `int`。
   - 属性类型只能是 `str`、`int`、`float`、`bool` 或 `Optional[...]`。
   - 关系类型只能是 `List["Class"]` 或 `Optional["Class"]`。
   - 关系目标类必须存在。
   - reverse 字段必须带 `# reverse`。

### 可交付结果

```text
otology_agent_workspace/tools/schema_validator.py
otology_agent_workspace/utils/valid_company_schema.py
otology_agent_workspace/utils/invalid_missing_id.py
otology_agent_workspace/utils/invalid_unknown_relation.py
```

### 验证方式

对三个 fixture schema 分别调用 `schema_validator`。

### 通过标准

- valid schema 返回 `valid=true`。
- invalid schema 返回 `valid=false`。
- 错误信息指出具体 class 和字段。
- 不依赖 LLM 判断。

### 常见失败方向

- 用字符串搜索代替 AST，导致误判。
- `Optional[str]` 和 `Optional["Country"]` 没区分属性/关系。
- 没有检查关系目标类是否存在。

## 6. 阶段六：schema_builder 单点闭环

### 目标

给定问题和 evidence，生成可校验的 `draft_schema.py`。

### 需要做什么

1. 编写 `schema_builder/AGENT.md`。
2. 约束输出必须写入：

```text
runs/ontology_workspace_runs/<run_id>/concepts/draft_schema.py
```

3. 要求生成后调用 `schema_validator`。
4. 若校验失败，必须自我修复一次。
5. 先用固定 evidence，不接 web 搜索。

### 可交付结果

```text
otology_agent_workspace/subagent_worksapce/schema_builder/AGENT.md
runs/ontology_workspace_runs/<run_id>/concepts/draft_schema.py
```

### 验证方式

输入：

```json
{
  "question": "美国有哪些数据分析公司",
  "sources": [
    {
      "source_id": "company_sample.csv",
      "source_kind": "upload",
      "file_type": "csv",
      "reason": "包含公司名称、国家、行业字段"
    }
  ],
  "evidence_manifest_path": "runs/ontology_workspace_runs/test/intermediate/evidence_manifest.json"
}
```

### 通过标准

- 生成 `draft_schema.py`。
- `schema_validator` 通过。
- 至少包含能回答问题的核心概念，例如 `Company`、`Industry`。
- 包含地点或国家过滤能力，例如 `country` 或 `headquartered_in`。
- 不生成实例数据。

### 常见失败方向

- schema 过度复杂。
- 类名、字段名不稳定。
- 关系方向混乱。
- 生成 schema 后没有调用 validator。

## 7. 阶段七：schema_judger 单点闭环

### 目标

判断 schema 是否足以回答问题，并能指出缺失项。

### 需要做什么

1. 编写 `schema_judger/AGENT.md`。
2. 准备两类 schema fixture：
   - 可回答 schema。
   - 不可回答 schema。
3. 输出固定 JSON：

```json
{
  "answerable": false,
  "coverage_score": 0.62,
  "missing_requirements": ["..."],
  "recommended_action": "patch_schema"
}
```

### 可交付结果

```text
otology_agent_workspace/subagent_worksapce/schema_judger/AGENT.md
otology_agent_workspace/utils/answerable_company_schema.py
otology_agent_workspace/utils/unanswerable_company_schema.py
```

### 验证方式

分别输入好 schema 和坏 schema。

### 通过标准

- 好 schema 返回 `answerable=true`。
- 坏 schema 返回 `answerable=false`。
- `missing_requirements` 具体到字段或关系。
- 不修改 schema。
- 不抽取数据。

### 常见失败方向

- 判断太宽松，缺字段也说可回答。
- 判断太严格，要求不必要字段。
- `recommended_action` 不可执行。

## 8. 阶段八：coordinator 半链路闭环

### 目标

让主控 agent 串起：

```text
problem_clarifier
-> evidence_collector
-> schema_builder
-> schema_judger
-> 展示 schema
-> 等待用户确认
```

### 需要做什么

1. 编写 `otology_agent_workspace/AGENT.md`。
2. 明确 coordinator 的 gate：
   - 用户未确认 problem，不进入 evidence。
   - 用户未确认 schema，不进入 data_extractor。
3. 定义中间结果路径。
4. 让 coordinator 对 subagent 输入输出只传 JSON。

### 可交付结果

```text
otology_agent_workspace/AGENT.md
runs/ontology_workspace_runs/<run_id>/intermediate/evidence_manifest.json
runs/ontology_workspace_runs/<run_id>/concepts/draft_schema.py
```

### 验证方式

运行：

```bash
python3 -m harness.run \
  --agent ontology_coordinator \
  --message '美国有哪些数据分析的公司' \
  --verbose
```

### 通过标准

- coordinator 先展示澄清后的 problem 和 steps。
- 用户未确认前，不继续执行。
- schema 生成后展示 Python schema 和关系表。
- 用户未确认 schema 前，不调用 `data_extractor`。
- verbose trace 能看清 subagent 调用顺序。

### 常见失败方向

- coordinator 自己做了 subagent 的工作。
- gate 失效，没确认就继续。
- subagent 输出不是 JSON，coordinator 难以解析。

## 9. 阶段九：schema 确认与表单渲染后端逻辑

### 目标

不依赖 LLM，实现 schema 的展示、确认和修改基础能力。

### 需要做什么

1. 实现 Pydantic schema parser。
2. 实现 schema -> 表单 JSON。
3. 实现确认动作：

```text
draft_schema.py -> confirmed_schema.py
```

4. 实现简单表单修改写回 schema。
5. 自然语言 patch 可以后置，先不做。

### 可交付结果

```text
harness/ontology/schema_service.py
runs/ontology_workspace_runs/<run_id>/concepts/confirmed_schema.py
```

### 验证方式

用 fixture schema 调用 schema service。

### 通过标准

- 能从 schema 生成实体和关系表单 JSON。
- 确认后生成 `confirmed_schema.py`。
- 修改字段名后重新生成合法 schema。
- 修改结果通过 `schema_validator`。

### 常见失败方向

- 表单 JSON 变成第二事实源。
- 修改写回后格式不可读。
- 关系字段和属性字段混淆。

## 10. 阶段十：data_extractor 单点闭环

### 目标

按 confirmed schema 和 evidence 生成实例化对象、事实表、关系表。

### 需要做什么

1. 编写 `data_extractor/AGENT.md`。
2. 明确输入：

```json
{
  "schema_path": ".../confirmed_schema.py",
  "sources": [],
  "evidence_manifest_path": "..."
}
```

3. 明确输出文件：
   - `data/instances.json`
   - `data/facts.csv`
   - `data/relations.csv`
   - `intermediate/extraction_report.json`
4. 先用固定 CSV fixture 验证。

### 可交付结果

```text
otology_agent_workspace/subagent_worksapce/data_extractor/AGENT.md
runs/ontology_workspace_runs/<run_id>/data/instances.json
runs/ontology_workspace_runs/<run_id>/data/facts.csv
runs/ontology_workspace_runs/<run_id>/data/relations.csv
runs/ontology_workspace_runs/<run_id>/intermediate/extraction_report.json
```

### 验证方式

输入 confirmed schema 和 company fixture。

### 通过标准

- `instances.json` 按 concept 分组。
- 每个实例有 `_id` 和 `_concept`。
- facts 只包含属性事实。
- relations 只包含对象关系。
- relations 中引用的 object id 必须在 instances 中存在。
- 输出包含 `source_refs` 和 `confidence`。

### 常见失败方向

- 字段超出 schema。
- 关系引用了不存在的实例。
- 把属性写进 relations。
- 把关系写进 facts。

## 11. 阶段十一：workspace_builder 后端闭环

### 目标

用后端逻辑把 confirmed schema 和抽取数据组织成 solver 可使用的 run workspace。

### 需要做什么

1. 实现 workspace builder。
2. 根据 confirmed schema 拆分生成 `concepts/*.py`。
3. 复制或链接数据文件到 run workspace。
4. 创建 `src/` 初始脚手架。
5. 生成 `workspace_manifest.json`。

### 可交付结果

```text
harness/ontology/workspace_builder.py
runs/ontology_workspace_runs/<run_id>/
├── data/
├── concepts/
├── src/
└── intermediate/workspace_manifest.json
```

### 验证方式

用阶段九、十的产物调用 workspace builder。

### 通过标准

- 目录结构完整。
- manifest 列出所有生成文件。
- `concepts/*.py` 可以 import。
- `src/main.py` 可以读取 data 文件。
- 重复运行不会破坏已有 confirmed schema 和 data。

### 常见失败方向

- run 目录和 harness runtime 的 `runs/` 目录混淆。
- 生成代码不可 import。
- 覆盖了用户或 solver 写过的 `src/` 文件。

## 12. 阶段十二：workspace_solver 单点闭环

### 目标

让 solver 只基于 run workspace 回答问题。

### 需要做什么

1. 编写 `workspace_solver/AGENT.md`。
2. 限制 solver：
   - 必须先读取 `concepts/` 和 `data/`。
   - 可以在 `src/` 写代码。
   - 可以执行代码。
   - 不重新构建 schema。
   - 不重新抽取实例。
3. 在 `harness.json` 中只给 `workspace_solver` 允许 `execute_code`。

### 可交付结果

```text
otology_agent_workspace/subagent_worksapce/workspace_solver/AGENT.md
runs/ontology_workspace_runs/<run_id>/src/main.py
runs/ontology_workspace_runs/<run_id>/intermediate/solver_result.json
```

### 验证方式

给 solver 输入：

```json
{
  "question": "美国有哪些数据分析公司",
  "schema_path": "runs/ontology_workspace_runs/<run_id>/concepts/confirmed_schema.py",
  "workspace_dir": "runs/ontology_workspace_runs/<run_id>"
}
```

### 通过标准

- solver 读取 workspace 文件。
- solver 在 `src/` 中写代码并执行。
- 最终答案来自执行结果或数据文件。
- 回答包含使用的 schema 和数据来源摘要。
- 不调用 `schema_builder`、`data_extractor`。

### 常见失败方向

- solver 直接凭常识回答。
- solver 忽略 workspace 数据。
- 代码写到错误目录。
- 运行路径使用真实路径和虚拟路径混乱。

## 13. 阶段十三：完整 MVP 闭环

### 目标

跑通从用户问题到最终答案的完整流程。

### 需要做什么

串联：

```text
用户问题
-> problem_clarifier
-> 用户确认
-> evidence_collector
-> schema_builder
-> schema_judger
-> 用户确认 schema
-> data_extractor
-> workspace_builder
-> workspace_solver
-> 最终答案
```

### 可交付结果

完整 run 目录：

```text
runs/ontology_workspace_runs/<run_id>/
├── concepts/
│   ├── draft_schema.py
│   └── confirmed_schema.py
├── data/
│   ├── instances.json
│   ├── facts.csv
│   └── relations.csv
├── src/
│   └── main.py
└── intermediate/
    ├── evidence_manifest.json
    ├── extraction_report.json
    ├── workspace_manifest.json
    └── solver_result.json
```

### 验证方式

运行 coordinator：

```bash
python3 -m harness.run \
  --agent ontology_coordinator \
  --message '美国有哪些数据分析的公司' \
  --verbose
```

### 通过标准

- 每个阶段都产生对应文件。
- 两个 gate 都生效：
  - problem 确认 gate。
  - schema 确认 gate。
- 最终答案可追溯到 `instances.json`、`facts.csv` 或 `relations.csv`。
- verbose trace 能看出完整调用链。
- 中途失败时能定位到具体阶段。

### 常见失败方向

- coordinator 状态管理不清，二次确认后上下文丢失。
- subagent 输出漂移，导致下游解析失败。
- schema 和 data 不一致。
- solver 绕过 workspace。

## 14. 阶段十四：回归测试与评估集

### 目标

把前面每个阶段的成功样例固定成回归测试，防止后续改动走偏。

### 需要做什么

1. 建立 `evals/`。
2. 每个 subagent 至少 3 个测试样例。
3. 每个工具至少 1 个成功样例和 1 个失败样例。
4. 对 LLM 输出做结构化校验，不要求全文完全一致。

### 可交付结果

```text
evals/ontology/
├── problem_clarifier_cases.jsonl
├── evidence_collector_cases.jsonl
├── schema_builder_cases.jsonl
├── schema_judger_cases.jsonl
├── data_extractor_cases.jsonl
└── workspace_solver_cases.jsonl
```

### 验证方式

运行评估脚本：

```bash
python3 -m otology_agent_workspace.evals.run_contract_tests
```

### 通过标准

- 每个 subagent 的输出 JSON 可解析。
- 必填字段齐全。
- 关键语义约束满足。
- 工具调用不越权。
- 文件产物存在且格式正确。

### 常见失败方向

- 评估标准过度依赖自然语言完全匹配。
- fixture 太少，覆盖不了常见问题。
- 没有把失败样例纳入测试。

## 15. 推荐里程碑

### M1：Agent 骨架可运行

包含阶段 1-3。

可展示成果：

```text
主 agent 和 subagent 都能启动；
problem_clarifier 可以稳定输出 problem + steps。
```

### M2：Schema 闭环可运行

包含阶段 4-8。

可展示成果：

```text
输入问题和 fixture 文件；
系统生成 evidence_manifest 和 draft_schema；
schema_judger 判断是否可回答；
coordinator 停在 schema 确认 gate。
```

### M3：数据闭环可运行

包含阶段 9-11。

可展示成果：

```text
用户确认 schema 后；
系统生成 confirmed_schema、instances、facts、relations 和 run workspace。
```

### M4：问答闭环可运行

包含阶段 12-13。

可展示成果：

```text
solver 基于 workspace 编写并执行代码；
最终回答用户问题；
答案可以追溯到数据来源。
```

### M5：稳定性和回归

包含阶段 14。

可展示成果：

```text
每个 subagent 有 contract test；
每次修改后可以快速判断是否偏离预期。
```

## 16. 每次迭代的完成定义

每次小迭代必须满足：

1. 有代码或文档产物。
2. 有至少一个可运行命令或测试脚本。
3. 有明确通过标准。
4. 有失败时的定位方向。
5. 不依赖人工猜测“看起来还行”。

如果某一步没有可验证产物，就拆小；如果某一步需要多个 subagent 同时改，就先改成单点验证。
