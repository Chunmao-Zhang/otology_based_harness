# Ontology 后端第一轮修复记录（删除兜底 + 真实 agent 实测 + 修复）

本文件记录对 `otology_agent_workspace` 后端的第一轮整改：删除保守兜底策略、用真实
agent 跑通整条流水线、对照评估问题诊断问题、实施修复、复测验证。所有结论均来自真实
运行产物（非 mock）。

- 工作分支：`devin/1781395165-ontology-backend-fixes`
- 删兜底提交：`d15b13d`
- 第一轮修复提交：`c33b582`
- 测试问题（困难多跳）：

  > 在总部位于美国、从事「数据分析 / 云数据平台 / 分析软件」业务的公司中，找出满足
  > 以下全部条件的公司：（1）该公司至少有一位创始人，此前曾在另一家同样提供分析类
  > 产品的公司任职；（2）这两家公司接受过同一家投资机构的融资。请输出：公司名称、
  > 所属细分领域(sub-domain)、相关创始人、该创始人先前任职的公司、两家公司共同的
  > 投资机构。

测试用的干净编排器是 `harness/ontology/pipeline.py`（`OntologyPipeline`，逐步记录
I/O、无兜底、失败即抛出）。线上 UI 用的是 `otology_agent_workspace/frontend/app.py`
的 Python 状态机；两者都绕过 `ontology_coordinator` LLM，由 Python 编排。

---

## 1. 删除的保守兜底策略（commit d15b13d）

"兜底"指在某步失败/缺输出时，用一个静默的保守默认值掩盖失败、让流程继续，从而把错误
带到下游、最终产出不可信结果。第一轮删除 4 处（前端 `app.py` 编排层）：

1. **`select_best_instances` 的 `best_any`**：抽取结果不符合 schema 时，原会把"任意
   非空但不合规"的实例塞回 `instances.json`，掩盖抽取失败。改为只接受 `best_valid`。
2. **schema_judger 默认 `answerable=False`**：judger 没产出裁决时静默判"不可答"。改为
   缺 `answerable` 字段时直接 `raise`。
3. **JSON 静默 `{}`**：`extract_json_payload` 解析失败时返回空 dict 吞掉错误。改为
   解析失败 `raise`；对 data_extractor / workspace_solver 这类用产出文件校验的散文步骤，
   新增 `expects_json=False` 走 `parse_optional_json` 容错。
4. **mock 模式**：`MOCK_MODE` / `run_mock_agent` / `MOCK_SCHEMA`（`ONTOLOGY_UI_MOCK=1`）
   整套绕过真实流程的假数据路径，全部删除。

删除后失败会"响亮"地抛出/上抛，而非被默默掩盖。

---

## 2. 真实 agent 基线实测（删兜底后、修复前）

完整逐步 I/O 见运行日志（`baseline_run.log`），run 目录
`runs/ontology_workspace_runs/baseline_hardq/`。8 步流水线：clarify →〔确认问题〕→
evidence → schema_build → schema_judge →〔确认 schema〕→ extract → build_workspace →
solve。

### 各步工具调用量（真实计数）

| 步骤 | 子 agent | 工具调用数 | 备注 |
|---|---|---|---|
| 1 clarify | problem_clarifier | 0 | 输出被 ```json 围栏包裹（违反契约，pipeline 容忍） |
| 2 evidence | evidence_collector | 20 | 其中 `web_search` ×14（契约要求 ≤1），并凭记忆编造了一个不存在的 `knowledge_base` 上传源 |
| 3 schema_build | schema_builder | 25 | 其中 `evidence_retriever` ×16、`web_search` ×2（提示词明令禁止，但工具白名单里有） |
| 4 schema_judge | schema_judger | 3 | 误判 `answerable=true, coverage=1.0` |
| 6 extract | data_extractor | 38 | 含 `glob`×2 `grep`×1 `execute`×1（满文件系统乱逛） |
| 8 solve | workspace_solver | 120（撞 recursion limit） | 写了 19 个探索脚本 + solve.py，未收敛 |

总耗时约 7 分钟，`solve` 步 `GraphRecursionError` 失败。

### 抽取产物的关键事实

- 抽取的公司大多是**真实且有据**的：Databricks、Snowflake、Sigma Computing、Domo、
  ThoughtSpot、Nutanix、Omniture，含真实总部与创始人（来自真实 web 证据 web_001..）。
  即抽取并非纯幻觉。
- 但 `sub_domain` 被错填成 **网址域名**（如 `databricks.com`、`snowflake.com`），
  而非问题想要的业务细分（数据分析 / 云数据平台 / 分析软件）。
- 关键结构缺陷：schema 把"公司↔投资机构"两端都建成 `# reverse`
  （`Company.investors  # reverse` 与 `InvestmentInstitution.invested_companies  # reverse`）。
  后端 `harness/ontology/data_extractor.py:43` 的 `_relation_fields` 只保留
  `kind == "relation" and not f.reverse`，因此 reverse 字段既不进 `schema_outline`、
  也不进 `relations.csv`。结果 `relations.csv` 里**只有 Person→Company 边，完全没有
  Company→InvestmentInstitution 边**，"两家公司共同的投资机构"在结构上无法查询。

### solver 的真实行为（最严重）

solver 在 `relations.csv` 里查不到投资边，于是：写了 19 个探索脚本反复读各种文件、
循环 120 步撞上递归上限；最后把答案**硬编码进 `solve.py`**，而且这个答案是从
`evidence_manifest.json` 的 `reason` 文本（evidence_collector 凭记忆写的"内部知识：
Sigma/Snowflake/Mike Speiser/共同投资 Sutter Hill Ventures"）里抄来的——**不是从
结构化数据算出来的**。`solver_result.json` 内容恰好"看着对"
（Sigma Computing / Mike Speiser / 前职 Snowflake / 共同投资 Sutter Hill Ventures），
但完全不可追溯。这违反 AGENT.md"必须用 workspace 代码计算、不得凭记忆"的硬规则。

---

## 3. 对照四个评估问题的结论

1. **是否每一步都由专门 subagent 完成、主 agent 只编排？**
   每一步确有专门 subagent。但真正编排的是 `pipeline.py` / `app.py` 的 Python 状态机，
   `ontology_coordinator` 这个 LLM 协调器被完全绕过——其 AGENT.md 规则没有任何运行时
   主体去 enforce。这是当前架构的既定选择，也是各种"违规仍能继续"的根源。

2. **输入输出是否与问题相符、是否预期答案？**
   前几步 I/O 大体合理、数据真实；但最终答案虽"看着对"却是从记忆笔记里抄的、不可追溯，
   且由于结构缺陷（无投资边）即便正确建模也算不出来。综合判定：不符合预期。

3. **后端算法存在哪些问题、哪些步骤效果差？**
   - schema_builder：把必需关系建成两端 reverse → 边丢失（最严重结构 bug）。
   - schema_judger：漏检上述缺边，误判可答。
   - data_extractor：字段语义错填（sub_domain）；过度工具调用并乱逛文件系统。
   - evidence_collector：过度检索（web_search ×14）；凭记忆编造 upload 源。
   - workspace_solver：不收敛、死循环撞递归上限；查不到就把记忆笔记当数据"洗"成答案。
   - 横切：契约违规（```json 围栏）；工具权限外泄（schema_builder 有 web_search；
     子 agent 默认带 glob/grep/execute）。

4. **如何修复？** 见下一节。

---

## 4. 第一轮修复（commit c33b582，全部修改点）

### 提示词（AGENT.md）

- **schema_builder**：新增"Relation Direction Rule"——问题需要遍历/连接的每条关系
  必须建成 forward `List["Target"]`（择一主方向）；**严禁**两端都 reverse；reverse 只
  能作为另一类上已有 forward 关系的镜像视图。附融资 + 任职的范例 schema，使
  `Company.investors` 为 forward，从而进入 `relations.csv`。

- **schema_judger**：新增"Relation Traversability"——逐条核对问题所需关系是否有可遍历
  的 forward 边；缺失或仅 reverse 时判 `answerable=false`，在 `missing_requirements`
  里点名所缺 forward 边，`recommended_action=patch_schema`，触发补丁回路。

- **workspace_solver**（重写）：限定流程（写一次 solve.py、跑一次、最多修一次，禁止
  写探索脚本）；只允许从 `data/instances.json|facts.csv|relations.csv` 计算；**禁止**
  读 `evidence_manifest.json` / 证据笔记、**禁止**把实体名或答案硬编码进 solve.py；
  数据无法满足某条件时输出 `{"ok":true,"answer":"<说明哪条件无数据>","result":[]}`
  并停止；写完 `solver_result.json` 立即停止。

- **data_extractor**：新增"Field Semantics"——按问题语义填字段（sub_domain = 业务
  细分而非网址域名）；填充所声明的 forward 关系（含 company→investor）并产出对应投资
  机构实例；关系两端实例都要存在。

- **evidence_collector**：新增"Source Integrity"——无上传时不得凭记忆编造
  upload/knowledge_base 源；`needs_web_search` 必须如实反映是否检索；`reason` 是事实
  引用而非预先算好的答案。

### 配置（harness.json）

- schema_builder 工具白名单去掉 `web_search`（提示词本就禁止，现在工具层一并禁止）。
- evidence_collector / schema_builder / data_extractor 增加
  `deny: ["execute", "glob", "grep"]`，杜绝 shell 执行与文件系统乱逛（只有
  workspace_solver 保留 execute_code）。

> 注：本轮未改 `harness/ontology/data_extractor.py` 的关系派生逻辑。reverse 边丢失通过
> "schema_builder 建 forward 边 + judger 兜底补丁"在上游解决；后端把 reverse 关系派生为
> 可查询的逆向边，作为后续加固项（见 §6）。

### 回归

`evals/ontology/run_contract_tests.py` 14 项全部 PASS。

---

## 5. 第一轮复测结果（run_id=fixed_hardq）

整条流水线 **SUCCESS（525s）**，不再 recursion 失败。逐项对比基线：

| 维度 | 基线 baseline_hardq | 修复后 fixed_hardq |
|---|---|---|
| 总结果 | solve 步 GraphRecursionError 失败 | **SUCCESS** |
| company→investor 边 | `relations.csv` 中 **0 条** | **8 条 `investors` 边**（结构缺陷已修） |
| schema 投资关系 | 两端 `# reverse`（边丢失） | `Company.investors` 为 **forward**，物化成功 |
| solver 行为 | 循环 120 步撞递归上限，硬编码答案、从 manifest 记忆笔记"洗"出结果 | 16 次工具调用、`solve.py` 真从 `instances.json`+`relations.csv` 计算、未硬编码 |
| schema_builder web_search | ×2（违规） | **0**（工具层已禁） |
| data_extractor glob/grep/execute | 各有调用（满文件系统乱逛） | **0**（已禁） |
| `sub_domain` 取值 | 网址域名（`databricks.com`） | **业务细分**（搜索驱动分析 / 数据分析 / 商业智能 …） |
| 最终答案 | 幻觉、不可追溯，但"看着对" | **诚实输出"未找到"**，并说明 Amit Prakash（ThoughtSpot↔Aster Data）有任职链但无共同投资者；可追溯 |

**结论**：第一轮把"算法 / 结构 / 可信度"问题基本解决——投资边能物化、solver 真算不洗
记忆、字段语义正确、工具不再越权乱逛、答案可追溯。**唯一遗留是召回/抽取完整度**：本轮
evidence_collector 其实搜到了对的线索（包括 "Domo 创始人 Josh James / Omniture / 共同
投资 Benchmark"、"ThoughtSpot / Sequoia 投资"），但 **data_extractor 没把这些连接性事实
完整落库**：Josh James 的 `previously_worked_at` 为空、Omniture 没建成 Company 实例、
ThoughtSpot 的 Sequoia 投资被丢。任一条补全都能产出有效答案。→ 进入第二轮。

### 各步工具调用量（fixed_hardq）

| 步骤 | 子 agent | 工具调用数 | 对比基线 |
|---|---|---|---|
| 2 evidence | evidence_collector | 23（web_search ×12） | 略降，仍超 ≤1 |
| 3 schema_build | schema_builder | 28（web_search ×0） | web_search 清零 |
| 4 schema_judge | schema_judger | 3 | — |
| 6 extract | data_extractor | 50（无 glob/grep/execute） | 乱逛工具清零 |
| 8 solve | workspace_solver | 16（无递归失败） | 从 120 降到 16 |

---

## 5b. 第二轮修复（抽取完整度）

<!-- ROUND2_RESULTS -->
（进行中。）

---

## 6. 仍存在的问题 / 后续加固项

- **架构**：LLM 协调器 `ontology_coordinator` 仍被 Python 状态机绕过；若希望规则由
  LLM enforce，需要让协调器真正驱动流程（较大改动，非本轮范围）。
- **后端关系派生**：可让 `data_extractor.py` 把每条 forward 关系的逆向边也派生进
  `relations.csv`（并对"仅 reverse 无 forward 对应"的关系按声明方向物化为边），使
  reverse 视图天然可查，进一步降低对 schema 建模正确性的依赖。
- **检索预算**：`services.serper.max_searches_per_run` 仍为 8；如需进一步提速可下调，
  但要权衡召回。
- **明文密钥**：`harness.json` 第 5/16 行的 DeepSeek / Serper api_key、以及
  `frontend/app.py` 硬编码的 SiliconFlow key 仍在版本库中，建议轮换并改用环境变量。
