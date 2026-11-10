# MySQL 求职档案隔离 Implementation Plan

> **状态：用户已批准实施（2026-11-06），按本文执行。** 仍不 commit/push、不改 `.env`、不迁移或删除真实用户数据。
> **For agentic workers:** 使用 `superpowers:subagent-driven-development` 或 `superpowers:executing-plans` 按任务执行；所有步骤使用复选框跟踪。

**Goal:** 用 MySQL 保存账号、基础资料和互相独立的求职档案，让岗位分析、任务执行、成长记录不再串档，并支持前端确认删除整份档案。

**Architecture:** 固定六张业务表，以账号拥有档案、档案拥有分析轮次/任务/成长记录建立关系，不为每次分析动态建表。创建档案时冻结基础资料与招聘要求，后续能力只在本档案内演进；后台分析先取快照，再在事务外调用 AI，最后通过档案存在性和版本校验提交结果。前端显示明确的当前档案，删除使用数据库事务清除全部关联在线数据，并拒绝迟到结果回写。

**Tech Stack:** 保留 Python、FastAPI、LangGraph、Vue 3、Pinia、现有 `SQLAlchemy==2.1.5`；新增 MySQL 8.4 LTS 与 `PyMySQL[rsa]==1.1.2`，不引入 Redis、消息中间件、新 Agent、向量库或新 UI 框架。

**Spec:** 本文「1. 已确认需求与产品契约」为本计划的规格依据，来自本次对话；新增工程细节随本文一起等待用户审阅。

## 审阅版任务顺序

以下均未实施；先完成隔离测试与迁移演练，再考虑切换真实环境，不是边开发边把用户旧数据搬进去。

| 任务 | 交付结果 | 依赖 |
| --- | --- | --- |
| 1. 数据库底座 | 六表、外键/索引、独立测试库，账号与基础资料接口 | 用户批准计划及本机数据库准备 |
| 2. 档案读写与删除 | 创建、列表、详情、鉴权、整档案事务删除 | 1 |
| 3. 分析与运行生命周期 | 新档案分析、结果持久化、快照、运行中删除、失败恢复 | 2 |
| 4. 任务与成长隔离 | 完成、补录、撤回、作废、清单、下一轮全部限当前档案 | 3 |
| 5. 前端上下文 | 档案 store、接口与缓存隔离、迟到请求防护 | 2–4 |
| 6. 前端档案管理 | 列表/选择/删除、历轮结果回看、各页空态 | 5 |
| 7. 旧数据迁移工具 | 只读盘点、校验备份、账号迁移、混合旧记录只读留存、合成数据演练 | 1–4 |
| 8. 回归与上线门槛 | 多账号多档案、删除竞态、性能及浏览器验收；明确切换/回退步骤 | 1–7 |

## Global Constraints

- 用户已于 2026-11-06 明确授权实施；执行范围仅限代码、测试与文档，不含决定真实数据迁移或生产上线。
- 不修改或覆盖现有未提交改动，不清理或重置 Git 工作区，不擅自 commit/push。
- 不修改 `.env`，不读取或输出 `CLOUD_API_KEY`、`JWT_SECRET`、数据库密码；后续凭据由用户本地填写。
- 不读取真实用户内容进行设计，不修改 `runs/users/user_2`、`user_3`，不删除或覆盖成长历史原始记录。
- 新版档案删除是用户明确请求的在线业务功能；它不授权开发者现在删除任何旧文件或生产数据库记录。
- 所有自动化验证只用合成账号、合成招聘要求、隔离的 MySQL 测试库；模型调用替身化，不消耗真实模型额度。
- 六表重构替代 Web 的账号级 JSON 持久化，不再把“保留旧 JSON 结构、不增加档案标识”当作本次实现限制；旧原件保留，不做 JSON schema 原地升级。
- 保留现有职业覆盖、证据来源规则、确定性成长判定、目标结局、云端/本机切换和当前页面视觉风格。
- 每阶段结束立即追加开发留痕：用户原话、关键产出、纠偏/未做项、失败与返工；不能收尾时一次性补记。

## 1. 已确认需求与产品契约

### 1.1 用户要看到的效果

1. **新分析是一份新档案**：填写目标岗位与招聘要求，点“开始分析”后创建独立档案；相同岗位名称也可以有不同公司的两份档案，不按岗位名称合并。
2. **下一轮不是新档案**：在档案 A 完成任务、登记成长、生成下一轮，全部仍归 A，轮次只在 A 内递增。
3. **前后互不影响**：A 的任务、成果、能力升级、依据与结局不自动带入 B；B 的分析不覆盖 A 的结果。
4. **基础资料共享，分析资料冻结**：账号的专业、年级、自述技能、项目经历存在用户资料表；目标岗位和招聘要求属于档案。创建时复制基础资料，之后修改基础资料不改写旧档案，也不能向旧档案回灌最新自述能力。
5. **结果能回看**：保存每轮完整分析结果，包括岗位画像、依据与来源、能力表、技能差距、学习计划、项目推荐、成长变化及目标结局，不只保存任务标题。
6. **可以选择与删除档案**：“我的档案”展示档案列表，任务/成长/分析明确显示当前档案；用户确认后删除选中档案及全部关联在线产物，保留账号、基础资料、其他档案。
7. **删除不是隐藏**：删除后不再出现在列表、查询、下一轮输入、能力计算或后台结果中；刷新与重新登录不能把它恢复出来。
8. **删除正在运行的档案也有定义**：允许删除；排队任务不再执行，已经发出的模型请求可能无法立即停止，但其结果必须丢弃，不得写回数据库、运行缓存或页面。
9. **单条更正仍保留**：撤回未入账成果、作废已入账成果、手动补录均保留，但只作用于当前档案；不把本次整档案删除扩成任意编辑历史或降低证据门槛。
10. **AI 助手仍是通用聊天**：当前聊天不自动引用档案，不新增聊天表或档案聊天功能；本地会话按账号隔离，不能把另一账号的聊天当作本人的历史。

### 1.2 删除确认与空态

- 确认框显示档案名称、目标岗位、将删除的分析/任务/成长数量；明确“永久删除这份档案，无法撤销”，取消不发删除请求。
- 删除非当前档案：当前页面与选择不变。
- 删除当前档案：切到剩余最近更新的一份；没有剩余档案时进入“还没有求职档案，开始一次岗位分析”空态，基础资料仍在。
- 删除失败：保留当前内容，允许重试；不能先假装成功再丢失页面上下文。
- 前端删除确认绑定打开时的档案 ID，不得因随后切换档案而删错对象。
- 离线迁移备份不属于在线业务数据；删除在线档案不会擅自销毁旧原件或备份，也不允许程序以后自动从备份把它导回。

### 1.3 不在本轮范围

不做账号注销、多人共享档案、档案间能力自动迁移、回收站、批量删除、外部招聘抓取、上传附件、模型改造、多进程/分布式队列或全站视觉重做；单条成长记录仍走已有撤回/作废机制，新增的是整份档案的永久删除。

## 2. 当前代码证据与改造边界

| 现状 | 已核实位置 | 本轮必须改变的边界 |
| --- | --- | --- |
| 新计划只追加全账号轮次 | `backend/services.py:435–564`；`agents/loop_store.py:525–559` | 新档案拥有自己的轮次，不能拿账号的最大轮次继续追加 |
| 待入账汇总扫描所有已完成任务 | `agents/growth_loop.py:328–353` | 只收集当前档案的未入账成果，去重也限当前档案 |
| 下一轮使用账号最新轮次的目标 | `backend/services.py:731–869` | 从请求中明确指定且鉴权的档案取快照，不允许“猜最新” |
| 基础资料和最近能力是两份全局浏览器缓存 | `web/src/utils/storage.js:14–22`；`web/src/stores/profile.js:28–76` | 基础资料入库；能力与结果按档案从服务端读取 |
| 下一轮仍能携带当前可编辑画像 | `web/src/api/endpoints.js:192–210` | 请求不接收替换旧档案资料/JD的字段 |
| 任务清空和整档案删除不是一件事 | `web/src/api/endpoints.js:124–137` | 保留档案内清任务行为，另建整档案删除接口 |
| 路由没有档案上下文 | `web/src/router/index.js:25–66` | 保留页面路径，增加显式档案 query 与全局选择器 |
| 账号已使用 SQLAlchemy，版本锁定 2.1.5 | `requirements.txt:17–19` | 复用现有 ORM，不新增第二套账号系统、不降级 SQLAlchemy |

当前工作树已有大量历史未提交改动；后续实施必须以此工作树为基线，不能用旧提交覆盖当前内容。

## 3. 性能与环境决定

- 使用本机 MySQL 8.4 LTS，不为了本次改造引入 Docker；实际安装或启停数据库等环境动作放在获准实施以后。
- 使用同步 SQLAlchemy 与 PyMySQL，适配现有同步 FastAPI 服务和工作线程；每个请求/工作线程创建自己的 Session，禁止把请求 Session 传给后台任务。
- `pool_pre_ping=True`，`pool_recycle=1800`；初始 `pool_size=5`、`max_overflow=0`、`pool_timeout=5`，仍保留现有单模型工作线程，不承诺因换库就提升模型速度。
- 档案、任务、历史列表分页，默认 20、最大 100；列表不读取完整 `result_json`、JD 原文或证据大字段，详情才读取。
- AI 推理期间不持有事务或行锁；数据库只参与输入快照读取、状态变化和结果保存，避免其他页面被模型等待拖住。
- 档案仍共享模型与工作队列：A 正在分析时 B 可能排队，但 A 的数据不会进入 B；“互不干涉”指业务数据隔离，不承诺各档案同时并行推理。
- 为账号归属、档案归属、轮次顺序和任务状态建立索引；先用合成数据及 `EXPLAIN` 实测，不先上缓存中间件。
- 数据库不可用时明确返回服务暂不可用，不偷偷退回 JSON/SQLite 写入，否则会形成两套不一致数据。
- MySQL DDL 可能隐式提交：初始化建表失败不能宣传成“事务会全部回滚”，必须在新空库中执行并检查六表完整性。

### 文档依据

Context7 在本会话不可用，已查官方文档；实施时继续使用项目锁定版本而不是照抄其他版本 API。

- SQLAlchemy 2.1 Session 生命周期与线程约束：https://docs.sqlalchemy.org/en/21/orm/session_basics.html
- SQLAlchemy MySQL 方言、连接与字符集：https://docs.sqlalchemy.org/en/20/dialects/mysql.html
- PyMySQL 的 MySQL 8 认证安装 extra：https://pymysql.readthedocs.io/en/latest/user/installation.html
- PyMySQL 1.1.2 发行元数据：https://pypi.org/pypi/PyMySQL/1.1.2/json

## 4. 前端范围与交互约定

| 页面/模块 | 改什么 | 不改什么 |
| --- | --- | --- |
| `HomeView.vue` 我的档案 | 上方账号基础资料；下方求职档案列表、切换、查看、删除；当前档案能力单独标清 | 不把账号资料误称为每次新建的用户，不重做视觉风格 |
| `App.vue` 与新 `PlanSelector.vue` | 登录后的分析/任务/成长区域提供紧凑的当前档案选择器 | 登录、注册、通用聊天不强迫先选档案 |
| `AnalyzeView.vue` | 新分析读取基础资料作为草稿，提交即新档案；已选档案只读回看快照与历轮结果 | 不再用全账号“是否已有轮次”决定新分析只是预览 |
| `TasksView.vue` | 筛选轮次、完成、清单清空都限定当前档案 | 不删其他档案任务，不把完整清单清空当作档案删除 |
| `GrowthView.vue` | 待入账、已入账、撤回、作废、补录、下一轮都限定当前档案 | 不读取当前可编辑基础资料替换旧快照 |
| `OverviewView.vue` | 摘要与入口跟随当前档案；没有档案时引导首次分析 | 不展示账号全局最近能力冒充当前档案能力 |
| `StatusView.vue` / health store | 数据部分改成数据库连接与当前档案读数，技术信息仍折叠 | 模型通道、技能标准和岗位资料仍是应用级状态 |
| auth / 通用聊天 | 退出登录失效旧请求；会话缓存按账号命名 | 不新增聊天历史数据库或自动档案上下文 |

### 4.1 页面与缓存

- 保留 `/home`、`/analyze`、`/tasks`、`/growth`、`/overview`、`/status` 的路径，用 `?plan=<档案ID>` 标识可分享的当前档案上下文；新建分析使用明确的新建态，不从 URL 猜最新档案。
- URL 指定不存在、已删除或其他账号的档案：展示“找不到这份档案”，不悄悄显示另一份的任务；未指定时才恢复当前账号上次选择或展示档案列表。
- 新增 `web/src/stores/plans.js`，维护档案列表、当前档案、切换代次、运行句柄；结果以服务端为准，先清空旧结果再加载新档案。
- 本地只保存账号命名空间下的当前选择与未提交草稿；不再持久化一份全局 `last-skills` 当作业务权威数据。
- 旧 `career-agent:profile` 没有账号归属：不能静默绑定当前账号；可在“导入本机旧资料”明确预览并确认后保存到基础资料接口，取消不动旧键。
- 旧 `career-agent:last-skills`、分析结果不得用于新档案初始能力，也不得当成基础资料自动导入。
- 通用聊天使用账号命名空间；未知归属的旧聊天键不自动展示给任意账号，不在这次迁移中擅自删除原键。

### 4.2 迟到响应保护

每次异步请求捕获 `accountId + planId + epoch`；所有成功、失败、轮询和 `finally` 更新都要验证上下文仍相同。切换、删除、退出登录递增 epoch 并中止旧请求；AbortController 只能中止浏览器等待，不能代替后端删后回写防护。

```js
const captured = { accountId: auth.user.id, planId: plans.activeId, epoch: plans.epoch }
const isCurrent = () => auth.user?.id === captured.accountId
  && plans.activeId === captured.planId && plans.epoch === captured.epoch
loading.value = true
try {
  const value = await listTasks(captured.planId, {}, { signal: controller.signal })
  if (isCurrent()) data.value = value
} catch (error) {
  if (isCurrent() && error.name !== 'AbortError') errorMessage.value = error.message
} finally {
  if (isCurrent()) loading.value = false
}
```

`listTasks(planId, params, options)` 是本计划规定的新接口封装；`auth.user` 沿用现有用户对象，新增的 `plans.activeId/epoch` 由档案 store 提供。A→B→A 也必须增加 epoch，不能只比较档案 ID。`client.js` 处理 401 时还要比较请求发出时的 token 与当前 token，不能让旧请求登出后来登录的账号。

## 5. 核心验收场景

| 场景 | 必须观察到的结果 |
| --- | --- |
| 用户甲创建 A“后端”、B“会计” | 两份独立档案，各从自己的第 1 轮开始，任务和能力不混用 |
| 同一账号两份同名岗位、不同 JD | 不按岗位名去重，目标资料和结果仍独立 |
| A 完成一个任务但未入账，随后 B 生成下一轮 | B 的待入账成果里没有 A 的任务，A 状态和能力不变 |
| A 有技能升级后创建 B | B 只从本次基础资料快照开始，不继承 A 的成长能力 |
| 修改账号基础资料、再打开 A | A 的旧快照、旧结果和下一轮输入不变；新建 C 使用新资料 |
| A 作废一条成长 | A 内重算；B 的能力、记录数量、结果版本都不变 |
| A 清空任务清单 | A 已入账成长不删；B 完全不变；A 任务不在下一轮偷偷重新入账 |
| 删除 A（有结果、任务、成长、岗位画像） | A 的全部在线关联行归零，查询 A 及其 run 返回 404；B、账号、基础资料保持原值 |
| A 的模型调用未返回时删除 A，再放行模型替身 | 不恢复 A，不新增子行，不向运行缓存写成功结果；B 正常使用 |
| 同档案重复点击/重试下一轮 | 不重复创建同一操作，不重复记成长、升级或创建任务 |
| 结果保存途中模拟数据库失败 | 成长、任务、当前能力要么一起提交、要么都不提交；已检查点的生成结果可只重试保存 |
| 用户乙猜到 A 的档案/任务/成长/run ID | 每一个读写/删除入口都拒绝，返回与不存在相同的 404 |
| A→B→A 切换和旧请求 401/失败/finally 迟到 | 不覆盖当前档案，不登出新账号，不错误结束新页面 loading |
| 删除当前唯一档案、刷新并重新登录 | 显示无档案空态，基础资料保留，不从旧 localStorage 恢复它 |
| 迁移含无法归属岗位的旧历史 | 作为只读旧记录保留，不猜测拆分、不作为新档案能力基线 |
| 迁移重复执行、或删掉迁入的在线旧记录后再次执行 | 识别已处理来源，不重复导入、不复活已删除的旧记录 |
| 数据库停用/密码错误/队列已满 | 返回清楚的失败状态，不输出连接密码，不转写 JSON，不留下永远排队的假进度 |

## 6. 六表结构与持久化边界

以下为拟新增结构，不代表当前已有这些字段。所有表使用 InnoDB；文本 utf8mb4；UUID 用 `CHAR(36)`；JSON 只承载结构化分析内容，不承担外键关联；时间统一 UTC，接口输出带时区的 ISO 时间。

| 表 | 主要字段 | 外键/唯一性/索引 |
| --- | --- | --- |
| `users` | 保留 `id INT`、`username VARCHAR(32)`、`password_hash VARCHAR(255)`、`created_at` | 保留原账号 ID、哈希和公开响应；用户名明确使用大小写敏感排序规则，不重置密码 |
| `user_profiles` | `user_id`、`major`、`grade`、`skills_json`、`projects_json`、`version`、`updated_at`、`legacy_receipt_json` | `user_id` 同时为主键及账号外键；迁移回执只记来源摘要与已处理状态，不存凭据 |
| `career_plans` | `id`、`user_id`、`request_key`、`title`、`target_job`、`jd_text`、`profile_snapshot_json`、`initial_skills_json`、`current_skills_json`、`job_snapshot_json`、`goal_json`、`version`、`next_round_no`、`active_run_id`、`state`、`legacy_payload_json`、创建/更新时间 | `user_id → users.id`；唯一 `(user_id,request_key)`；索引 `(user_id,updated_at,id)`；`state=draft/ready/legacy_readonly` |
| `analysis_rounds` | `id`（也作为 run_id）、`plan_id`、`request_key`、`round_no`、`kind`、`status`、`attempt_token`、`input_version`、`input_json`、`result_json`、`error_code`、`error_message`、创建/开始/完成时间 | `plan_id → career_plans.id ON DELETE CASCADE`；唯一 `(plan_id,round_no)`、`(plan_id,request_key)`、`(id,plan_id)`；索引 `(status,created_at)` |
| `tasks` | `id`、`plan_id`、`round_id`（补录可空）、`task_no`、`kind`、`source`、`title`、`skills_json`、`details_json`、`status`、`evidence_json`、`version`、创建/完成时间 | 档案外键级联；复合外键 `(round_id,plan_id) → analysis_rounds(id,plan_id)` 防串轮；索引 `(plan_id,status,id)`、`(plan_id,round_id,id)` |
| `growth_records` | `id`、`plan_id`、`round_id`（调整可空）、`operation_key`、`kind=normal/adjustment`、`entry_json`、`void_of`、`created_at` | 档案外键级联；复合轮次外键；唯一 `(plan_id,operation_key)`、`(plan_id,void_of)`；索引 `(plan_id,created_at,id)` |

### 6.1 字段语义

- `profile_snapshot_json` 保存这次表单中通过校验的自述资料，不读取其他档案能力；创建时同步更新本人的基础资料，二者在一个短事务提交。用户未编辑的资料从基础资料表预填，不用旧 `last-skills` 反向补全。
- `initial_skills_json` 首次成功分析时冻结；未完成初次分析时为 NULL，合法的零技能基线为 `[]`，不能把二者混为一谈。
- 后续轮次不再重新运行 Profile Agent 来“取更高等级”，而是使用冻结首轮基线与本档案的有效成长结果，避免作废后的升级被新一次模型提取复活。
- `job_snapshot_json` 同时保存岗位要求、岗位画像、招聘证据、来源报告、依据强度；后续只复用本档案的快照，不加载用户目录内其他岗位画像缓存。
- `result_json` 保存 `project_result()` 公开分析字段及完整最终计划；列表接口只取摘要，详情才返回结果。每次运行包含冻结的模型通道选择，不保存云端密钥。
- `round_no` 是本档案的运行序号，`kind=initial/preview/next_round`；界面明确标注预览，预览不增加执行任务、不更新当前能力/目标或要求快照。
- 手动补录直接创建档案内任务，`round_id=NULL`，不虚构一个“最新分析轮次”、不覆盖当前目标。必须先完成该档案首次分析；没有档案时引导先分析，不能再创建无归属的成果。
- 一条正常 `growth_records` 对应一次批量成长判定，`entry_json` 包含当批全部事件、证据快照、before/after、升级、受阻、跳过与无效结果；不能按事件拆成多轮升级。
- 成长证据是独立快照，不外键级联依赖可清空的任务行；事件内保留 `source_task_id` 追溯值，写入前核验它属于同一档案。清任务可以移除任务行，但不能带走已入账成长证据。
- 作废引用稳定的 `growth_record_id + event_index`，`void_of` 为两者的固定编码；事件数组保存后不重排，禁止再用整张历史列表的临时下标作为记录身份。
- 不给所有历史指纹直接加唯一键：原规则允许受阻成果后来重试，已处理集合须沿用 `GrowthStore.seen_fingerprints` 的真实判定，只是范围缩至当前档案；原子提交由运行幂等键与档案锁保证。
- `legacy_payload_json` 只用于只读旧档案，普通档案必须为 NULL；旧记录不参与任何新分析、任务汇总或能力判定。

### 6.2 数据库连接与删除骨架

在现有 `backend/db.py` 保留 Base 和 User；新增 `backend/models.py` 放其他五表，避免同时维持两种 User 类型；建表命令显式执行，应用运行时只校验结构，不偷偷创建旧 SQLite 文件。

```python
engine = create_engine(
    database_url,
    pool_pre_ping=True,
    pool_recycle=1800,
    pool_size=5,
    max_overflow=0,
    pool_timeout=5,
)
factory = sessionmaker(bind=engine, expire_on_commit=False)
```

新增 `PlanRepository(factory)`，所有方法显式接收 `owner_id` 与 `plan_id`；HTTP 的 owner 只能来自登录态，不能相信请求里的 user_id。代表性的删除逻辑如下，实际返回 404 的映射放在 API 层：

```python
def delete_plan(self, owner_id: int, plan_id: str) -> None:
    with self.factory.begin() as session:
        plan = session.scalar(
            select(CareerPlan).where(
                CareerPlan.id == plan_id,
                CareerPlan.user_id == owner_id,
            ).with_for_update()
        )
        if plan is None:
            raise LookupError("plan_not_found")
        session.execute(delete(CareerPlan).where(CareerPlan.id == plan.id))
```

依靠已测试的数据库外键级联删除轮次、任务、成长；提交后清理该档案运行缓存。删除不生成包含原文的“备份档案”；迁移前的独立备份另按授权保留。删除失败或回滚不得先清 UI/运行缓存，删除成功后的迟到 worker 只能失败退出，禁止 `upsert` 重建父记录。

## 7. API 与计算生命周期

### 7.1 新接口契约

统一通过 `/api/plans/{plan_id}` 传档案身份。旧账号级 `/tasks`、`/growth/*`、`/analyze-and-save`、`/analyze`、`/runs/*` 同步替换，不留“缺 plan_id 就使用最新档案”的兼容分支；前后端一并发布。

| 方法与路径 | 请求/响应核心 |
| --- | --- |
| `GET /api/profile` | 返回本人基础资料与 `version`，没有资料返回空字段，不读他人或旧全局浏览器缓存 |
| `PUT /api/profile` | `{major,grade,skills,projects,version}`；版本冲突 409，不改任何已建档案 |
| `POST /api/plans` | `{request_key,title?,target_job,jd_text,user_profile}`；201 `{plan_id,state:"draft"}`，同账号同 request_key 返回原档案、不重复更新资料 |
| `GET /api/plans?limit=&before=` | 本人档案摘要、数量、运行状态、目标结局，不返回大结果；默认20、最多100 |
| `GET /api/plans/{id}` | 当前快照、当前能力、目标结局、运行句柄、删除数量摘要；旧档案返回 readonly 标记 |
| `DELETE /api/plans/{id}` | 成功204；不存在与越权均404；不接受“删除其他账号”参数 |
| `POST /api/plans/{id}/analyses` | `{request_key,kind:"initial"或"preview"}`；202 `{plan_id,run_id,status}`，初次分析成功后不允许再改首轮基线 |
| `GET /api/plans/{id}/rounds` | 分页轮次摘要；只返回该档案 |
| `GET /api/plans/{id}/rounds/{round_id}` | 冻结输入与完整历史结果，只读 |
| `GET /api/plans/{id}/runs/{run_id}` | 从数据库读取持久运行状态；每次核验账号、档案、run 的三者关系 |
| `POST /api/plans/{id}/runs/{run_id}/retry` | computed 只补提交；failed/interrupted 用原冻结输入重试，轮次/操作身份不变；running不重复排队 |
| `GET /api/plans/{id}/tasks` | 原任务/轮次/summary结构，增加分页；摘要查询范围一致 |
| `POST /api/plans/{id}/tasks/{task_id}/complete` | `{evidence,note?}`；不再接收客户端 `profile_skills`；完成与升级预览仍分开 |
| `POST /api/plans/{id}/tasks/clear` | 保留原 `drop_pending_events` 二次确认语义，只清本档案任务，不删分析结果、快照或已入账成长 |
| `GET /api/plans/{id}/growth/pending` | 当前档案待入账；后台不扫描账号所有任务 |
| `POST /api/plans/{id}/growth/manual-event` | `{kind,title,skills,evidence,request_key}`；不接收替换目标字段 |
| `POST /api/plans/{id}/growth/pending/withdraw` | `{task_id}`，只撤回本档案未入账成果 |
| `GET /api/plans/{id}/growth/history` | 分页历史 + 本档案能力和计数；正常批次与调整记录区分展示 |
| `POST /api/plans/{id}/growth/history/{record_id}/void` | `{event_index,reason,request_key}`；返回本档案重算能力，不影响其他档案 |
| `POST /api/plans/{id}/growth/next-round` | 仅 `{request_key}`；服务端取档案快照/能力/待办，不收 user_profile、target_job、jd_text 或自由 growth_events |
| `GET /api/health?plan_id=` | 系统级连接/模型状态 + 可选本人档案数据读数；无档案时不默认合并账号历史 |

所有创建/运行请求的 `request_key` 为 UUID，每次明确的新用户操作生成新值，网络重试复用原值；同一键不同内容返回409，不能静默改写原操作。普通读写遇到其他账号对象或跨档案子对象统一404；当前档案忙返回409；输入格式问题422；数据库不可用/队列满503，不透出驱动密码或 SQL 参数。

### 7.2 新建与预览

“开始分析”顺序为 `POST /plans` 创建并选中草稿档案，再 `POST /plans/{id}/analyses` 提交初次运行；第二步失败时档案保持“尚未开始/失败，可重试或删除”，不能显示永远排队，也不能由客户端隐式创建第二份。

查看旧结果直接 GET，不重新调用模型；保留显式预览接口以免丢失现有只读分析能力，预览也存为该档案的运行结果，但不提交任务、成长、能力或首轮快照。没有成功首轮时不能进行预览/下一轮/补录，提示先完成首次分析。

### 7.3 短事务与运行恢复

新 `backend/plan_compute.py` 提供无业务存储写入的计算边界；`backend/services.py` 负责输入加载/事务编排，`backend/plan_repository.py` 负责带归属条件的数据库操作，现有 `backend/jobs.py` 只调度、不做业务结果唯一来源。

1. **领取**：先预留队列容量；短事务锁定档案，校验可写/不忙，按 `(plan_id,request_key)` 去重，创建 queued 轮次并递增本档案序号，设置 `active_run_id`；事务或调度失败释放容量并标明失败状态。
2. **冻结**：工作线程独立开 Session，领取该轮次并生成 `attempt_token`，取本档案版本、快照、能力、待办任务版本、有效历史和选定模型通道，保存到 `input_json` 后关闭事务。
3. **计算**：只用上述值调用工作流；`growth_node` 输出候选成长记录和能力，不写 JSON/SQL；首轮读取自述资料提取基线，后续跳过重复画像提取，沿用冻结基线与要求。
4. **检查点**：用短事务核验父档案仍存在、active_run_id、attempt_token、input_version 一致，把完整候选结果写进 `result_json`、状态改 computed；不符合则丢弃结果，不建父档案。
5. **提交**：再用短事务做同样核验，原子保存当批成长、更新当前能力、保存新任务、结局及轮次成功状态、递增档案 version、清 active_run_id；同一操作重复提交只返回已保存结果。
6. **重启**：从数据库恢复 queued；上次进程的 running 标 interrupted，用户可重试；computed 只补数据库提交，不重调模型；数据库已是 succeeded 时不再次执行。

本轮仍限定单进程、一个模型工作线程，不引入租约心跳与分布式锁。运行中同档案的完成/撤回/作废/补录/清任务写入返回409并提示结束后重试，其他档案不受这把行锁影响；删除是例外，始终允许锁定并删除。这样不需要在推理期间长期锁库，也不接受正在改变的半套输入。

```python
def is_current_attempt(plan, run) -> bool:
    return (
        plan is not None
        and plan.active_run_id == run.id
        and plan.version == run.input_version
        and run.status in {"running", "computed"}
    )
```

调用检查点/提交时另比较工作线程持有的 attempt_token 与记录中的值；所有 UPDATE 带这些条件，影响行数不是1就拒绝提交。不能只在推理开始前检查一次。任务去重、作废引用、成长批量规则仍复用既有确定性算法，不给“删除”开绕过升级门槛的旁路。

### 7.4 必须同步收口的隐藏入口

- `backend/analyze.py:67–103,195–203` 的进程级 `set_store` 和临时 JSON 复制不再出现在 Web 工作流；换成显式只读计算输入。
- `agents/growth_agent.py` 不再自行加载用户级历史或写 store；`graph/state.py`、`graph/workflow.py` 明确区分 initial 与 continuation 输入，下一轮不重新抽取自述等级。
- `backend/services.py` 的岗位画像保存/加载改为当前档案快照，不再写 `JobProfileStore` 的用户目录 JSON。
- `backend/app.py` 启动、`backend/auth.py` 首次注册的旧自动迁移都取消；MySQL 不可用不能自动创建旧 SQLite 或用户 JSON 目录。
- `backend/jobs.py` 运行记录绑定 owner+plan；删除后清理记录，`_execute` 的成功/失败/异常回调须重新查记录是否仍在，不能对删除前持有的对象继续设置 result。运行详情每次还要通过数据库归属检查。
- `main.py` 普通分析与 `--loop` 改为使用本项目的鉴权 HTTP 接口，令牌读取 `CAREER_API_TOKEN` 环境变量，服务地址由 `--api-url` 指定（默认本机），轮次操作要求 `--plan-id`；缺少身份/档案就明确拒绝，不再绕过服务写全局 JSON。只做已有 CLI 行为接入，不加新业务能力。
- 旧 JSON store 类可保留给只读迁移、离线历史规则测试；应用运行链路不得再实例化默认真实用户 store。相关旧测试的语义必须更新，不能为了让测试变绿保留串档分支。


## 8. 任务分解（TDD）

每个任务都按「先写失败测试 → 跑红 → 最小实现 → 跑绿 → 全量回归」推进；测试只用合成账号与临时数据库。数据库地址统一由 `CAREER_DB_URL` 提供，未配置时沿用现有 `runs/app.db`，**运行期连接失败不会自动退回 JSON**。

### 任务 1：数据库底座与基础资料

**Files**
- Create: `backend/models.py`（UserProfile、CareerPlan、AnalysisRound、PlanTask、GrowthRecord）
- Modify: `backend/db.py`（URL 解析、MySQL 连接参数、`init_db()` 建六表）、`backend/config.py`（`database_url()`、连接池常量）
- Create: `backend/profile_service.py`（读取/更新基础资料、版本冲突）
- Test: `backend/tests/test_archive_db.py`

**Interfaces**
- `db.engine_for(url)`、`db.session_factory(url)`、`db.init_db()`
- `profile_service.get_profile(factory, user_id)` → `{major,grade,skills,projects,version}`
- `profile_service.put_profile(factory, user_id, payload, expected_version)` → 新版本；冲突抛 `VersionConflict`

**Steps**
- [ ] 写测试：空库建出六表；重复 `init_db()` 幂等；资料首读返回空字段；并发版本冲突 409；外键级联删除计划后子行归零。
- [ ] 跑 `pytest backend/tests/test_archive_db.py -v`，确认因缺少模型/函数失败。
- [ ] 实现模型与引擎；MySQL 参数 `pool_pre_ping/pool_recycle=1800/pool_size=5/max_overflow=0/pool_timeout=5`；SQLite 测试打开外键。
- [ ] 跑绿并跑一遍现有认证测试，确认账号表兼容。

### 任务 2：档案仓库、接口与删除

**Files**
- Create: `backend/plan_repository.py`、`backend/plans_api.py`
- Modify: `backend/app.py`（挂载 `/api/plans` 与 `/api/profile`）
- Test: `backend/tests/test_plan_api.py`

**Interfaces**
- `create_plan(factory, owner_id, request_key, title, target_job, jd_text, profile)`
- `list_plans(factory, owner_id, limit, before)`、`get_plan(factory, owner_id, plan_id)`
- `delete_plan(factory, owner_id, plan_id)`（行锁 + 级联 + 删除计数摘要）
- 路由：`GET/PUT /api/profile`、`POST/GET /api/plans`、`GET/DELETE /api/plans/{id}`

**Steps**
- [ ] 测试：同账号同 `request_key` 只建一份；同名岗位不同 JD 建两份；越权 GET/DELETE 一律 404；删除 A 后 A 的轮次/任务/成长归零且 B、账号、资料不变；重复删除仍 404；删除计数与实际行数一致。
- [ ] 跑红。
- [ ] 实现仓库与路由；所有查询强制带 `owner_id`；删除使用 `with_for_update()` 与单事务。
- [ ] 跑绿；补 `DELETE /api/plans/{id}` 删除后清理该档案运行缓存的单元测试。

### 任务 3：档案级存储适配与运行生命周期

**Files**
- Create: `backend/plan_stores.py`（`SqlLoopStore`、`SqlGrowthStore`，复用 `LoopStore`/`GrowthStore` 的读方法与纯规则）
- Modify: `backend/deps.py`（按登录用户 + plan_id 构造 `Stores`）、`backend/analyze.py`（计算输入改为显式快照，不读用户目录缓存）、`backend/jobs.py`（记录绑 owner+plan，删除后回调失效）
- Modify: `backend/app.py` 现有分析/轮次/运行路由增加档案上下文
- Test: `backend/tests/test_plan_runs.py`

**Interfaces**
- `SqlLoopStore(factory, owner_id, plan_id)`；`SqlGrowthStore(factory, owner_id, plan_id)`
- 两者 `load()` 从数据库物化既有结构，`_write(data)` 在单事务内同步；不读写账号级 JSON
- `jobs.submit(..., owner=user_id, plan_id=plan_id)`；`jobs.drop_plan(plan_id)`

**Steps**
- [ ] 测试：A 的 `append_round` 不进入 B 的 `rounds`；A 完成任务后 B 的待入账为空；成长写入只落在 A 的 `growth_records`；删除 A 后迟到的成功回调不写回；运行中删除后列表 404；同档案重复 `request_key` 不重复建轮。
- [ ] 跑红。
- [ ] 实现适配器：轮次 `payload_json` 保存原轮次条目，任务写 `tasks`；成长条目逐行写 `growth_records`，`load()` 按创建顺序重建；`_write` 同步后校验档案仍存在，否则抛 `PlanGone`。
- [ ] 分析路径使用档案快照；模型推理期间不持事务；结果提交前校验 `active_run_id + version`。
- [ ] 跑绿。

### 任务 4：任务与成长全面限档

**Files**
- Modify: `backend/services.py`（`complete_task`、`pending`、`withdraw`、`void`、`manual`、`clear`、`next-round` 全部只接收档案 `Stores`）
- Modify: `agents/growth_loop.py`（`pending_growth_events` 已通过 store 限档，补跨档案回归测试）
- Modify: `backend/app.py` 对应路由
- Test: `backend/tests/test_plan_growth_boundary.py`

**Steps**
- [ ] 测试：A 完成任务、B 生成下一轮时 B 的 pending 为空；A 作废只重算 A；A 清任务不影响 B、不清已入账成长；A 的补录不带目标字段且不改 B；A 的下一轮仍用 A 的快照与能力；同 `request_key` 重试不重复入账。
- [ ] 跑红；实现只需把接口收紧到档案 `Stores`，并删除“账号最大轮次”兜底。
- [ ] 跑绿并跑 `tests/test_growth_next_round.py`。

### 任务 5：前端档案上下文与迟到响应

**Files**
- Create: `web/src/stores/plans.js`、`web/src/components/PlanSelector.vue`
- Modify: `web/src/api/endpoints.js`（所有业务请求带 `planId`，新增档案/资料接口）、`web/src/utils/storage.js`（账号命名空间）、`web/src/stores/auth.js`、`web/src/stores/profile.js`、`web/src/router/index.js`（`?plan=`）
- Test: `web/src/stores/__tests__/plans.spec.js`、`web/src/api/__tests__/endpoints.spec.js`

**Steps**
- [ ] 测试：切换档案后旧请求成功/失败/`finally` 都不覆盖新状态；A→B→A 旧 epoch 结果被丢弃；401 只对发出时同一 token 生效；URL 指定不存在档案显示找不到；未指定时恢复本账号上次选择。
- [ ] 跑红；实现 `plans` store（列表、activeId、epoch、loading/error）与请求封装。
- [ ] 跑 `npm test` 绿。

### 任务 6：前端档案列表、删除与空态

**Files**
- Modify: `web/src/views/HomeView.vue`、`AnalyzeView.vue`、`TasksView.vue`、`GrowthView.vue`、`OverviewView.vue`、`StatusView.vue`、`App.vue`、`AppSidebar.vue`
- Test: 各 View 现有 spec 增补档案场景

**Steps**
- [ ] 测试：删除确认绑定打开时 ID；取消不发请求；删除当前档案切到最近一份；删最后一份显示空态且基础资料保留；非当前档案删除不改变当前选择；所有页显示当前档案名。
- [ ] 跑红；实现列表/切换/删除与无档案空态，保持现有视觉语言。
- [ ] 跑 `npm test` 与 `npm run build`。

### 任务 7：旧数据只读迁移工具

**Files**
- Create: `scripts/migrate_legacy_plans.py`
- Modify: `backend/migration.py`（只保留只读盘点与显式导入）
- Test: `backend/tests/test_legacy_migration.py`

**Steps**
- [ ] 测试：合成 `loop.json/growth.json` 导入为一份 `legacy_readonly` 档案；重复执行不重复导入；同账号多岗位旧数据不自动拆分；缺归属字段时拒绝猜测；删除迁入档案后再次执行不复活（除非显式 `--reimport`）。
- [ ] 跑红；实现 `--dry-run`（默认）、`--apply`、`--user`、`--confirm`，写入前打印计数并校验备份哈希。
- [ ] 跑绿；不接入应用启动、不自动执行。

### 任务 8：回归、性能与切换门槛

- [x] 后端全量：`.venv/Scripts/python.exe -m pytest tests backend/tests -q`。
- [x] 前端全量：`cd web && npm test && npm run build`。
- [x] 合成数据多账号多档案场景跑一遍，覆盖第 5 节 16 个验收场景。
- [x] `EXPLAIN` 检查 `(user_id,updated_at)`、`(plan_id,status)`、`(plan_id,round_no)`；列表分页不取大字段。
- [x] 浏览器验收：创建 A/B、切换、A→B→A、删除 A、删最后一份、刷新、重新登录。
- [ ] 切换步骤：备份 `runs/app.db` → 建 MySQL 库与最小权限账号 → 设 `CAREER_DB_URL` → `python -m backend.db --init` → 迁移账号 → 迁移旧业务数据（显式）→ 启动并验收。
- [ ] 回退：停止服务、恢复原 `CAREER_DB_URL`/SQLite 与备份；新版不写旧 JSON，回退前不得删除旧原件。

## 9. 测试矩阵

| 层 | 覆盖 |
| --- | --- |
| 仓库/事务 | 六表建表、幂等初始化、复合外键、级联删除、并发版本、request_key 幂等、越权 404 |
| 档案隔离 | A/B 轮次、任务、成长、能力、快照、作废、清任务、下一轮、同名岗位 |
| 运行生命周期 | queued/running/computed/failed/interrupted、删除竞态、迟到结果丢弃、重试不重复、重启恢复 |
| 前端 | plans store、迟到响应、token 代次、URL 档案、删除确认、空态、每页 planId |
| 迁移 | 只读盘点、重复执行、混合归属、不复活已删记录、备份校验 |
| 兼容 | 现有认证、健康检查、任务分页、并发完成、模型降级、目标结局测试继续通过 |

## 10. 自审

- 规格覆盖：第 1.1 节 10 条均有任务承接；删除、运行中删除、迁移未知归属在任务 2/3/7。
- 占位检查：所有任务都列出文件、接口、失败测试与命令，无“稍后补充”。
- 类型一致：统一使用 `owner_id/plan_id/request_key/run_id/active_run_id/attempt_token/version`；前端统一 `plans.activeId/epoch`。
- 明确不做：账号注销、回收站、跨档案能力迁移、自动迁移、多进程队列、聊天档案化。
