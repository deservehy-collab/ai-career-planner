# AI 职业规划平台

这是一个在本机运行的 AI 职业规划平台：把一份招聘要求原文交给它，七个 Agent 拆出能力差距、学习路线和实战项目；每完成一项任务并提交证据，成果写回下一轮的能力基线。当前版本还能基于同一份档案生成简历（Word / PDF），以及做一轮按档案出题的面试陪练。

- **默认不调用云端 API**：不切通道时推理全部走本机 Ollama 的 `qwen2.5:3b`；也可在「系统状态」页切到 OpenAI 兼容的云端通道。
- **结论可回查**：岗位要求对应到招聘原文的句子，技能名在原文里逐字出现才算依据。
- **结算是确定性的**：等级、匹配度、能否升级由代码规则计算，模型只负责读文本、写措辞。
- **单机可复现**：六张关系表默认落在 SQLite，不需要 Redis、Docker 或云端 API Key（默认通道）；需要时整个库可切到 MySQL 8。
- **档案互不串用**：同一账号可以有多份求职档案，每份档案的分析、任务、成长与能力升级各自独立。

> 评审导航：10 分钟复现见第 1 节；复现性声明见第 2 节；功能与原理见第 3 节；测试与评测见第 6 节。

## 1. 10 分钟极速复现（评审专用）

从一台已装好 Python、Node.js 与 Ollama 的电脑开始，按顺序执行即可。本仓库包含运行所需的全部源代码、依赖清单、环境变量模板和测试；默认不需要云端账号、私有数据或外部数据库。

### 1.1 时间预算

| 阶段 | 步骤 | 用时 | 完成标志 |
| --- | --- | --- | --- |
| 装依赖 | 第 1–4 步 | 4–6 分钟（取决于网络与模型下载） | `.venv`、`qwen2.5:3b`、前后端依赖就绪 |
| 启动 | 第 5–6 步 | 1–2 分钟 | `/api/ping` 返回 ok，5173 能打开登录页 |
| 主流程 | 第 7 步 | 1–3 分钟 | 一轮七节点分析结果 |
| 验证 | 第 8 步 | 约 2 分钟 | 后端 699、前端 153 条测试通过 |

### 1.2 环境要求

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

默认不需要 MySQL/PostgreSQL、Redis、Docker，也不需要任何云端大模型 API Key。账号、基础资料、求职档案、分析轮次、任务与成长记录都在六张关系表里；默认使用项目内置的 SQLite，首次启动自动建表。设置 `CAREER_DB_URL` 可整体切到 MySQL 8（见 `docs/delivery/` 的切换与回退说明）。Chrome / Edge 只在导出简历 PDF 时使用；没有浏览器时导出 Word 仍可用。

### 1.3 依赖清单

后端直接依赖（`requirements.txt`，均为经过验证的精确版本；传递依赖由 pip 自动安装）：

| 依赖 | 版本 | 用途 |
| --- | --- | --- |
| `langchain-ollama` | 1.1.0 | 调用本地 Ollama 的 Chat 接口 |
| `langchain-openai` | 1.7.2 | 调用云端 OpenAI 兼容通道（可选通道） |
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
| `vitest` | ^3.2.7 | 前端单元测试（devDependency） |
| `jsdom` | ^26.1.0 | 测试用 DOM 环境（devDependency） |
| `@vue/test-utils` | ^2.5.1 | 组件挂载与断言（devDependency） |

简历 Word / PDF 导出不引入任何新依赖：Word 由项目内代码直接写 OOXML，PDF 复用本机浏览器的打印引擎。

### 1.4 环境变量模板

变量全部可选，不建 `.env` 也能完整运行。模板原文见 [`.env.example`](.env.example)，复制成项目根目录的 `.env` 即可：

```dotenv
# ── 认证 ──────────────────────────────────────────────────────────────
# 不设置时首次使用会随机生成并写入 runs/jwt_secret.key。
# JWT_SECRET=please-change-me

# ── 本地模型 ──────────────────────────────────────────────────────────
# 七节点分析、AI 助手、面试出题都使用这个地址。
# OLLAMA_HOST=http://localhost:11434

# ── 云端模型通道（可选）───────────────────────────────────────────────
# 不配置 CLOUD_API_KEY 时，云端选项在页面上置灰，本地通道不受影响。
# CLOUD_BASE_URL=https://open.bigmodel.cn/api/paas/v4
# CLOUD_API_KEY=
# CLOUD_MODEL=glm-4.7-flash

# ── 存储与导出（可选）─────────────────────────────────────────────────
# 留空 = 使用 runs/app.db 的 SQLite；填 mysql+pymysql://user:pass@host/db 则整库切 MySQL 8。
# CAREER_DB_URL=
# PDF 导出的浏览器路径；留空时自动查找 Chrome / Edge。
# RESUME_PDF_BROWSER=
```

| 变量 | 作用 | 默认 |
| --- | --- | --- |
| `JWT_SECRET` | JWT 签名密钥；设置后不再读写 `runs/jwt_secret.key` | 首次启动随机生成并写入该文件 |
| `OLLAMA_HOST` | 模型调用使用的 Ollama 地址（分析、AI 助手、面试出题都走它） | `http://localhost:11434` |
| `CLOUD_BASE_URL` | 云端通道的 OpenAI 兼容接口地址 | `https://open.bigmodel.cn/api/paas/v4` |
| `CLOUD_API_KEY` | 云端通道的 API key；不配置时云端选项在页面上置灰 | 空 |
| `CLOUD_MODEL` | 云端通道的模型名 | `glm-4.7-flash` |
| `CAREER_DB_URL` | 整库数据库地址；空值使用本地 SQLite | 空（`runs/app.db`） |
| `RESUME_PDF_BROWSER` | 简历 PDF 导出使用的浏览器可执行文件 | 自动查找 Chrome / Edge |

**已存在的环境变量优先**，不会被 `.env` 覆盖；改完 `.env` 需要重启后端。也可以不用 `.env`，直接把变量导出到当前终端：

```bash
# Git Bash / macOS / Linux
set -a; . ./.env; set +a
```

端口固定为：后端 `8000`，前端 `5173`。前端通过 Vite 代理把 `/api` 转发到 `http://127.0.0.1:8000`（见 `web/vite.config.js`），浏览器侧是同源请求，因此无需配置 CORS 或前端环境变量。

### 1.5 分步操作

#### 第 1 步：获取代码并创建虚拟环境（约 1 分钟）

```bash
git clone https://github.com/deservehy-collab/ai-career-planner.git
cd ai-career-planner
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
cd ..
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

**7.1 岗位分析**

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

**7.2 档案、简历与面试**

1. 「我的档案」可以新建第二份求职档案，切换后分析、任务与成长各自独立；
2. 「简历制作」会把当前档案的专业、年级、技能与项目带入草稿，可编辑 A4 预览并导出 Word / PDF；
3. 「面试陪练」按当前档案出 6 道题（技术 / 项目 / 动机 / 反问），参考要点默认收起；题目不写库、不进成长记录。

**7.3 任务与成长**

1. 在「学习与项目」里打开一条任务，提交证据后才会标记完成；
2. 「成长记录」出现对应技能，下一轮计划的缺口按新能力基线重算。

#### 第 8 步：跑自动化测试与构建（约 2 分钟）

在仓库根目录执行：

```bash
# Windows
.venv/Scripts/python.exe -m pytest tests backend/tests -q

# macOS / Linux
.venv/bin/python -m pytest tests backend/tests -q
```

再执行前端测试与构建（同样不需要后端或 Ollama 在运行）：

```bash
cd web
npm test
npm run build
```

通过标准（2026-11-13 本机实测）：

```text
699 passed, 2 xfailed, 131 subtests passed in 87.51s
Test Files  22 passed (22)
     Tests  153 passed (153)
✓ built in 1.60s
```

后端测试不需要 Ollama 在运行，也不会读写项目里的 `runs/`（使用临时目录构造 store）。`2 xfailed` 是 Windows 上并发写 JSON 的已知边界，用例照跑、失败留痕，不计入回归失败。

### 1.6 这一遍要看什么

1. **粘了原文**：结果页每条岗位要求都能展开看到它对应的原文句子，标记为「原文依据」；匹配度分母只统计有原文支撑的要求。
2. **不粘原文、但岗位在 32 个内置画像里**（AI 应用、会计、护理、教师、设计、机械、法律等）：依据栏标题变成「内置资料摘要」，逐条能回查到资料原句和出处编号，但**照样不显示匹配度百分比** —— 那是同类岗位的通行说法，不是这家公司的话。
3. **不粘原文、岗位也不在画像里**：岗位要求整栏标注「模型补充」、不显示匹配度，页面明说没有一句能回查，而不是摆一排 0。
4. **任务完成**：任务可以一键完成（不填内容，记为「自述完成 · 未附凭证」），也可以附凭证或心得；自述属于弱证据，不会被当成「已满足」的直接证据。
5. **成长闭环**：完成一条任务后，成长记录里出现对应技能；下一轮计划的缺口会按新能力重算。
6. **档案隔离**：新建第二份档案后切换，旧档案的任务与成长不会混进来；删除档案会整份清理，账号基础资料和其他档案保留。
7. **简历**：编辑后导出 Word / PDF，缺的事实保持空白，不会由模型编造；草稿按账号 + 档案存在本机浏览器。
8. **面试**：题目只依据当前档案与简历草稿，电话、邮箱不会进入提示词；生成失败时页面给可读文案和重试按钮。

界面上的「演示数据」标识表示数据由演示流程现场产生，不含真实用户信息。

### 1.7 常见问题

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| 前端提示后端未连接 / 请求 502 | 后端没启动，或端口不是 8000 | 确认第 5 步进程还在；前端代理固定指向 `127.0.0.1:8000` |
| 分析一直排队或报模型错误 | Ollama 没启动，或模型没拉取 | `ollama list`；`curl http://localhost:11434/api/tags`；必要时 `ollama serve` |
| 首次分析很慢 | 3B 模型首次加载 + 多次模型调用 | 等 1–3 分钟；纯 CPU 会更久，属正常 |
| 面试出题提示「模型没有按格式返回题目」 | 小模型偶发坏 JSON，后端已自动重采样一次 | 点页面「重试」；仍失败可在「系统状态」切云端通道 |
| 简历 PDF 导出返回 503 | 本机找不到 Chrome / Edge | 设置 `RESUME_PDF_BROWSER` 指向浏览器，或改导出 Word |
| 简历导出返回 422 | 内容为空，或照片不是 2MB 内的 JPG/PNG | 先填姓名、求职意向或任意一段经历；换一张合规照片 |
| `python` 不是内部或外部命令 | Python 未加入 PATH | 安装时勾选 Add to PATH，或直接写 Python 的绝对路径 |
| PowerShell 不允许激活虚拟环境 | 执行策略限制 | 不用激活，直接调用 `.venv/Scripts/python.exe` |
| `npm install` 报 ERESOLVE 或引擎不匹配 | Node 版本过低 | 升级到 Node 18+，推荐 20/22 LTS |
| 测试出现 2 个 xfailed | Windows 并发写文件的已知边界 | 不是回归失败；用例照跑、失败留痕，见「设计边界」 |
| 用 `demo` 登录失败 | 干净仓库没有预置账号 | 先到注册页注册 `demo / demo1234` |

## 2. 复现性声明

- **代码与依赖齐全**：仓库包含运行所需的全部源代码、`requirements.txt`、`web/package.json` 与 `package-lock.json`、`.env.example` 和测试；默认不依赖云端 API、私有数据、外部数据库或向量库。公开仓库未登录可访问（HTTP 200）。
- **版本与提交可核验**：首个提交 2026-10-08，当前共 18 次提交，最近一次为 2026-11-13；工作区不含运行数据或密钥（`runs/`、`.env` 被 gitignore）。
- **提交粒度完整**：历史是 18 次按功能模块推进的线性提交（第 18 次为本文档定稿），不是一次性导入。首个提交只有 `agents/`、`graph/`、`main.py` 与依赖清单（17 个文件、904 行），是能跑通流水线的后端骨架；前端在 `b222c23`（第 7 次提交）才出现，知识库、鉴权、六表存储、简历导出、面试陪练在后续提交中逐个落地。每条提交说明写明了该次交付的模块。
- **AI 协作留痕**：开发过程与 AI 编程助手的对话按主题整理为三份 Prompt 历史快照（岗位匹配 Agent、学习路线与项目 Agent、成长闭环 Agent），与提交记录对应，随作品材料单独提交。对话按原始会话整理，保留技术事实、报错与数字，口语与重复内容做了压缩；仓库内不含会话中的本机路径、账号与密钥。
- **模型与参数固定**：`agents/llm.py` 使用 `qwen2.5:3b`，`temperature=0.2`、`num_ctx=8192`、`num_predict=2048`；面试出题单独使用 `temperature=0.3`、`num_predict=1400`、`num_ctx=8192`，并用 JSON Schema 约束输出。同一模型版本下，证据回查、匹配度分母、技能归一、成长门禁等判定由确定性代码完成；模型生成的自由文本与个别等级判断可能措辞不同。
- **测试可复现**：测试 patch 掉模型调用、用临时目录构造 store，不碰项目 `runs/`、不需要 Ollama 在跑，因此是秒级、结果确定的回归。2026-11-13 实测：后端 `699 passed, 2 xfailed, 131 subtests passed`（87.5 秒），前端 22 个文件 153 条用例（约 16 秒），`npm run build` 通过（1.6 秒）。
- **评测可复现**：`golden/cases.yaml` 是 24 条人工标注用例，覆盖四类岗位与主要证据路径；`python scripts/eval_golden.py --dry-run` 只校验用例形状、不调模型，已在干净环境通过。
- **运行数据从零开始**：首次启动自动创建 `runs/app.db`（六张表）与 `runs/jwt_secret.key`；删除 `runs/` 等于清空账号与业务数据，重新注册即可从零复现。截图与视频使用本地演示账号现场产生的数据，不含真实用户信息。
- **验证环境**：Windows 11 x64、Python 3.12.3、Node 24.21.0、Ollama 0.34.4、RTX 4050 6 GB + 16 GB 内存、Chrome。macOS / Linux 将路径换成 `.venv/bin/`，其余命令相同。

## 3. 项目怎么工作

### 3.1 页面与能力

| 路由 | 页面 | 能做什么 |
| --- | --- | --- |
| `/overview` | 概览 | 登录后的默认落地页，给出当前档案、最近一轮和下一步入口 |
| `/assistant` | AI 助手 | 与当前模型通道流式对话，会话跨路由保持 |
| `/home` | 我的档案 | 账号基础资料、档案列表与当前能力表 |
| `/analyze` | 岗位分析 | 七节点工作流：岗位要求 / 技能差距 / 学习路线 / 项目推荐 |
| `/tasks`、`/tasks/learning`、`/tasks/projects` | 学习与项目 | 任务清单、学习路线、实战项目三个子页 |
| `/growth` | 成长记录 | 本轮条、成长历史、撤回与作废 |
| `/resume` | 简历制作 | 按档案自动带入，A4 预览，导出 Word / PDF |
| `/interview` | 面试陪练 | 按档案与简历草稿出 6 题，参考要点可展开 |
| `/status`、`/status/data`、`/status/diag` | 系统状态 | 模型通道、队列、存储与知识库只读读数 |

### 3.2 目录结构

| 目录 | 职责 |
| --- | --- |
| `backend/` | Web 层适配：路由、认证、后台任务队列、按用户隔离的数据层。**不实现业务规则**，全部委托 `agents/` 与 `graph/` |
| `agents/` | 业务实现：七个 Agent 节点 + 两个 JSON store + 技能词表（`skill_dict.py`） |
| `graph/` | LangGraph 流水线编排：`profile → growth → retrieval → job → assessment → learning → project` |
| `knowledge/` | `skills/`（22 份）、`projects/`（66 份）的 YAML 是技能等级标准与项目模板来源；`jobs/*.yaml`（32 份）提供岗位画像、规范技能名，以及带 `source_id` 的资料条目 |
| `agents/external_sources.py` | 把岗位画像里的资料条目转成与「粘贴原文」同形状的证据，带条数与重要性护栏；纯查表，不联网 |
| `web/` | Vue 3 + Vite 前端；`src/**/__tests__/` 是 Vitest 单元测试 |
| `backend/tests/` | 后端回归测试：接口边界、鉴权、并发与生命周期 |
| `tests/` | 业务回归测试：技能名归一、成长记账、学习/项目计划校验 |
| `golden/`、`scripts/eval_golden.py` | 黄金评测集与运行器：固定输入、确定性计分、与上一份结果对比回归 |

### 3.3 七节点流水线

| 节点 | 做什么 |
| --- | --- |
| profile | 整理专业、年级、自述技能与目标岗位 |
| growth | 读取成长记录，形成本轮能力基线 |
| retrieval | 把用户粘贴的招聘原文逐句切开；没粘原文时退到内置资料摘要；两者都没有就交出空证据并写明原因 |
| job | 从原文中抽取岗位要求，标注依据来源 |
| assessment | 评估能力差距，计算匹配度 |
| learning | 生成学习路线与阶段目标 |
| project | 推荐实战项目并给出验收标准 |

`retrieval` 与 `profile → growth` 从起点并行，在 `job` 前汇合；其余节点顺序执行。`assessment` 后有一个提前收尾分支：所有岗位要求都已有直接证据时直接结束，不再生成没有缺口可挂的任务。一次分析的结果会写入任务记录，任务完成后通过成长闭环影响下一轮。

### 3.4 岗位依据的三档来源

`retrieval_status` 只有三种取值，来源逐条可回查：

1. **`user_pasted`｜你当场粘贴的招聘要求原文**（`AnalyzeRequest.jd_text`，上限 6000 字）：`agents/user_jd.py` 纯文本切句，不调模型；每条岗位要求都要求技能名在原文中逐字出现，模型自报的编号一律不收。这是唯一能给「满足这份 JD」匹配度百分比的一档（`requirement_basis = "user_pasted"`）。
2. **`official_digest`｜内置资料摘要**：没粘原文、但目标岗位匹配到 `knowledge/jobs/*.yaml` 画像时，`agents/external_sources.py` 把画像里带 `source_id` 的资料原句转成同形状的证据。**能逐条回查到条目，但那是同类岗位的通行说法，不是这家公司的话**，因此 `requirement_basis` 最高只到 `knowledge_base`，同样不给匹配度百分比。每技能最多取 2 条、单轮最多 24 条，超出部分按重要性丢弃并在结果里如实计数。
3. **`no_source`｜没有原文可依**：既没粘原文、岗位也不在画像里，节点交出空证据并写明原因，流水线照常走；岗位要求由模型按该行业的通行标准拆解，逐条标注 `model_supplement`，前端明说「没有一句能回查」。

系统里**没有向量检索，也没有联网抓取，更没有某家真实公司的 JD 语料**；第 2 档的条目是仓库里维护的 YAML，`agents/skill_dict.py` 的技能词表同样是查表，不是检索。一条岗位要求最终算哪一档来源，由 `agents/source_attribution.py` 单点派生成 `user_jd / official_data / knowledge_base / model_inference` 四值——**回查不到证据编号的一律算模型推断**，后端只算一次，前端只读这份结论。五个 Agent 的提示词按领域自适应书写，非信息技术领域（医护、金融、法律、教育等）按专业知识、实操技能、行业工具、证书资质、软技能拆解。系统对任意专业/岗位都能分析，质量取决于你粘的原文有多完整；项目推荐与等级标准以 AI 岗为样本，未收录时会如实标注，不会伪造来源。

### 3.5 确定性结算与成长闭环

- 模型负责读文本、抽要求和写措辞；等级、匹配度、升级资格由 `agents/growth_rules.py` 等确定性代码结算。
- 每轮最多升一档、只升不降；成果类型决定等级上限：学习任务最高到初级，项目最高到中级，高级不通过自动结算授予。
- 任务可以一键完成，凭证与心得选填：不填记为自述完成（弱证据），填了记为已附凭证；自述不得覆盖已有的强证据，目标达成仍要求直接证据。
- 完成后写入成长记录，下一轮计划按新能力基线重算缺口。
- 详细规则与测试见 `agents/growth_agent.py`、`agents/assessment_agent.py`、`tests/`。

### 3.6 多用户与档案隔离

- 除 `/api/ping`、注册、登录外，所有接口要求 Bearer token；`get_current_user` 验签后还会查库确认账号仍存在。
- 业务数据按账号隔离：接口里的 `owner_id` 只取登录态，越权访问一律 404；`GET /api/runs/{run_id}` 只能看到本人提交的后台任务。
- 同账号可以有多份**求职档案**。每次主动「开始新档案」创建独立档案，后续轮次归当前档案；档案的分析、任务、成长与能力升级互不串用。
- 建档时冻结基础资料与 JD 快照；之后修改账号基础资料只影响新档案，不追溯旧档案。
- 删除档案会级联清理它的轮次、任务与成长记录，账号基础资料和其他档案保留；运行中的任务若晚到，结果会被丢弃。
- 登录限速按 IP + 用户名统计失败次数，注册限速按 IP 统计成功建号数（进程内滑动窗口）。

### 3.7 简历与面试（只读能力）

- **简历制作**：页面按当前档案带入专业、年级、技能与项目，前端编辑、A4 预览。Word 由 `backend/resume_docx.py` 直接生成 OOXML（零新增依赖）；PDF 由 `backend/resume_pdf.py` 调本机 Chrome / Edge 的 `--print-to-pdf` 渲染。两个导出接口都只接收当前编辑内容，**不读也不写业务数据**；草稿按「账号 + 档案」存在浏览器 localStorage。
- **面试陪练**：`POST /api/interview/questions` 只读档案，证据来自 `target_job`、`jd_text`（截断 3000 字）、`profile_snapshot`、`current_skills` 以及前端传来的简历草稿；电话、邮箱等联系方式不进入提示词。输出 6 题、分类为技术 / 项目 / 动机 / 反问，反问由代码确定性生成。本地通道用 Ollama 的 JSON Schema 结构化输出，格式不达标会自动重采样一次；结果不写库、不进成长记录。

## 4. 数据布局

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

简历草稿、面试结果、AI 助手会话都属于浏览器侧状态，不进入 `runs/`，也不参与六张表的结算。

## 5. 设计边界与已知限制

这版是单机应用，下面几条是当前版本的真实边界，按此预期使用即可：

1. **单进程部署**：所有分析与规划任务共用一条队列，同时最多 3 个，超出的请求直接返回 503。任务锁、数据隔离与迁移逻辑都依赖单进程，不能用多 worker 或多实例启动。
2. **存储是单机级别**：默认 SQLite，可整体切到 MySQL 8；两种后端不能同时写。六张表按 `plan_id` 划清档案边界，删除档案由数据库外键级联。它适合单机、少量用户，不替代分布式数据库，也不支持多实例并发部署。
3. **登录令牌不能提前吊销**：JWT 默认 24 小时有效；退出登录只清除浏览器里的令牌。账号被删除后接口会拒绝该账号，但已签发的令牌在过期前仍能通过签名校验。
4. **限速按进程计数**：登录/注册限速是进程内滑动窗口，多实例部署时各进程独立计数（当前部署口径就是单进程）。
5. **交互式 API 文档默认关闭**：接口除探活、注册、登录外都需要 Bearer token，文档页自身没有鉴权入口，因此默认不开放 Swagger；需要调试时可在后端临时开启。
6. **本地 3B 模型的能力上限**：模型只负责读文本和写措辞，结论由确定性代码结算；自由文本的措辞、个别等级判断可能与示例不同。非 AI 岗位的技能标准与项目模板没有收录时，系统会如实标注「未收录」，不会编造来源。
7. **CLI 与 Web 数据不互通**：根目录 `main.py` 是早期单用户命令行工具，读写 `runs/` 根目录的 JSON；Web 端业务数据在 `runs/app.db`（或 `CAREER_DB_URL` 指向的库）的六张表里。不要在 Web 使用期间用 CLI 改同一份数据。
8. **内置资料摘要覆盖 32 个岗位画像**：`knowledge/jobs/*.yaml` 共有 490 条带 `source_id` 的资料条目（64 个出处编号），是项目内维护的同类岗位通行说法，不是联网抓取的实时 JD。它只让「没粘原文」的一轮从「没有依据」变成「有可回查的参考依据」，**不会**因此给出匹配度；陌生岗位依旧走 `no_source`，`/status` 的「岗位资料」读数用来确认这份覆盖是否真的存在。
9. **面试出题不落库**：刷新页面后能否恢复取决于本机缓存；生成过程中直接关标签页会中断请求。题目深浅受本地 3B 模型能力限制，复杂岗位偶尔只到概念确认层。

## 6. 测试与评测

```bash
# 后端回归（业务 + 接口）
.venv/Scripts/python.exe -m pytest tests backend/tests -q

# 前端单元测试与构建
cd web
npm test
npm run build
```

- 测试全程不调 Ollama、不碰项目 `runs/`：模型调用一律 patch 掉消费方模块上的 `structured_invoke`，store 用 `tempfile` 临时目录构造，鉴权在 `tests/conftest.py` 的收集阶段就被覆盖成固定测试用户。所以整套跑完不需要本机起着模型。
- 当前结果（2026-11-13）：后端 `699 passed, 2 xfailed, 131 subtests passed`（87.5 秒），前端 22 个文件 153 条用例（约 16 秒），构建 1.6 秒。`2 xfailed` 是 Windows 上并发写 JSON 的已知边界，用例照跑、失败留痕，不算回归失败。
- 后端覆盖档案隔离与删除、鉴权与限速、任务并发与队列上限、运行生命周期、旧数据迁移、简历内容与导入导出、面试出题格式与失败映射；前端覆盖模型通道切换、聊天会话跨路由保持与刷新恢复、分析页降级提示、档案切换与删除、简历编辑器、面试陪练状态。

黄金评测集用于「改 prompt / 换模型 / 动知识库之后，效果是变好还是变坏」：

```bash
# 只校验用例文件，不调模型（秒级）
.venv/Scripts/python.exe scripts/eval_golden.py --dry-run

# 跑全部 24 条并保存结果
.venv/Scripts/python.exe scripts/eval_golden.py --json-out runs/eval/golden-latest.json

# 与上一份结果对比，列出回归用例
.venv/Scripts/python.exe scripts/eval_golden.py --baseline runs/eval/golden-latest.json
```

评测集只写**确定能判定**的期望：知识库规范技能、证据门槛、领域隔离、高优先级缺口覆盖；不写「文风好不好」这类主观项。用例在临时目录里跑，不写真实成长记录。
