# AI Career Agent

AI Career Agent 是一个在本机运行的求职规划系统：把一份招聘要求原文交给它，七个 Agent 拆出能力差距、学习路线和实战项目；每完成一项任务并提交证据，成果会写回下一轮的能力基线。

- **默认不调用云端 API**：不切通道时推理全部走本机 Ollama 的 `qwen2.5:3b`；也可在「系统状态」页切到 OpenAI 兼容的云端通道。
- **结论可回查**：岗位要求对应到招聘原文的句子，技能名在原文里逐字出现才算依据。
- **结算是确定性的**：等级、匹配度、能否升级由代码规则计算，模型只负责读文本、写措辞。
- **单机可复现**：六张关系表默认落在 SQLite，不需要 Redis、Docker 或云端 API Key（默认通道）；需要时整个库可切到 MySQL 8。

## 10 分钟复现（评审专用）

从一台没装过本项目的电脑开始，按顺序执行即可。本仓库包含运行所需的全部源代码、依赖清单、环境变量模板和测试；默认不需要云端账号、私有数据或外部数据库。

### 1. 环境要求

| 组件 | 要求 | 本项目验证环境 |
| --- | --- | --- |
| 操作系统 | Windows 10/11；macOS / Linux 使用等价命令 | Windows 11 x64 |
| Python | 3.11+ | 3.12.3 |
| Node.js | 18+（含 npm） | 24.21.0 / npm 11.19.0 |
| Ollama | 能运行 `qwen2.5:3b` | 0.34.4 |
| 模型 | `qwen2.5:3b`（约 1.9 GB） | 已拉取 |
| 内存 | 建议 16 GB | 16 GB |
| GPU | 可选；有 GPU 更快 | RTX 4050 6 GB |
| 磁盘 | 预留约 4–5 GB | — |
| 浏览器 | Chrome / Edge 最近版本 | Chrome |

默认不需要 MySQL/PostgreSQL、Redis、Docker，也不需要任何云端大模型 API Key。账号、基础资料、求职档案、分析轮次、任务与成长记录都在六张关系表里；默认使用项目内置的 SQLite，首次启动自动建表。设置 `CAREER_DB_URL` 可整体切到 MySQL 8（见 `docs/delivery/` 的切换与回退说明）。

### 2. 依赖清单

后端直接依赖（`requirements.txt`，均为经过验证的精确版本；传递依赖由 pip 自动安装）：

| 依赖 | 版本 | 用途 |
| --- | --- | --- |
| `langchain-ollama` | 1.1.0 | 调用本地 Ollama 的 Chat 接口 |
| `langgraph` | 1.2.14 | 七节点流水线编排 |
| `pydantic` | 2.13.5 | 请求与节点状态模型 |
| `fastapi` | 0.143.0 | Web API |
| `uvicorn` | 0.54.0 | ASGI 服务器 |
| `SQLAlchemy` | 2.1.5 | 六表 ORM（SQLite / MySQL 8） |
| `PyMySQL` | 1.1.3 | MySQL 驱动（仅切换 `CAREER_DB_URL` 时需要） |
| `PyJWT` | 2.15.1 | 登录令牌 |
| `PyYAML` | 6.0.3 | 读取 `knowledge/` 下的 YAML |
| `pytest` | 9.1.1 | 测试运行器（第 8 步需要） |
| `httpx` | 0.28.1 | FastAPI `TestClient` 的依赖（第 8 步需要） |

前端直接依赖（`web/package.json`，由 `web/package-lock.json` 锁定）：

| 依赖 | 版本 | 用途 |
| --- | --- | --- |
| `vue` | ^3.5.13 | 前端框架 |
| `vue-router` | ^4.5.0 | 路由与登录守卫 |
| `pinia` | ^2.3.0 | 状态管理 |
| `vite` | ^6.0.7 | 开发服务器与构建（devDependency） |
| `@vitejs/plugin-vue` | ^5.2.1 | Vue 单文件组件支持（devDependency） |

### 3. 环境变量与端口

后端读取以下可选环境变量，**都有默认值，不设置也能完整运行**：

| 变量 | 作用 | 默认 |
| --- | --- | --- |
| `JWT_SECRET` | JWT 签名密钥；设置后不再读写 `runs/jwt_secret.key` | 首次启动随机生成并写入该文件 |
| `OLLAMA_HOST` | 模型调用使用的 Ollama 地址（七节点分析与页面内 AI 助手都走它） | `http://localhost:11434` |
| `CLOUD_BASE_URL` | 云端通道的 OpenAI 兼容接口地址 | `https://open.bigmodel.cn/api/paas/v4` |
| `CLOUD_API_KEY` | 云端通道的 API key；不配置时云端选项在页面上置灰 | 空 |
| `CLOUD_MODEL` | 云端通道的模型名 | `glm-4.7-flash` |

完整模板见 [`.env.example`](.env.example)。复制成项目根目录的 `.env` 后，后端启动时会自动读取；**已存在的环境变量优先**，不会被 `.env` 覆盖。改完 `.env` 需要重启后端。也可以不用 `.env`，直接把变量导出到当前终端：

```bash
# Git Bash / macOS / Linux
set -a; . ./.env; set +a
```

端口固定为：后端 `8000`，前端 `5173`。前端通过 Vite 代理把 `/api` 转发到 `http://127.0.0.1:8000`（见 `web/vite.config.js`），浏览器侧是同源请求，因此无需配置 CORS 或前端环境变量。

### 4. 分步操作

#### 第 1 步：获取代码并创建虚拟环境（约 1 分钟）

```bash
git clone https://github.com/deservehy-collab/ai-career-agent.git
cd ai-career-agent
python -m venv .venv
```

下面用 `.venv/Scripts/python.exe`（Windows）或 `.venv/bin/python`（macOS / Linux）直接调用虚拟环境，不需要先激活。

#### 第 2 步：安装并拉取本地模型（约 2 分钟，取决于网速）

安装 [Ollama](https://ollama.com) 后：

```bash
ollama pull qwen2.5:3b
ollama list
```

`ollama list` 中能看到 `qwen2.5:3b` 即通过。Windows 安装包会自动启动 Ollama；如果提示连接失败，先运行 `ollama serve`。

#### 第 3 步：安装后端依赖（约 1–1.5 分钟）

```bash
# Windows
.venv/Scripts/python.exe -m pip install -r requirements.txt

# macOS / Linux
.venv/bin/python -m pip install -r requirements.txt
```

#### 第 4 步：安装前端依赖（约 1 分钟）

```bash
cd web
npm install
```

#### 第 5 步：启动后端（新开一个终端，回到仓库根目录）

```bash
# Windows
.venv/Scripts/python.exe -m uvicorn backend.app:app --host 127.0.0.1 --port 8000 --workers 1

# macOS / Linux
.venv/bin/python -m uvicorn backend.app:app --host 127.0.0.1 --port 8000 --workers 1
```

探活：

```bash
curl http://127.0.0.1:8000/api/ping
# {"status":"ok","time":"..."}
```

`--workers 1` 是硬要求：任务队列、数据隔离与迁移依赖单进程，不能用多 worker 启动。

#### 第 6 步：启动前端并注册（约 1 分钟）

另开一个终端：

```bash
cd web
npm run dev
```

打开 <http://localhost:5173/register> 注册账号。仓库不附带任何用户数据（`runs/` 被 gitignore），所以干净克隆里没有现成账号。想用演示账号，就用 `demo / demo1234` 注册一次，之后即可登录；也可以注册自己的账号。

#### 第 7 步：跑一遍主流程（一次分析约 1–3 分钟）

1. 登录后打开左侧「岗位分析」页；
2. 填写专业、年级、目标岗位、自述技能；
3. 把招聘要求原文粘贴到「把招聘要求粘到这里」；
4. 点「开始分析」，等待七节点流水线完成。

可复制的示例招聘要求（也可以换成你的目标岗位）：

```text
【AI 应用开发工程师】
岗位职责：
1. 负责大模型应用的需求分析、方案设计与后端开发；
2. 使用 Python 开发服务端接口，熟悉 FastAPI 等 Web 框架；
3. 参与 RAG、Agent 等应用落地，持续优化效果与性能。
任职要求：
1. 本科及以上学历，计算机相关专业；
2. 熟练掌握 Python，了解 SQL 与 Git；
3. 具备良好的沟通能力与文档习惯，有 LangChain 实践经验者优先。
```

#### 第 8 步：跑自动化测试（约 10 秒）

在仓库根目录执行：

```bash
# Windows
.venv/Scripts/python.exe -m pytest tests backend/tests -q

# macOS / Linux
.venv/bin/python -m pytest tests backend/tests -q
```

再执行前端测试（同样不需要后端或 Ollama 在运行）：

```bash
cd web
npm test
```

通过标准：

```text
492 passed, 2 xfailed
9 passed
```

后端测试不需要 Ollama 在运行，也不会读写项目里的 `runs/`（使用临时目录构造 store）。

时间预算：前 6 步约 6–7 分钟；一次完整分析在本机约 1–3 分钟；测试约 10 秒。首次加载模型会额外占用几十秒，纯 CPU 环境分析会更久。

### 5. 这一遍要看什么

1. **粘了原文**：结果页每条岗位要求都能展开看到它对应的原文句子，标记为「原文依据」；匹配度分母只统计有原文支撑的要求。
2. **不粘原文、但岗位在内置画像里**（会计 / AI 应用 / 新媒体运营 / 中学教师）：依据栏标题变成「内置资料摘要」，逐条能回查到资料原句和出处编号，但**照样不显示匹配度百分比** —— 那是同类岗位的通行说法，不是这家公司的话。
3. **不粘原文、岗位也不在画像里**：岗位要求整栏标注「模型补充」、不显示匹配度，页面明说没有一句能回查，而不是摆一排 0。
4. **任务完成**：学习计划里的任务必须提交证据才能标记完成；证据为空会被拒绝。
5. **成长闭环**：完成一条任务后，成长记录里出现对应技能；下一轮计划的缺口会按新能力重算。

界面上的「演示数据」标识表示数据由演示流程现场产生，不含真实用户信息。

### 6. 常见问题

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| 前端提示后端未连接 / 请求 502 | 后端没启动，或端口不是 8000 | 确认第 5 步进程还在；前端代理固定指向 `127.0.0.1:8000` |
| 分析一直排队或报模型错误 | Ollama 没启动，或模型没拉取 | `ollama list`；`curl http://localhost:11434/api/tags`；必要时 `ollama serve` |
| 首次分析很慢 | 3B 模型首次加载 + 七节点串行 | 等 1–3 分钟；纯 CPU 会更久，属正常 |
| `python` 不是内部或外部命令 | Python 未加入 PATH | 安装时勾选 Add to PATH，或直接写 Python 的绝对路径 |
| PowerShell 不允许激活虚拟环境 | 执行策略限制 | 不用激活，直接调用 `.venv/Scripts/python.exe` |
| `npm install` 报 ERESOLVE 或引擎不匹配 | Node 版本过低 | 升级到 Node 18+，推荐 20/22 LTS |
| 测试出现 2 个 xfailed | Windows 并发写文件的已知边界 | 不是回归失败；用例照跑、失败留痕，见「设计边界」 |
| 用 `demo` 登录失败 | 干净仓库没有预置账号 | 先到注册页注册 `demo / demo1234` |

## 复现性声明

- **代码与依赖齐全**：仓库包含运行所需的全部源代码、`requirements.txt`、`web/package.json` 与 `package-lock.json`、`.env.example` 和测试；默认不依赖云端 API、私有数据、外部数据库或向量库。
- **模型与参数固定**：`agents/llm.py` 使用 `qwen2.5:3b`，`temperature=0.2`、`num_ctx=8192`、`num_predict=2048`。同一模型版本下，证据回查、匹配度分母、技能归一、成长门禁等判定由确定性代码完成；模型生成的自由文本与个别等级判断可能措辞不同。
- **测试可复现**：测试 patch 掉模型调用、用临时目录构造 store，不碰项目 `runs/`、不需要 Ollama 在跑，因此是秒级、结果确定的回归。`2 xfailed` 是 Windows 并发写 JSON 的已知边界，用例仍执行并留痕，不计入回归失败。
- **运行数据从零开始**：首次启动自动创建 `runs/app.db`（六张表）与 `runs/jwt_secret.key`；删除 `runs/` 等于清空账号与业务数据，重新注册即可从零复现。截图与视频使用本地演示账号现场产生的数据，不含真实用户信息。
- **验证环境**：Windows 11 x64、Python 3.12.3、Node 24.21.0、Ollama 0.34.4、RTX 4050 6 GB + 16 GB 内存。macOS / Linux 将路径换成 `.venv/bin/`，其余命令相同。

## 项目怎么工作

### 目录结构

| 目录 | 职责 |
| --- | --- |
| `backend/` | Web 层适配：路由、认证、后台任务队列、按用户隔离的数据层。**不实现业务规则**，全部委托 `agents/` 与 `graph/` |
| `agents/` | 业务实现：七个 Agent 节点 + 两个 JSON store + 技能词表（`skill_dict.py`） |
| `graph/` | LangGraph 流水线编排：`profile → growth → retrieval → job → assessment → learning → project` |
| `knowledge/` | `skills/`、`projects/` 的 YAML 是技能等级标准与项目模板的来源；`jobs/*.yaml` 提供各岗位画像、规范技能名，以及**人工整理、带出处编号的资料原句**（`skills[].evidence`，AI 应用、会计、新媒体运营、中学教师） |
| `agents/external_sources.py` | 把上面那份 `evidence` 转成与「粘贴原文」同形状的证据条目（`provenance = "official_digest"`），带条数与重要性护栏；纯查表，不联网 |
| `web/` | Vue 3 + Vite 前端；`src/**/__tests__/` 是 Vitest 单元测试 |
| `backend/tests/` | 后端回归测试：接口边界、鉴权、并发与生命周期 |
| `tests/` | 业务回归测试：技能名归一、成长记账、学习/项目计划校验 |

### 七节点流水线

| 节点 | 做什么 |
| --- | --- |
| profile | 整理专业、年级、自述技能与目标岗位 |
| growth | 读取成长记录，形成本轮能力基线 |
| retrieval | 把用户粘贴的招聘原文逐句切开；没粘原文时退到内置资料摘要；两者都没有就交出空证据并写明原因 |
| job | 从原文中抽取岗位要求，标注依据来源 |
| assessment | 评估能力差距，计算匹配度 |
| learning | 生成学习路线与阶段目标 |
| project | 推荐实战项目并给出验收标准 |

七个节点顺序执行；一次分析的结果会写入任务记录，任务完成后通过成长闭环影响下一轮。

### 岗位依据的三档来源

`retrieval_status` 只有三种取值，来源逐条可回查：

1. **`user_pasted`｜你当场粘贴的招聘要求原文**（`AnalyzeRequest.jd_text`，上限 6000 字）：`agents/user_jd.py` 纯文本切句，不调模型；每条岗位要求都要求技能名在原文中逐字出现，模型自报的编号一律不收。这是唯一能给「满足这份 JD」匹配度百分比的一档（`requirement_basis = "user_pasted"`）。
2. **`official_digest`｜内置资料摘要**：没粘原文、但目标岗位匹配到 `knowledge/jobs/*.yaml` 画像时，`agents/external_sources.py` 把画像里人工整理、带出处编号（`source_id`）的资料原句转成同形状的证据。**能逐条回查到原句，但那是同类岗位的通行说法，不是这家公司的话**，因此 `requirement_basis` 最高只到 `knowledge_base`，同样不给匹配度百分比。每技能最多取 2 条、单轮最多 24 条，超出部分按重要性丢弃并在结果里如实计数。
3. **`no_source`｜没有原文可依**：既没粘原文、岗位也不在画像里，节点交出空证据并写明原因，流水线照常走；岗位要求由模型按该行业的通行标准拆解，逐条标注 `model_supplement`，前端明说「没有一句能回查」。

系统里**没有向量检索，也没有联网抓取，更没有某家真实公司的 JD 语料**；第 2 档的摘要是仓库里手写的 YAML，`agents/skill_dict.py` 的技能词表同样是查表，不是检索。一条岗位要求最终算哪一档来源，由 `agents/source_attribution.py` 单点派生成 `user_jd / official_data / knowledge_base / model_inference` 四值——**回查不到证据编号的一律算模型推断**，后端只算一次，前端只读这份结论。五个 Agent 的提示词按领域自适应书写，非信息技术领域（医护、金融、法律、教育等）按专业知识、实操技能、行业工具、证书资质、软技能拆解。系统对任意专业/岗位都能分析，质量取决于你粘的原文有多完整；项目推荐与等级标准以 AI 岗为样本，未收录时会如实标注，不会伪造来源。

### 确定性结算与成长闭环

- 模型负责读文本、抽要求和写措辞；等级、匹配度、升级资格由 `agents/growth_rules.py` 等确定性代码结算。
- 每轮最多升一档、只升不降；成果类型决定等级上限：学习任务最高到初级，项目最高到中级，高级不通过自动结算授予。
- 任务必须提交证据才能完成；完成后写入成长记录，下一轮计划按新能力基线重算缺口。
- 详细规则与测试见 `agents/growth_agent.py`、`agents/assessment_agent.py`、`tests/`。

### 多用户与档案隔离

- 除 `/api/ping`、注册、登录外，所有接口要求 Bearer token；`get_current_user` 验签后还会查库确认账号仍存在。
- 业务数据按账号隔离：接口里的 `owner_id` 只取登录态，越权访问一律 404；`GET /api/runs/{run_id}` 只能看到本人提交的后台任务。
- 同账号可以有多份**求职档案**。每次主动「开始新档案」创建独立档案，后续轮次归当前档案；档案的分析、任务、成长与能力升级互不串用。
- 建档时冻结基础资料与 JD 快照；之后修改账号基础资料只影响新档案，不追溯旧档案。
- 删除档案会级联清理它的轮次、任务与成长记录，账号基础资料和其他档案保留；运行中的任务若晚到，结果会被丢弃。
- 登录限速按 IP + 用户名统计失败次数，注册限速按 IP 统计成功建号数（进程内滑动窗口）。

## 数据布局

```text
runs/
├── app.db              # 默认 SQLite：users / user_profiles / career_plans /
│                       # analysis_rounds / tasks / growth_records 六张表
└── jwt_secret.key      # JWT 签名密钥（首次使用时生成；删除等于全员强制登出）
```

设置 `CAREER_DB_URL` 后整库改连 MySQL 8（`mysql+pymysql://...`），不再读写本地 SQLite。
表结构由 `python -m backend.db --init` 幂等创建。

单用户时代（v1.x）的 `runs/loop.json`、`runs/growth.json` **不再在启动或注册时自动迁移**。
需要时用 `scripts/migrate_legacy_plans.py` 先只读盘点，再显式 `--apply --confirm` 导入为一份
`legacy_readonly` 档案；脚本会先备份并校验 SHA-256，原件不改名、不删除。

## 设计边界与已知限制

这版是单机应用，下面几条是当前版本的真实边界，按此预期使用即可：

1. **单进程部署**：所有分析与规划任务共用一条队列，同时最多 3 个，超出的请求直接返回 503。任务锁、数据隔离与迁移逻辑都依赖单进程，不能用多 worker 或多实例启动。
2. **存储是单机级别**：默认 SQLite，可整体切到 MySQL 8；两种后端不能同时写。六张表按 `plan_id` 划清档案边界，删除档案由数据库外键级联。它适合单机、少量用户，不替代分布式数据库，也不支持多实例并发部署。
3. **登录令牌不能提前吊销**：JWT 默认 24 小时有效；退出登录只清除浏览器里的令牌。账号被删除后接口会拒绝该账号，但已签发的令牌在过期前仍能通过签名校验。
4. **限速按进程计数**：登录/注册限速是进程内滑动窗口，多实例部署时各进程独立计数（当前部署口径就是单进程）。
5. **交互式 API 文档默认关闭**：接口除探活、注册、登录外都需要 Bearer token，文档页自身没有鉴权入口，因此默认不开放 Swagger；需要调试时可在后端临时开启。
6. **本地 3B 模型的能力上限**：模型只负责读文本和写措辞，结论由确定性代码结算；自由文本的措辞、个别等级判断可能与示例不同。非 AI 岗位的技能标准与项目模板没有收录时，系统会如实标注「未收录」，不会编造来源。
7. **CLI 与 Web 数据不互通**：根目录 `main.py` 是早期单用户命令行工具，读写 `runs/` 根目录的 JSON；Web 端业务数据在 `runs/app.db`（或 `CAREER_DB_URL` 指向的库）的六张表里。不要在 Web 使用期间用 CLI 改同一份数据。
8. **内置资料摘要只覆盖已收录的 4 个岗位**：`knowledge/jobs/*.yaml` 里的资料条目是人工整理的同类岗位通行说法（当前 86 条、9 份出处），不是真实公司的 JD。它只让"没粘原文"的一轮从"没有依据"变成"有可回查的参考依据"，**不会**因此给出匹配度；陌生岗位依旧走 `no_source`，`/status` 的「岗位资料」读数用来确认这份覆盖是真的存在。

## 测试

```bash
# 后端回归
.venv/Scripts/python.exe -m pytest backend/tests -q

# 全量（业务 + 后端）
.venv/Scripts/python.exe -m pytest tests backend/tests -q
```

- 测试全程不调 Ollama、不碰项目 `runs/`：模型调用一律 patch 掉消费方模块上的 `structured_invoke`，store 用 `tempfile` 临时目录构造，鉴权在 `tests/conftest.py` 的收集阶段就被覆盖成固定测试用户。所以整套跑完是秒级，也不需要本机起着模型。
- 当前结果：后端 `492 passed, 2 xfailed`（约 10 秒），前端 `9 passed`（约 2 秒）。`2 xfailed` 是 Windows 上并发写 JSON 的已知边界，用例照跑、失败留痕，不算回归失败。
- 前端测试覆盖模型通道切换、聊天会话跨路由保持与刷新恢复、分析页的降级提示；`cd web && npm run build` 继续作为构建门禁。
