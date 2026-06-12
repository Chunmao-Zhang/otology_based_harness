## Ontology Based Harness 规划

**昨天完成：**CoG 智能体构建完成，基本能够稳定

**遗留问题：**如何将用户问题、可选上传文件、schema 构建、数据抽取、工作区生成与智能体代码执行结合起来，构建一套完整的以 Ontology 为中心的问答系统。

---

核心链路：

```text
用户问题 + 可选上传文件
-> 问题澄清与规划确认（待办：子问题拆分，复杂问题拆分为多个子问题）
-> 整理输入证据（统一读取用户上传文件；如果文件不足以回答问题，可以上网搜索资料）
-> 构建 ontology schema
-> 判断 schema 是否足以回答问题
-> 用户确认或修改 schema
-> 按确认 schema 查找数据，构建实例化对象
-> 构建智能体工作区
-> 调用智能体，访问工作区并回答用户问题
```

当前可复用能力：

- `harness/agents/agent_loop.py`：支持 coordinator、subagents、skills、workspace tools；每个 agent 可配置独立 workspace，并默认读取该 workspace 下的 `AGENT.md` 作为任务说明。
- `harness/tools/workspace_loader.py`：支持 workspace 自定义工具。
- `harness/tools/web_search.py`：支持网络搜索。
- `deepagents_kbqa_general`：可作为访问工作区、编写/执行代码并回答问题的智能体参考。
- `otology_skill`：可参考其 schema 生成、本体重构、artifact 验证思路。

## 1. 整体流程

### 1.1 接收用户问题

用户输入自然语言问题，可以上传文件，也可以不上传。

示例：

```text
美国有哪些数据分析的公司
```

主控 agent 接收：

- 用户问题
- 上传文件列表

### 1.2 问题澄清与规划确认

收到用户问题和文件后，第一步调用 `problem_clarifier` 做问题澄清：

1. 分析用户问题和上传文件内容，提炼核心需求。
2. 生成待解决的问题描述和解决步骤，只包含用户关心的内容。
3. 将问题和步骤展示给用户确认。

输出只包含两个字段：

```json
{
  "problem": "获取在美国经营的数据分析公司列表，包括公司名称和所属细分领域",
  "steps": [
    "根据问题构建 Company、Industry 实体及其关系",
    "搜索并抽取符合条件的数据",
    "返回公司名称和领域的列表"
  ]
}
```

用户确认后才进入下一步。如果用户修改问题或补充要求，回到本步骤重新规划。

> 使用的 subagent：`problem_clarifier`

### 1.3 整理输入证据

主控 agent 调用证据整理能力，统一读取本轮可用输入：

- 有上传文件：统一读取文件内容、字段信息、文本片段和必要的元数据。
- 无上传文件：基于用户问题生成 web 搜索计划，收集可用于 schema 构建的证据。
- 上传文件不足以回答问题：保留上传内容作为证据，同时使用 web 搜索补全。
- 待办：后续可按文件类型细分结构化/非结构化处理；当前阶段文件类型只作为读取方式和证据说明。

coordinator 根据以上判断生成 evidence plan。schema 构建和数据抽取都走统一 agent：

```json
{
  "sources": [
    {
      "source_id": "upload_001.xlsx",
      "source_kind": "upload",
      "file_type": "xlsx",
      "reason": "Excel 文件，含公司表、行业表，可作为实体和关系设计证据"
    },
    {
      "source_id": "upload_002.txt",
      "source_kind": "upload",
      "file_type": "txt",
      "reason": "文本文件，描述公司业务和行业归属，可作为实体和关系设计证据"
    }
  ],
  "needs_web_search": false,
  "handler": "schema_builder"
}
```

> 使用的 subagent：`evidence_collector`

### 1.4 构建 schema 草案

调用统一的 `schema_builder`。它读取用户上传文件、已保存 evidence 和必要的 web 搜索结果，直接构建当前问题所需的 ontology schema。

schema 草案保存为 Pydantic 模型文件，作为唯一事实源：

- `concepts/draft_schema.py`：Pydantic 模型文件。供 agent 推理、后续概念文件生成使用，同时也是表单渲染的来源。
- 表单 JSON 由后端渲染逻辑从 Pydantic 自动生成，不单独维护。
- 用户修改后由后端 schema 服务应用回 Pydantic 文件。

Pydantic 格式约定：

```python
# 实体：类名 PascalCase，注释标注 # entity_type: <type>
# 属性字段标注 Python 类型：str / int / float / bool
# 正向关系：List["TargetClass"]（多值）或 Optional["TargetClass"]（单值），关系类型由字段类型自动推断
# 反向关系：List["TargetClass"]，注释标注 # reverse
from typing import List, Optional

class Company:  # entity_type: Organization
    _id: str
    name: str
    country: Optional[str]
    operates_in_industry: List["Industry"]
    headquartered_in: Optional["Country"]

class Industry:  # entity_type: BusinessDomain
    _id: str
    name: str
    operates_in_industry_r: List["Company"]  # reverse

class Country:  # entity_type: Location
    _id: str
    name: str
    headquartered_in_r: List["Company"]  # reverse
```

### 1.5 判断 schema 是否可回答问题

调用 `schema_judger` 检查 schema 是否覆盖问题需求。

判断重点：

- 是否有答案实体。
- 是否有过滤条件需要的属性或关系。
- 是否能表达问题中的地点、行业、时间、数量、比较、排序等约束。
- 是否有足够 evidence 支撑 schema。

示例输出：

```json
{
  "answerable": false,
  "coverage_score": 0.62,
  "missing_requirements": [
    "Company 缺少 country 字段，无法过滤出美国公司",
    "Company 缺少 industry 字段，无法判断是否属于数据分析领域"
  ],
  "recommended_action": "web_search"
}
```

如果 `answerable=false`，由 `schema_builder` 基于缺失项继续搜索并补全 schema，然后再次判断。

> 使用的 subagent：`schema_judger`；信息不足时调用 `schema_builder` 补全

### 1.6 用户确认或修改 schema

schema 进入数据抽取前必须让用户确认。

展示内容：

| Head | Head Type | Head Value Type | Relation | Tail | Tail Type | Tail Value Type |
|---|---|---|---|---|---|---|
| Company | Organization | str | operates_in_industry | Industry | BusinessDomain | str |
| Company | Organization | str | headquartered_in | Country | Location | str |

同时展示 Python schema：

```python
from typing import List, Optional


class Company:
    _id: str
    name: str
    headquartered_in: Optional["Country"]
    operates_in_industry: List["Industry"]


class Industry:
    _id: str
    name: str
    operates_in_industry_r: List["Company"]  # reverse


class Country:
    _id: str
    name: str
    headquartered_in_r: List["Company"]  # reverse
```

提供两种修改方式：

**方式一：直接编辑表单并提交。** 前端将表单编辑结果提交给 coordinator，后端 schema 服务写入 Pydantic 文件，`schema_validator` 校验后直接确认，无需再次展示。

**方式二：自然语言回复。** coordinator 将自然语言转成 schema patch，修改后重新展示给用户确认。

自然语言示例：

- `确认`
- `把 headquartered_in 改成 located_in`
- `Company 增加 employees:int`
- `Industry 的类型改成 Sector`
- `删除 Country`

方式二的修改流程：

```text
用户自然语言修改
-> coordinator 转成 schema patch
-> 后端 schema 服务修改 Pydantic 文件
-> schema_validator 校验
-> 后端渲染逻辑重新生成表单 JSON
-> 再次展示给用户确认
```

确认后产物：

```text
runs/ontology_workspace_runs/<run_id>/concepts/confirmed_schema.py
```

展示给用户的表单由后端渲染逻辑从 `confirmed_schema.py` 渲染生成。

> 此步骤由 coordinator 直接处理，不调用 subagent

### 1.7 按确认 schema 构建实例化对象

确认后才允许查找数据并构建实例化对象。

当前统一调用 `data_extractor`。待办：后续可按结构化/非结构化来源扩展不同抽取策略。`data_extractor` 根据 confirmed schema 读取上传文件、evidence 和必要的 web 搜索结果，输出可被后续代码直接操作的数据文件。

统一输出：

| 文件 | 内容 |
|---|---|
| `data/instances.json` | 按 confirmed schema 实例化后的对象集合，包含实体、属性和对象引用 |
| `data/facts.csv` | 属性事实表，用于轻量筛选、统计和校验 |
| `data/relations.csv` | 对象之间的关系表，用于遍历关系和构建子图 |
| `intermediate/extraction_report.json` | 抽取统计：各类型数量、confidence 分布、未匹配字段 |

实例化对象和事实记录应尽量附带 `source_refs` 和 `confidence`。

`instances.json` 示例：

```json
{
  "Company": [
    {
      "_id": "company:palantir",
      "name": "Palantir",
      "country": "United States",
      "operates_in_industry": ["industry:data_analytics"],
      "source_refs": ["web_001#chunk_002"],
      "confidence": 0.88
    }
  ],
  "Industry": [
    {
      "_id": "industry:data_analytics",
      "name": "Data Analytics"
    }
  ]
}
```

> 使用的 subagent：`data_extractor`

### 1.8 构建智能体工作区

后端调用工作区构建逻辑，将 confirmed schema 和实例化数据整理成一个可被智能体直接访问、读写和执行代码的 run workspace。目标目录结构：

```text
runs/ontology_workspace_runs/<run_id>/
├── data/
│   ├── instances.json
│   ├── facts.csv
│   └── relations.csv
├── concepts/
│   ├── Entity.py
│   ├── Person.py
│   ├── Event.py
│   └── Relation.py
├── src/
│   ├── main.py
│   ├── rules.py
│   └── utils.py
└── intermediate/
    ├── evidence_manifest.json
    ├── extraction_report.json
    └── workspace_manifest.json
```

- `data/`：存储实例化对象和可查询的事实、关系表。
- `concepts/`：存储由 confirmed schema 拆分生成的概念对象定义，例如 `Entity.py`、`Person.py`、`Event.py`、`Relation.py`。实际文件名由 schema 中的概念决定。
- `src/`：存储模型编写的可执行代码，用于读取和操作 `data/`、`concepts/`，并完成问题求解。
- `intermediate/`：存放中间结果、证据清单、抽取报告、调试输出和工作区 manifest，不作为最终答案的主要事实源。

> 此步骤由后端 workspace 构建逻辑处理，不调用 subagent

### 1.9 调用智能体访问工作区回答

主控 agent 调用求解智能体，输入：

- 原始问题
- confirmed schema
- run workspace path

求解智能体在工作区内执行：

```text
读取 concepts/ 理解对象定义
-> 读取 data/ 中的实例、事实和关系
-> 在 src/ 中编写或修改可执行代码
-> 执行代码生成中间结果
-> 基于结果回答用户问题
```

最终回答包含：

- 直接答案
- 使用的 schema 版本
- 数据来源摘要
- web 来源 URL，若本轮使用了网络搜索

> 使用的 subagent：`workspace_solver`

## 2. Agent Workspace、AGENT.md、Tool

7 个 Agent：1 个主控 `ontology_coordinator`，6 个工作 subagent。

### 2.1 Subagent

| Subagent | 职责 |
|---|---|
| `ontology_coordinator` | 主控，路由任务、控制流程 gate、与用户交互 |
| `problem_clarifier` | 分析问题+文件，生成问题描述和解决步骤给用户确认 |
| `evidence_collector` | 统一整理上传文件和 web 证据，生成可追踪的 evidence plan |
| `schema_builder` | 基于问题、上传文件和证据构建或补全 schema |
| `schema_judger` | 判断 schema 是否足以回答问题 |
| `data_extractor` | 按 confirmed schema 构建实例化对象、事实表和关系表 |
| `workspace_solver` | 访问 run workspace，在 `src/` 中编写/执行代码并返回最终答案 |

### 2.2 AGENT.md

每个 agent/subagent 的具体任务说明放在自己的工作目录下的 `AGENT.md` 中，coordinator 只负责流程编排和 gate 控制。

约定：

- `harness` 默认读取 `agent.workspace/AGENT.md` 作为该 agent 的 system prompt 主体。
- 主 agent 和 subagent 必须配置不同 workspace，才能拥有各自独立的 `AGENT.md`。
- `skills/` 只作为可选补充：用于放可复用长文档、脚本或示例，不承载 subagent 的主任务说明。
- workspace tools 从当前 agent workspace 开始查找；如果子目录没有 `tools/`，则向上继承最近父 workspace 的 `tools/`。
- 如果 agent 显式配置了 `prompt`，则该 prompt 会替代默认的 `AGENT.md` 加载逻辑；ontology harness 默认不使用 `prompt` 覆盖。

| Agent | Workspace | 任务说明文件 |
|---|---|---|
| `ontology_coordinator` | `otology_agent_workspace/` | `AGENT.md` |
| `problem_clarifier` | `otology_agent_workspace/subagent_worksapce/problem_clarifier/` | `AGENT.md` |
| `evidence_collector` | `otology_agent_workspace/subagent_worksapce/evidence_collector/` | `AGENT.md` |
| `schema_builder` | `otology_agent_workspace/subagent_worksapce/schema_builder/` | `AGENT.md` |
| `schema_judger` | `otology_agent_workspace/subagent_worksapce/schema_judger/` | `AGENT.md` |
| `data_extractor` | `otology_agent_workspace/subagent_worksapce/data_extractor/` | `AGENT.md` |
| `workspace_solver` | `otology_agent_workspace/subagent_worksapce/workspace_solver/` | `AGENT.md` |

### 2.3 Tool

这里只列模型必须主动调用的工具。文件保存、schema 确认、表单渲染、工作区构建属于后端流程，不作为 Agent tool。

| Tool | 用途 | 输入 | 输出 |
|---|---|---|---|
| `source_reader` | 读取上传文件，提取字段信息、样例行、文本片段和元数据 | `file_paths`, `question` | `{sources: [{source_id, file_type, columns, sample_rows, chunks, metadata}]}` |
| `web_search` | 搜索网络内容 | `query` | `{results: [{title, url, snippet}]}` |
| `evidence_retriever` | 从已保存证据中取相关片段 | `query`, `source_ids`, `top_k` | `{chunks: [{evidence_id, source_id, text, score}]}` |
| `schema_validator` | 校验 Pydantic schema 是否可用 | `schema_path` 或 `schema_text` | `{valid, errors}` |


## 3. 工作区文件与通信

### 3.1 文件生命周期

| 文件 | 生成者 | 阶段 |
|---|---|---|
| `data/uploads/<id>` | 用户上传 / coordinator | 1.1 |
| `runs/ontology_workspace_runs/<run_id>/intermediate/evidence_manifest.json` | `evidence_collector` | 1.3 |
| `runs/ontology_workspace_runs/<run_id>/concepts/draft_schema.py` | `schema_builder` | 1.4 |
| `runs/ontology_workspace_runs/<run_id>/concepts/confirmed_schema.py` | 后端 schema 服务（经用户确认后） | 1.6 |
| `runs/ontology_workspace_runs/<run_id>/data/instances.json` | `data_extractor` | 1.7 |
| `runs/ontology_workspace_runs/<run_id>/data/facts.csv` | `data_extractor` | 1.7 |
| `runs/ontology_workspace_runs/<run_id>/data/relations.csv` | `data_extractor` | 1.7 |
| `runs/ontology_workspace_runs/<run_id>/intermediate/extraction_report.json` | `data_extractor` | 1.7 |
| `runs/ontology_workspace_runs/<run_id>/concepts/*.py` | 后端 workspace 构建逻辑 | 1.8 |
| `runs/ontology_workspace_runs/<run_id>/src/*.py` | `workspace_solver` | 1.9 |

### 3.2 Agent 间通信

Coordinator 与 subagent 之间传递结构化 JSON，不依赖聊天上下文。

| 调用方向 | 传递内容 |
|---|---|
| coordinator → `problem_clarifier` | `{question, upload_paths}` |
| `problem_clarifier` → coordinator | `{problem, steps}` |
| coordinator → `evidence_collector` | `{question, upload_paths}` |
| `evidence_collector` → coordinator | `{sources: [{source_id, file_type, source_kind, reason}], needs_web_search, evidence_manifest_path}` |
| coordinator → `schema_builder` | `{question, sources, evidence_manifest_path}` |
| `schema_builder` → coordinator | `schema_path (draft_schema.py)` |
| coordinator → `schema_judger` | `{question, schema_path}` |
| `schema_judger` → coordinator | `{answerable, missing_requirements}` |
| coordinator → `data_extractor` | `{schema_path, sources, evidence_manifest_path}` |
| `data_extractor` → coordinator | `{instances_path, facts_path, relations_path, extraction_report_path}` |
| coordinator → 后端 workspace 构建逻辑 | `{schema_path, instances_path, facts_path, relations_path}` |
| 后端 workspace 构建逻辑 → coordinator | `workspace_dir` |
| coordinator → `workspace_solver` | `{question, schema_path, workspace_dir}` |
| `workspace_solver` → coordinator | 最终答案 |


## 4. Schema 设计

Schema 以 Pydantic 模型文件为唯一事实源。表单双向转换由后端渲染逻辑完成，无需维护额外格式。

### 4.1 Pydantic 格式约定

实体类规则：

- 类名 PascalCase。
- 类注释用 `# entity_type: <type>` 标注实体语义分类。
- `_id` 字段固定为实体标识，类型 `str` 或 `int`。
- 属性字段标注 Python 类型：`str` / `int` / `float` / `bool`。
- 正向关系：`List["TargetClass"]`（多值）或 `Optional["TargetClass"]`（单值）。
- 反向关系：`List["TargetClass"]`，注释标注 `# reverse`。

关系类型由字段类型自动推断，schema 文件中不需要显式标注：

| Pydantic 字段 | 关系类型 |
|---|---|
| `Optional["X"]` | many_to_one |
| `List["X"]` | one_to_many / many_to_many |
| `Optional["X"]` 且 X 有反向 `Optional["Y"]` | one_to_one |

### 4.2 示例

```python
from typing import List, Optional

class Company:  # entity_type: Organization
    _id: str
    name: str
    country: Optional[str]
    operates_in_industry: List["Industry"]
    headquartered_in: Optional["Country"]

class Industry:  # entity_type: BusinessDomain
    _id: str
    name: str
    operates_in_industry_r: List["Company"]  # reverse

class Country:  # entity_type: Location
    _id: str
    name: str
    headquartered_in_r: List["Company"]  # reverse
```

### 4.3 表单转换

Pydantic → 表单流程：

```text
后端渲染逻辑解析 Pydantic 类
-> 提取 entity：类名、entity_type、value_type
-> 提取 relation：head、tail、relation_type（由字段类型推断）
-> 生成扁平表单 JSON
-> 前端渲染可编辑表单
```

用户修改表单 → Pydantic 流程：

```text
前端提交修改后的表单 JSON
-> 后端 schema 服务对比 diff
-> 写入新的 Pydantic 文件
-> schema_validator 校验
```

表单 JSON 由后端渲染逻辑自动生成，结构如下：

```json
[
  {"type":"entity","name":"Company","entity_type":"Organization","value_type":"str"},
  {"type":"entity","name":"Industry","entity_type":"BusinessDomain","value_type":"str"},
  {"type":"relation","head_entity":"Company","relation":"operates_in_industry",
   "relation_type":"many_to_many","tail_entity":"Industry"}
]
```

## 5. 数据与工作区格式

抽取输出以实例化对象为主，事实表和关系表为辅。智能体优先读取 `instances.json`，在需要统计、过滤或遍历关系时使用 `facts.csv` 和 `relations.csv`。

### 5.1 instances.json

```json
{
  "Company": [
    {
      "_id": "company:palantir",
      "_concept": "Company",
      "name": "Palantir",
      "country": "United States",
      "operates_in_industry": ["industry:data_analytics"],
      "source_refs": ["web_001#chunk_002"],
      "confidence": 0.88
    }
  ],
  "Industry": [
    {
      "_id": "industry:data_analytics",
      "_concept": "Industry",
      "name": "Data Analytics"
    }
  ]
}
```

字段说明：

| 字段 | 说明 |
|---|---|
| `_id` | 实例标识，推荐格式 `{concept}:{normalized_name}` |
| `_concept` | 对应 concepts 目录中的概念类名 |
| 普通字段 | schema 中定义的属性字段 |
| 关系列表字段 | schema 中定义的关系字段，值为目标实例 `_id` |
| `source_refs` | 数据来源引用 |
| `confidence` | 置信度 0~1 |

### 5.2 facts.csv

属性事实表，每行表达一个实例的一个属性值：

```csv
subject,concept,attribute,value,value_type,source_refs,confidence
company:palantir,Company,country,United States,str,web_001#chunk_002,0.88
```

### 5.3 relations.csv

关系表，每行表达两个实例之间的一条关系：

```csv
subject,subject_concept,relation,object,object_concept,source_refs,confidence
company:palantir,Company,operates_in_industry,industry:data_analytics,Industry,web_001#chunk_002,0.88
```

### 5.4 intermediate/

`intermediate/` 用于存放可追踪但不直接面向用户的中间结果：

```json
{
  "evidence_manifest": "本轮使用了哪些上传文件、web 页面和片段",
  "extraction_report": "抽取数量、置信度、缺失字段、冲突记录",
  "workspace_manifest": "data/concepts/src 的文件清单和生成时间"
}
```

### 5.5 extraction_report.json

```json
{
  "total_instances": 18,
  "total_facts": 42,
  "total_relations": 16,
  "entity_count": 18,
  "relation_types_used": ["operates_in_industry"],
  "avg_confidence": 0.87
}
```

## 6. Workspace 结构

```text
otology_agent_workspace/
├── AGENT.md
├── subagent_worksapce/
│   ├── problem_clarifier/
│   │   └── AGENT.md
│   ├── evidence_collector/
│   │   └── AGENT.md
│   ├── schema_builder/
│   │   └── AGENT.md
│   ├── schema_judger/
│   │   └── AGENT.md
│   ├── data_extractor/
│   │   └── AGENT.md
│   └── workspace_solver/
│       └── AGENT.md
├── tools/
│   ├── __init__.py
│   ├── source_reader.py
│   ├── evidence_retriever.py
│   └── schema_validator.py
├── utils/
│   ├── problem_clarifier_contract.py
│   ├── valid_company_schema.py
│   ├── invalid_missing_id.py
│   └── invalid_unknown_relation.py
├── data/
│   ├── uploads/
│   └── evidence/
├── runs/
│   └── <run_id>/
│       ├── data/
│       │   ├── instances.json
│       │   ├── facts.csv
│       │   └── relations.csv
│       ├── concepts/
│       │   ├── confirmed_schema.py
│       │   ├── Entity.py
│       │   ├── Person.py
│       │   ├── Event.py
│       │   └── Relation.py
│       ├── src/
│       │   ├── main.py
│       │   ├── rules.py
│       │   └── utils.py
│       └── intermediate/
│           ├── evidence_manifest.json
│           ├── extraction_report.json
│           └── workspace_manifest.json
└── memory/
    └── MEMORY.md
```

## 7. 工具设计

这里只说明模型必须调用的工具。

`otology_agent_workspace/tools/` 只放 agent 可调用工具。格式校验、schema fixture、后端流程辅助函数放在 `otology_agent_workspace/utils/`，不注册为 agent tool。

### 7.1 `source_reader`

统一读取上传文件，输出字段信息、样例行、文本片段和文件元数据。待办：后续可将读取结果扩展为结构化表格摘要、文本 chunk、页面内容等更细的 evidence 类型。

### 7.2 `web_search`

无上传文件或 schema 信息不足时搜索网络，返回标题、URL、摘要片段。

### 7.3 `evidence_retriever`

根据问题、source_id 或关键词，从已保存 evidence 中取相关文本片段。

### 7.4 `schema_validator`

校验 Pydantic schema 是否语法正确，字段类型和实体关系是否可用。

## 8. harness 配置草案

模型配置按实际 provider 填写。

```json
{
  "defaults": {
    "model": "deepseek/deepseek-v4-flash",
    "tools": { "allow": ["*"], "deny": [] },
    "max_steps": 80
  },
  "agents": [
    {
      "id": "ontology_coordinator",
      "type": "coordinator",
      "workspace": "otology_agent_workspace",
      "subagents": [
        "problem_clarifier", "evidence_collector",
        "schema_builder",
        "schema_judger",
        "data_extractor",
        "workspace_solver"
      ],
      "default": true
    },
    { "id": "problem_clarifier", "workspace": "otology_agent_workspace/subagent_worksapce/problem_clarifier", "tools": { "allow": ["source_reader"], "deny": [] } },
    { "id": "evidence_collector", "workspace": "otology_agent_workspace/subagent_worksapce/evidence_collector", "tools": { "allow": ["source_reader", "web_search", "evidence_retriever"], "deny": [] } },
    { "id": "schema_builder", "workspace": "otology_agent_workspace/subagent_worksapce/schema_builder", "tools": { "allow": ["source_reader", "web_search", "evidence_retriever", "schema_validator"], "deny": [] } },
    { "id": "schema_judger", "workspace": "otology_agent_workspace/subagent_worksapce/schema_judger", "tools": { "allow": ["schema_validator"], "deny": [] } },
    { "id": "data_extractor", "workspace": "otology_agent_workspace/subagent_worksapce/data_extractor", "tools": { "allow": ["source_reader", "web_search", "evidence_retriever"], "deny": [] } },
    { "id": "workspace_solver", "workspace": "otology_agent_workspace/subagent_worksapce/workspace_solver", "tools": { "allow": ["execute_code"], "deny": [] } }
  ]
}
```

## 9. 示例 schema

问题：美国有哪些数据分析的公司

```python
from typing import List, Optional

class Company:  # entity_type: Organization
    _id: str
    name: str
    country: Optional[str]
    operates_in_industry: List["Industry"]

class Industry:  # entity_type: BusinessDomain
    _id: str
    name: str
    operates_in_industry_r: List["Company"]  # reverse
```

## 10. 实施顺序

1. 创建 `otology_agent_workspace/`、各 subagent 工作目录及其 `AGENT.md`，并创建 `harness.json`
2. 实现 `source_reader`、`evidence_retriever`、`schema_validator`
3. 实现后端 schema 保存、表单渲染、工作区构建逻辑
4. 跑通 `schema_builder` → `schema_judger` → 用户确认
5. 实现统一数据抽取，生成 `instances.json`、`facts.csv`、`relations.csv`
6. 接入 `workspace_solver`，在 run workspace 中执行代码回答问题

## 11. MVP 标准

1. 无上传文件时 web 搜索构建 schema
2. 有上传文件时统一读取文件并构建 schema
3. 上传文件不足时能结合 web 搜索补全 schema
4. schema 展示为确认表格和 Python class
5. 用户能确认或修改 schema
6. 构建实例化对象、事实表和关系表
7. 构建包含 `data/`、`concepts/`、`src/`、`intermediate/` 的 run workspace
8. 调用智能体访问工作区并返回答案
