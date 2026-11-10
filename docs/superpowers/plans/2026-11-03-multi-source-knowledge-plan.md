# 多来源知识驱动升级 · 第一轮实施方案（仅审查与设计，未改任何代码）

**Goal:** 让系统在岗位资料不足时仍能产出可信规划，且用户能看懂每条结论来自哪个来源、有没有原文依据。

**Architecture:** 不新建数据模型层，而是在现有 `job_evidence → job_requirements → skill_gaps → learning/project` 链路上，把已散落在 9 套枚举里的"来源"信息收敛成**一个派生视图函数**；岗位画像内容（core_tasks/career_path 等）从"已送达浏览器但无人渲染"补成展示层；外部资料走 `knowledge/sources/` 新目录，复用 YAML 里已存在却没有读取方的 `evidence.source_id` 结构。

**Tech Stack:** Python 3 + FastAPI + LangGraph + LangChain（不变）；Vue 3 + Vitest（不变）；JSON 文件存储（不变）；**零新增依赖**。

**Spec:** 用户任务书《AI Career Agent 多来源知识驱动升级实施任务书》（2026-11-03）。

**Global Constraints（逐条抄自任务书，每个阶段都受这些约束）：**
- 不得修改真实用户数据 `runs/users/user_2`、`user_3`（含 `user_3/job_profiles/54bc7f29….json`）。
- 不得删除或覆盖成长历史原始记录（`growth.json` 只能 append / void）。
- 不得擅自 commit 或 push；工作区现有 42 项未提交改动不得覆盖、清理或 reset。
- 不得修改 `.env`，不得泄露 `CLOUD_API_KEY`、`JWT_SECRET`。
- 所有验证使用独立测试数据。
- 不得为新功能破坏现有功能；不得随意增加 Agent；保留现有七节点架构。
- 禁止方向：低代码 / MCP 与外部工具 / GitHub-Gitee 仓库核验 / 文件上传 / 向量库 / 自建观测 / 模型微调 / `plan_id` 重构 / **Schema 版本升级** / 大量增加 Agent / 无必要的数据库中间件 / 额外增加「未经确认」一类无效校验标记。
- 第一轮只审查与设计：不改代码、不装依赖。

---

## 1. 当前架构分析

### 1.1 七节点真实契约（`graph/workflow.py:34-45`）

| 节点 | 实现 | 读 state | 写 state | 调模型 |
|---|---|---|---|---|
| profile | `agents/profile_agent.py:79` | `user_profile` | `user_skills` `user_strengths` `user_weaknesses` | 1（无技能无项目时 0） |
| growth | `agents/growth_agent.py:191` | `user_skills` `growth_events` `target_job` | `user_skills` `user_skills_before` `skill_updates` `growth_diff` | **0** |
| retrieval | `agents/retrieval_agent.py:56` | `target_job` `jd_text` `snapshot_*` | `job_evidence` `retrieval_status` `retrieval_diagnostics` | **0（只切句，不查 knowledge/）** |
| job | `agents/job_agent.py:711` | `target_job` `job_evidence` `retrieval_status` `job_profile` `enable_job_profile` `snapshot_*` + `JobCatalog` | `job_requirements` `job_profile` `job_profile_origin` `job_profile_warnings` `job_requirements_basis` `job_requirements_grounded` | 1–2 |
| assessment | `agents/assessment_agent.py:459` | `user_skills` `job_requirements` | `skill_gaps` `assessment_answers` | 1 |
| learning | `agents/learning_agent.py:848` | `skill_gaps` `target_job` `user_skills` | `learning_plan` | 1 |
| project | `agents/project_agent.py:643` | `skill_gaps` `learning_plan` `user_skills` `target_job` | `project_plan` | 1 |

`final_plan` 不在图内，由 `graph/workflow.py:150` `build_final_plan()` 用确定性代码汇总。

### 1.2 来源与证据机制：已经有 9 套枚举，不是从零起步

一条 `job_requirements[i]` 当前有 10 个字段，**模型只填 5 个，其余全部由代码回填并覆盖**（`agents/job_agent.py:694-708`）：

| 字段 | 合法值 | 谁赋值 |
|---|---|---|
| `skill` | 字符串，可被改写为规范名 | 模型 → 代码改写 |
| `importance` | 1–5 | 代码（画像级取 max，否则沿用模型值并标 `model_generated`） |
| `level` | 入门/初级/中级/高级 | 代码按 `level_basis` 覆盖 |
| `reason` | 一句话 | 模型 / 画像 YAML |
| `evidence_status` | `supported` / `partially_supported` / `model_supplement` | **纯代码** |
| `evidence_refs` | `E01…` 编号 | 代码回查，模型自报一律丢弃 |
| `source_companies` | 公司 slug 列表 | 代码（生产路径恒空） |
| `source_notes` | 原文句子列表 | 代码 |
| `importance_basis` | `profile_skill_importance` / `model_generated` | 代码 |
| `level_basis` | `{origin: jd_text/profile_level/model_inferred, quote, by_company}` | 代码 |

现存 9 套"来源"词表：`job_evidence[].provenance`、`retrieval_status`、`job_requirements_basis`、`evidence_status`、`importance_basis`、`level_basis.origin`、`job_profile[].source`、`job_profile_origin`、`evidence_kind`。

**结论：任务书设想的 skill / importance / evidence_status / source_text / skill_level / skill_level_source 六项，现状已具备，只是名字和存放位置散。** 真缺的只有四项：
1. 没有单一 `source_type` 口径（散在 9 套里，且三套互不同名）。
2. `official_data` 完全不存在（全库搜 `official/官方/职业标准/source_link/url` 无命中），但 `knowledge/jobs/*.yaml` 的 `skills[].evidence[]` 已有 `{source_id, requirement_type, context, note}` 结构（会计/运营/教师用 `source_*_std_001`/`source_*_jd_001`），**只是生产代码没有任何读取方**——向量检索下线时这条链断了（`agents/skill_dict.py:8-11` 有说明）。
3. `verified` / `user_claimed` 只存在于**学生侧**（`user_skills[].evidence_kind`、assessment 的 `verification`），要求条目侧没有、也不该有。
4. 一次只能一份 JD（请求体 `jd_text: str`，`backend/schemas.py:81-85`，上限 6000 字），同名技能的重要程度**不是取最大值而是保留首条**（`job_agent.py:857` `dedupe_by(key="skill")` 保 first），因为用户原文路径下 `importance` 恒为 `None`（`agents/user_jd.py:84-98`）。

### 1.3 关键发现：阶段四要的展示，大半数据已经在浏览器里

`backend/analyze.py:45-63` 的对外白名单已经把画像三兄弟送出接口，但 `web/src` 全量搜索 **0 处读取**：

| 字段 | 已送达 | 前端读取处 |
|---|---|---|
| `job_profile`（summary / core_tasks / tools / deliverables / entry_requirements / career_path，每条带 `source: jd/standard/model`） | ✅ | **0** |
| `job_profile_origin`（`cache`/`knowledge_base`/`generated`/`unavailable`） | ✅ | **0** |
| `job_profile_warnings` | ✅ | **0** |
| `job_requirements_grounded` | ✅ | 0（只读 `goal.grounded`） |
| `latest_round.snapshot`（含 `jd_text`/`job_evidence`/`job_requirements`/basis，`GET /api/tasks` 原样返回） | ✅ | 0（`TasksView.vue` 只读 `.learning_plan/.project_plan`） |

而已经做完的：JD 粘贴入口（`AnalyzeView.vue:196-212` textarea + `jd_text` + 6000 字拦截 + 原文过期提醒）、逐条要求展示与原文反显（`JobRequirementTable.vue:117-148` 有 `evidence_status` 三态徽标、`importance_basis`、`level_basis.quote`、可点 E 编号芯片）、来源中文文案（`enums.js` 的 `requirementBasisText`/`evidenceStatusText`/`levelOriginText`/`retrievalStatusText`）、折叠容器 `details.fold`、来源注脚 `.panel-src`。

**含义：阶段四 ①②④ 三块主要工作量在"渲染已到达的数据 + 一个 6 行的枚举映射"，不在建管道。**

### 1.4 任务书前提与现状不符的四处（必须先对齐）

| 任务书写法 | 代码现状 | 影响 |
|---|---|---|
| "改造 retrieval 节点：查询现有岗位知识" | retrieval **不碰 `knowledge/`**，只把 `jd_text` 切句；知识库岗位匹配在 **job 节点** `job_agent.py:726-736` 的 `catalog.resolve_profile` | 按字面搬动会切断 basis 判定链（见 §8 风险 R1），需重新划边界 |
| "为不同来源建立统一结构"（新枚举） | 已有 9 套枚举 + 三套互不同名的"来源"轴；`graph/state.py:71` `check_update` 白名单会拦任何新 state 键 | 只能做**派生视图**或**条目级加键**，不能新造第四套平行枚举写进 state |
| "evidence_status 加 verified / user_claimed" | 这两个概念属于**掌握侧**（`evidence_kind` + `verification`），要求侧只有 supported/partial/model_supplement | 把两条轴合并会让 assessment 的诚实性不变量（`golden/scoring.py:213-230`）失去判定依据 |
| "多份 JD 的重要程度分别保留、聚合取最大" | 只支持 1 份；`source_id` 恒为 `"user_pasted"`，句子无法回溯属于哪份 JD | 要做必须先给 evidence 加真实 `source_id`，动的是全站耦合最紧的那个结构 |

---

## 2. 数据结构设计（对应阶段一）

### 2.1 核心决策：派生视图，不新建存储结构

新增**纯函数模块** `agents/source_attribution.py`，从既有条目字段算出统一视图，**不写进 state、不写进存储**：

```python
SOURCE_KNOWLEDGE_BASE = "knowledge_base"
SOURCE_USER_JD        = "user_jd"
SOURCE_OFFICIAL_DATA  = "official_data"   # 本方案新增，见 §2.5
SOURCE_MODEL          = "model_inference"

def requirement_source(req: dict) -> str: ...
def requirement_citation(req: dict) -> dict:  # {source_type, source_id, quotes}
    ...
```

为什么这样而不是新加字段：零迁移、零 `schema_version` 触碰、`check_update` 白名单不受影响、旧评测结果与新代码可同时解析、前端拿到的历史 run 结果不会因为缺字段而崩（现有页面已有"缺字段退回推断"的先例，`stores/analysis.js:75-79`）。

### 2.2 派生规则（唯一真值表，避免三套枚举互相打架）

| 条目状态 | 派生 `source_type` | 派生依据性质 |
|---|---|---|
| `evidence_refs` 能回查到 `provenance=user_pasted` 的证据 | `user_jd` | `explicit` |
| 同上但只命中别名/子串（`evidence_status=partially_supported`） | `user_jd` | `explicit`（弱） |
| `importance_basis=profile_skill_importance` 或 `level_basis.origin=profile_level` | `knowledge_base` | `explicit`（人工整理） |
| `evidence_refs` 回查到 `source_id` 以 `source_std_` / `source_jd_` 开头的外部资料（§2.5 起才可能） | `official_data` | `explicit`（可反查，非用户当场提供） |
| `evidence_status=model_supplement` | `model_inference` | `inferred` |

`evidence_status` 保持三值不改名（改名 = 前端 6 处文案 + `test_golden_cases.py` 全崩）。**要求侧不引入 `verified`/`user_claimed`**：这两个值继续只活在学生侧 `evidence_kind`（project/growth/self_report/none/unknown）与 `verification`（direct/inferred/unverified）里，两条轴在 UI 上并列显示、不合并。

### 2.3 多来源并存的最小扩展（条目级加键，不动结构）

`job_evidence` 每条加两个键（`agents/user_jd.py:84-98` 构造点写入）：

```python
"source_id": f"jd_{n:02d}",        # 替代恒定的 "user_pasted"
"jd_index": n,                     # 属于第几份 JD；现有单份路径恒为 1
```

`provenance` 保持 `"user_pasted"` 不变 —— 这是兼容关键：`job_agent._attach_evidence`、`jd_match` 分母、`job_profile._valid_refs` 三处都靠 `provenance=="user_pasted"` 判断（见 §8 R1），改它风险远大于收益；新键只做加法。

多份 JD 的重要程度改为"分别保留 + 聚合取最大"：

```python
"importance_by_source": {"jd_01": 5, "jd_02": 3},   # 各来源独立值
"importance": 5,                                      # 现有 max 语义不变
```

现状"同名技能保留首条"（`job_agent.py:857`）改成保留 importance 最大的那条并把 `source_notes`/`evidence_refs` 合并去重。**注意**：这一步依赖 §2.3 的 `source_id`，属于阶段一的最后一格，可独立砍掉而不影响其它设计（见 D2）。

### 2.4 模型推断的隔离补齐

现状只有 2 处排除推断（`jd_match.py:102-115` 分母、`goal.py:57-92` 禁止自封达成）；**缺口生成、学习规划、项目规划、成长记账都不排除**（`assessment_agent.py:303` 全量遍历、`learning_agent.py` 全文无 `model_supplement` 引用）。

补齐方式不改判定逻辑、只加标注：
- gap 条目透传 `requirement_source`（`assessment_agent.py:374-390` 已在透传 `evidence_kind`，同位置加一个键）。
- 学习/项目阶段把"来源=模型推断"的缺口排在真实缺口之后，但**不删除**（删了用户就少一条建议，与"库薄不能成为不工作的理由"冲突）。
- 评测里加一条不变量：`job_requirements_basis=user_pasted` 时，模型补充项不得进入 `jd_match` 分母（现有行为，锁死它）。

### 2.5 `official_data` 的落地方式（复用断链，不新建结构）

外部资料文件放 **`knowledge/sources/`**（新目录）——必须新目录，因为三个 loader 都 glob 各自目录，形状不符会让 `JobCatalog` / `SkillKnowledgeBase` / `ProjectKnowledgeBase` 直接加载失败：

```yaml
# knowledge/sources/accountant-jd-anon-001.yaml
source_id: source_acct_jd_001      # 与 jobs YAML 里 evidence.source_id 同命名法
origin_type: public_jd_summary     # public_jd_summary | official_standard
job_id: accountant
collected_at: "2026-11-03"
review_status: reviewed            # reviewed | pending_verification（YAML 注释里的"待核实"结构化成字段）
skills:                            # 只保留技能要求与摘要，不存整份原文
  - skill: 会计准则
    importance: 5
    requirement_type: required     # required | preferred | bonus
    context: qualification         # responsibility | qualification | bonus_section
    note: "多家岗位合并整理，非单份 JD 逐字引用"
```

新增 `agents/external_sources.py` 做**读取方**（这是现在完全没有的那块），把条目转成 `job_evidence` 形状喂给既有 `_attach_evidence`，于是 `source_type=official_data` 自动获得与 user_jd 同等的回查能力，无需改匹配算法。

**不做**：不自动抓取、不批量入库、不存完整原文、不共享用户私有 JD（任务书要求）。

---

## 3. 工作流改造方案（对应阶段三）

### 3.1 边界重划：retrieval 加诊断，job 仍算 basis

任务书的"retrieval 查知识库"与"job 融合"，落到本代码库的正确切法是：

- **retrieval（扩展，不改职责性质）**：`chunk_user_jd` 产出带 `source_id`/`jd_index` 的证据；新增产出 `retrieval_diagnostics.coverage`（纯确定性统计：命中知识库岗位画像否、切出几句、几条、词表命中率）；**保持返回空不抛错**（现有 `_empty_update` 行为，`retrieval_agent.py:43-53`）。
- **job（保持融合点）**：`resolve_requirements_basis` **留在 job 节点**。理由：它同时被 `goal.py`、`analyze.requirement_basis()`、前端标灰三处消费，搬到 retrieval 会造成"产生点与消费点分离"，是三处链路里最容易静默失效的一处。retrieval 只提供事实（有没有原文、命中几条），job 决定口径（三档 basis）。
- **知识库岗位匹配**：保持在 job 节点的 `catalog.resolve_profile`，不搬。retrieval 通过 `retrieval_diagnostics` 报告"这个岗位有没有画像"这一事实即可。
- **外部资料**：由 retrieval 读取 `knowledge/sources/` 产出 evidence（它是"取数"节点，性质一致），仍由 job 融合评级。

这样七节点、五处模型调用、basis 产生点、画像缓存 key **全部不动**。

### 3.2 陌生岗位路径（现状已能跑通，只补一个专门信号）

实测路径（以"Java 后端工程师"、无 JD 为例）：`resolve_profile` 返回 None → `format_catalog_skills_block(None)` 注入"本次未匹配到岗位知识库的规范技能表" → 三个改写函数因 `profile is None` 全部 no-op → `resolve_requirements_basis` 返回 `("model_only", False)` → assessment/learning/project 照常执行 → **能出完整结果**，`jd_match.overall="insufficient"` + `match_rate=None`。

缺的不是执行能力，是**一个说人话的专门信号**：现在只有三条通用旁证（basis=model_only、每条 `model_supplement`、分母为 0），job_node 对目录外岗位没有任何专门 warning 字段。

方案：`job_profile_origin` 已有 `unavailable` 这个官方"没资料"信号（`job_agent.py:743`），且**已经送达浏览器**。阶段三不新增枚举，只在 `retrieval_diagnostics.coverage` 里补一个确定性布尔 `catalog_profile_matched`，前端拼成人话。

### 3.3 明确不做

不新增 Agent、不加节点、不加 checkpointer、不加流式（进度是另一条独立议题，本任务书未要求）。条件边只在已有的 assessment 之后复用一次（"资料不足 → 提示但不跳过"）。

---

## 4. 前后端接口改造方案（对应阶段四）

### 4.1 零新接口的四块展示

| 展示 | 数据来源 | 需要的改动 |
|---|---|---|
| 岗位画像卡（职责/工具/交付物/入行门槛/成长台阶） | `result.job_profile`（已在返回体） | 新组件 `JobProfileCard.vue`，每条按 `source: jd/standard/model` 上徽标；`enums.js` 加 6 行 `profileSourceText/Tone` |
| 「本次分析用了哪些资料」 | `retrieval_status` + `job_profile_origin` + `job_requirements_basis` + `retrieval_diagnostics` + `job_profile_warnings` | 新组件 `EvidenceProvenanceCard.vue`，用 `.panel-src` + `.notice-*`；**没有真实数字就不显示数量**（沿用 `JdMatchCard.vue:73-83` 的"给不出就不摆一排 0"范式） |
| 技能依据展开 | `JobRequirementTable` 已有全部字段 | 把平铺的 `source_notes` 包进现成 `details.fold`（`EvidencePanel.vue:114-116` 已示范） |
| 覆盖情况提示（资料不足要明说） | `job_profile_origin=unavailable` + `catalog_profile_matched` | 一条 `.notice-warn`；**禁止百分比**（现有页面已有"不给百分比"的既定口径） |
| 本次使用的资料（按轮次回看） | `latest_round.snapshot`（`GET /api/tasks` 已返回） | `TasksView.vue` 读 `.snapshot` 渲染折叠块，零后端改动 |

### 4.2 需要的后端改动（只有两处，都不是新接口）

1. `retrieval_diagnostics` 加 `coverage` 子块（`retrieval_agent.py` 内确定性计算，不调模型）。
2. 采纳 §2.5 才需要：`knowledge/sources/` loader + `/api/health` 的 `knowledge` 段落里加一个 `sources` 计数（与现有 `skills`/`projects` 同构，前端 `StatusView.vue` 已有渲染路径）。

阶段一/三/四的其它部分不需要动 `backend/schemas.py`、`services.py`、`analyze.py` 的对外契约。`_PUBLIC_STATE_FIELDS` 白名单**已经包含**所有要用的字段，不用扩。

---

## 5. 数据获取与知识库建设方案（对应任务书第五节）

### 5.1 先定规范再收数据（规范即 §2.5 的 `knowledge/sources/*.yaml`）

三条硬约束，来自现有校验器：
- `validate_knowledge.py` **没有字段白名单**，加字段不会失败；真正会挡的只有三条：技能名必须出现在岗位画像里、项目 `domain` 必须 ∈ `VALID_DOMAINS`、每领域 ≥3 项目且 ≥2 难度档。
- **`schema_version` 与 knowledge YAML 无关**（它只管 `runs/` 里 loop/growth/job_profile 三个运行时 store），所以扩知识库**不会**触发任务书禁止的"Schema 版本升级"。
- 外部资料**绝不能**放进 `knowledge/{jobs,skills,projects}` 三个被 glob 的目录。

### 5.2 匿名化必须现在定死

`knowledge/jobs/ai-application-engineer.yaml:8-10` 现存真实公司名（恒生电子、北京众智诚坚、易思维(杭州)、编号 J10355），是唯一带可反查真名的样例；会计/运营/教师三份已用 `source_*_jd_001` 匿名合并写法。**新数据一律向匿名写法靠齐**，公共库只留摘要 + 技能要求 + 允许公开的来源说明，不默认保存完整原文。

### 5.3 第一批的诚实成本账

任务书设想 6 个岗位 × 10–20 份 JD。其中 Java 后端 / Web 前端 / 数据分析师 / 算法工程师 **4 个岗位现状完全空白**，而按现有校验规则，一个新岗位画像会连带要求：技能词条（`level_guides` 四档齐全）+ 每领域 ≥3 项目模板。也就是**补一个岗位 = 动三个目录**，这是上一轮我判断"手写补库算不过来"的实测依据。

建议裁剪：**先做 2 个岗位（Python 后端、Web 前端）的画像 + 每岗位 3 份摘要型外部资料**，把入库流程跑通、校验通过、评测能对比，再决定要不要继续。数据收集本身是人工活，不是代码活，我可以写采集模板与校验脚本，但不能批量抓取（任务书禁止未授权爬取）。

### 5.4 学习资源索引

`knowledge/skills/*.yaml` 的 `learning_resources` 字段**已存在但 7 个文件全部是 `[]`**。这是一个已经预留好、填进去即被 `format_for_prompt` 带入提示词的现成位置，比新造任何索引都便宜。只放官方文档/公开课程/GitHub 项目链接，不复制教程内容。

---

## 6. 测试与评测方案（对应阶段五）

### 6.1 必动的既有测试（改动面已被现有断言锁死）

| 文件 | 用例 | 为什么会被动 |
|---|---|---|
| `tests/test_stage3_validation.py:610-617` | 断言 requirement 恰好 9 个键 | §2.3 加 `importance_by_source` 要同步更新 |
| `tests/test_golden_cases.py:175 test_requirements_basis_matches_jd` | 把 basis 锁成二值 | 引入第 4 种来源路径时必改 |
| `tests/test_job_profile.py:86/147/202` | 编号回查、来源标签块、schema 不符 | §2.5 改 evidence 来源后文案会变 |
| `tests/test_jd_match.py:156-162/257/262/339-341` | 推断项不进分母、无原文时承认全模型推测 | 诚实性回归的守门用例，改后必须仍然通过 |
| `tests/test_job_agent_evidence.py:36-39` | 把 YAML evidence 当 fixture | §2.5 让它成为生产路径后要复核 |
| `web/src/.../JobRequirementTable`（**当前无 spec**） | — | 加折叠时补一个 spec，按 `SkillGapTable.spec.js` 写法 |

任务书 10 个场景与现有能力的对应：已有资料无 JD / 已有资料有 JD / 无资料有 JD / 无资料无 JD 四格**现状已可测**（`golden/cases.yaml` 的 24 条覆盖 6 条含 JD、18 条不含，长尾 3 条 `longtail-nurse/electric/law` 就是"无资料无 JD"）；未知技能保留 = `test_nonai_skill_booking.py:65`；相关技能不合并 = `test_assessment_evidence.py:109`；JD 内容不足 = `retrieval_diagnostics.reason=user_jd_empty`。**真新增只有三格**：多份 JD 重要度、重复 JD 判重、模型输出格式异常不污染数据。

### 6.2 四模式对比的成本（必须先算再决定）

现状 `scripts/eval_golden.py:92` 传 `enable_job_profile=False`，**评测绕开了 `knowledge_base` 这条依据路径**，所以"仅知识库"这一模式现在根本测不到。要四模式对比，必须先让它可选。

算力账：24 用例 × 4 模式 = 96 次完整分析 ≈ 480 次模型调用，单 worker 串行。本地 `qwen2.5:3b` 按此前实测的岗位分析耗时，这个量级是**小时级**；云端免费档间歇 429。

建议：**抽样 8 条代表用例（每领域 1–2 条 + 1 条长尾）× 4 模式 = 32 次**，并把结果用 `--json-out` 落盘到**仓库内一个可提交目录**（`runs/` 被 gitignore，报告存 `runs/eval/` 不会入库；要给别人看需要新增 `eval-reports/` 或显式指定路径）。这是"评测落盘"那条借鉴的正当落点。

### 6.3 新增不变量

- 来源标注不互换：`source_type=official_data` 的条目不得被计入"用户粘贴原文"的任何统计。
- 推断不得冒充：`model_supplement` 条目不得出现在 `jd_match.requirement_total` 内（锁死现有行为）。
- 资料不足必须诚实：`job_profile_origin=unavailable` 时前端不得显示任何匹配率或覆盖率。

---

## 7. 文件清单

### 7.1 需要新增（6 个）

| 文件 | 职责 |
|---|---|
| `agents/source_attribution.py` | 派生视图纯函数：`requirement_source` / `requirement_citation`，§2.2 真值表的唯一实现 |
| `agents/external_sources.py` | `knowledge/sources/` 只读 loader，把外部资料转成 `job_evidence` 形状（§2.5 的缺失读取方） |
| `knowledge/sources/*.yaml` | 第一批外部资料（2 岗位 × 3 份摘要），匿名 `source_id` |
| `web/src/components/JobProfileCard.vue` | 岗位画像卡（数据已到浏览器） |
| `web/src/components/EvidenceProvenanceCard.vue` | 「本次用了哪些资料」聚合视图 |
| `tests/test_source_attribution.py` | §2.2 真值表 + §6.3 三条不变量（纯函数，不打模型） |

### 7.2 需要修改（10 个，都是加法）

| 文件 | 改什么 |
|---|---|
| `agents/user_jd.py` | evidence 加 `source_id` / `jd_index`；`provenance` 保持不变 |
| `agents/retrieval_agent.py` | 产出 `retrieval_diagnostics.coverage`；接入 external sources |
| `agents/job_agent.py` | 同名技能重要程度"分别保留 + 取最大"；gap 侧透传来源 |
| `agents/assessment_agent.py` | gap 条目加 `requirement_source`（同 `evidence_kind` 位置） |
| `web/src/utils/enums.js` | 加 `profileSourceText/Tone`（6 行）与 coverage 相关文案 |
| `web/src/components/JobRequirementTable.vue` | 原话平铺改 `details.fold` 折叠 |
| `web/src/views/AnalyzeView.vue` | 挂两个新组件 + 覆盖提示 notice |
| `web/src/views/TasksView.vue` | 读 `latest_round.snapshot` 展示"本次使用的资料" |
| `scripts/eval_golden.py` | `enable_job_profile` 可配、模式矩阵、默认落盘 |
| `README.md` | 纠正「岗位依据只有两个来源」→ 实际三档（与 `goal.py:16-18` 对齐） |

**不动的文件（重要）**：`graph/state.py`（`check_update` 白名单不扩）、`backend/schemas.py`、`backend/services.py` 的快照与落盘逻辑、`agents/growth_store.py`、`agents/goal.py` 的三档判定、`agents/job_profile.py` 的 `cache_key`/`jd_fingerprint`/`SCHEMA_VERSION`、三个 `knowledge/*` loader 的 glob 行为。

---

## 8. 潜在风险与兼容性

| # | 风险 | 证据 | 防护 |
|---|---|---|---|
| R1 | **`provenance` 与 `E01` 编号契约是全站最紧耦合**：`_attach_evidence`、`jd_match` 分母、`job_profile._valid_refs` 三处同时依赖它；retrieval 改产出结构会让 supported/partial/model_supplement 与匹配度分母连锁错位 | `job_agent.py:563-572`、`jd_match.py:102-115`、`job_profile.py` 编号校验 | 只**追加** evidence 键、**不改** `provenance` 值域；编号仍按全量 evidence 顺序生成；先跑 `test_jd_match.py` + `test_job_profile.py` 全绿再进下一步 |
| R2 | **改 `jd_text` 语义会让全部历史画像缓存整批失效**（缓存 key = sha256(岗位名 + jd_fingerprint(jd_text))） | `job_profile.py:580-617` | 阶段一/三**不碰 `jd_text` 字段语义**；若做多份 JD，用新增 `jd_texts` 列表字段走"新键优先、旧键回退"，`jd_text` 保持单份兼容读法，旧缓存继续命中 |
| R3 | 把 `resolve_requirements_basis` 搬到 retrieval 会造成 basis 产生点与 `goal.py` / `analyze.requirement_basis` / 前端标灰三处消费点分离，**静默失效**（不报错，只是结论变松） | `job_agent.py:526-543`、`goal.py:57-92`、`analyze.py:105-122` | 明确不搬（§3.1）；加一条回归：`model_only` 时 `goal.status` 不得为 `achieved` |
| R4 | 引入第 4 种来源与 `test_golden_cases.py:175` 的二值死锁冲突，可能误改导致**评测失去回归意义** | 同上表 | 改测试前先记录当前 baseline 通过率；改动只在"新增 official_data 用例"分支生效，原 24 条断言不动 |
| R5 | 新目录/新字段撞 `validate_knowledge` 三条硬规则（技能必须在画像里、domain ∈ VALID_DOMAINS、每领域 ≥3 项目） | `validate_knowledge.py:126-128/165-182` | 外部资料放 `knowledge/sources/`（无 loader glob）；每加一个新岗位画像同步补技能与 ≥3 项目模板，先跑校验脚本再提交 |
| R6 | **真实用户数据风险**：`runs/users/user_3/job_profiles/` 有 6.8KB 真缓存、4 个 `loop.json.cleared-*` 备份 | 实测目录列表 | 所有验证走 `tests/` 合成 fixture 与独立临时目录；沿用 `analyze.py:82-102` 的 `_isolated_growth_store()` 隔离范式；禁止用 `user_2/user_3` 做写入验证 |
| R7 | 工作区已有 42 项未提交改动，`golden/` 整个未入库；误 `git add -A` 会把 `.env` 类风险放大 | `git status --short` = 42 | 本方案期间**不执行任何 git 写操作**；每阶段结束给"建议提交的文件名清单"，由用户手动 stage 单文件 |
| R8 | 前端加画像卡可能打破"不编造读数"的既有产品口径（现有组件明确拒绝伪造百分比与伪造进度） | `RunProgressCard.vue:11-13`、`JdMatchCard.vue:73-83`、`enums.js:3-4` | 画像卡每个条目必须带来源徽标；无 `evidence_refs` 即显示"模型推断"；覆盖率一类数字一律不显示 |
| R9 | `graph/state.py:36` 注释写 `cache / generated / unavailable`，漏了代码实际会写的 `knowledge_base`（文档漂移，评审会当成 bug） | `state.py:36` vs `job_agent.py:757` | 列入阶段四的文档修正项（一行注释） |
| R10 | README「岗位依据只有两个来源」与代码三档不一致，会被外部评审判为"设计与实现不符" | `README.md:257-262` vs `goal.py:16-18` | 阶段四改 README 该段 |

**兼容性总结**：设计全程只触碰**条目级加键**与**新增只读模块**，不触碰 `state` 键集合、不触碰三处 `SCHEMA_VERSION=1`、不触碰 `runs/` 存储形状，因此**零迁移、零版本升级争议、旧 run 结果仍可正常渲染**。

---

## 9. 分阶段实施顺序

每阶段末尾都有**停手确认点**（除了阶段 0），验收不通过不进下一阶段。

**阶段 0 · 基线留档（不改代码，1 次会话内完成）**
- 跑 `python -m pytest tests backend/tests -q`、`python scripts/validate_knowledge.py`、`cd web && npm run test && npm run build`，记录当前通过数。
- 跑一次 `python scripts/eval_golden.py --dry-run` 与一次小样本 `--limit 4 --json-out <临时路径>`，作为回归基线。
- 产出：基线数字 + 本报告 §1 的事实复核。
- **停手点：把基线交给你确认。**

**阶段 1 · 来源派生视图（纯函数，最安全，先落地）**
- `agents/source_attribution.py` + `tests/test_source_attribution.py`（§2.2 真值表、§6.3 不变量）。
- 只读、不接入工作流即可交付，失败可整体回退。
- 验收：pytest 全绿 + 新用例覆盖 5 种派生分支 + 现有 `test_jd_match.py`/`test_goal.py` 零改动通过。
- **停手点：给你看派生视图的字段样例（一条真实 requirement 转出来的 source_type / evidence 性质）。**

**阶段 2 · 前端「看得见」（零后端改动，价值最直观）**
- `JobProfileCard.vue` + `EvidenceProvenanceCard.vue` + `enums.js` 6 行 + `JobRequirementTable` 折叠 + `TasksView` 读 snapshot + 覆盖提示 notice。
- 验收：Vitest 新增 2 个 spec；浏览器实测四种情形（有画像有 JD / 有画像无 JD / 无画像有 JD / 无画像无 JD），截图对照；确认无画像时**没有任何数字被伪造**。
- **停手点：让你看页面，你说"这有什么用"就当场砍掉不喜欢的块。**

**阶段 3 · 来源隔离补齐 + 陌生岗位专门提示**
- retrieval 产 `coverage`；assessment gap 透传 `requirement_source`；learning/project 排序降权但不删除。
- 不碰 `jd_text`、不碰 basis 产生点。
- 验收：`test_job_agent_evidence.py`/`test_nonai_skill_booking.py`/`test_agent_knowledge_injection.py` 全绿；评测里"无资料无 JD"长尾 3 条结果与阶段 0 基线一致（除新增标注）。
- **停手点：给你新旧两次同岗位分析结果对比。**

**阶段 4 · 外部资料入库（要真人收数据，最可能被砍）**
- `knowledge/sources/` + `agents/external_sources.py` + 2 岗位 × 3 份匿名摘要 + 采集模板文档。
- 先跑 `validate_knowledge.py` 再谈接入；接入后 `source_type=official_data` 走既有回查。
- 验收：Python 后端 / Web 前端两个岗位在**不粘 JD**时 `requirement_basis` 从 `model_only` 升为"有外部资料依据"，且前端显示来源与 `review_status`。
- **停手点：数据内容需要你或你同学过目，我不替你判定"这条招聘要求真实吗"。**

**阶段 5 · 多份 JD（可选，风险最高，可整体放弃）**
- `jd_texts` 新字段 + evidence `source_id` 分来源 + importance 分别保留与聚合。
- 前置：阶段 1–3 全绿且用户确认这条值得做（D2）。
- 验收：`jd_profile` 缓存对旧单份 `jd_text` 仍 100% 命中（R2 回归）。

**阶段 6 · 评测矩阵**
- `eval_golden.py` 模式开关 + 抽样 8 条 × 4 模式 + 报告落盘到可提交目录 + 放开 `test_golden_cases.py:175` 的死锁。
- 验收：产出四模式对比表（技能提取准确性 / 来源标记准确性 / 归一准确性 / 规划合理性 / 不足时诚实度 / 原功能回归），并明确写出哪些差异是本地 3b 模型能力造成的、不是架构造成的。

---

## 10. 需要你拍板的 6 个决策

| # | 决策 | 我的建议 | 代价 |
|---|---|---|---|
| D1 | retrieval 是否按任务书字面接管"知识库查询" | **不接管**，只做证据供给 + 覆盖度诊断（§3.1） | 照字面做要多改 3 处消费点，风险 R3 |
| D2 | 多份 JD 要不要做 | **本大轮先不做**（阶段 5 可选） | 全站耦合最紧的结构 + 缓存失效风险 R2 |
| D3 | `official_data` 要不要引入 | **要**，但第一批压到 2 岗位 × 3 份 | 数据靠人工收集，不是代码问题 |
| D4 | 评测四模式跑全量还是抽样 | **抽样 8 条**，全量留到云端有额度时 | 全量 ≈ 小时级串行，且免费档 429 会打断 |
| D5 | 岗位画像卡做不做 | **做**，数据已经到浏览器，这是最便宜的可见创新度 | 一个组件 + 一个 spec |
| D6 | 阶段顺序 1→2 还是 2→1 | **1→2**（先纯函数零风险，再一次性把展示做扎实） | 先做 2 会返工文案 |

---

## 附：本轮核实方式

只读核查，未修改任何文件、未安装任何依赖、未执行任何 git 写操作。四个方向独立取证：证据与来源数据结构、工作流与画像缓存链路、前端展示与接口契约、知识库数据与评测体系；所有路径与行号来自当前工作树实测（HEAD `54ce7a2`，42 项未提交）。

对我上一份简报的两处修正：
1. "项目模板骨架没肉" —— **错**。15 行是纯字段，内容齐全（covers/prerequisites/tech_stack/stage_hint/deliverable/4 条验收/预计周数），四领域难度梯度也齐；`validate_knowledge.py` 强制 9 字段 + 每领域 ≥3 项目 + ≥2 难度档。
2. "岗位画像内容前端一个字都没显示" —— **半对**。`job_profile` 的 summary/core_tasks/career_path 确实 0 引用；但岗位要求、原话、证据强度、等级来源、匹配度、结局卡**全都在显示**（`JobRequirementTable.vue`、`JdMatchCard.vue`、`GoalCard.vue`）。上一轮我漏看了 `JobRequirementTable.vue`。
