"""后端集中配置：Ollama、模型、CORS、端口与项目路径。

模型名**不在这里另立一套**，而是直接从 Agent 侧导入（``agents.llm.MODEL_NAME``），
这样前端展示的模型名与真正调用的模型永远不会漂移 —— 本项目不擅自更改
六阶段业务使用的模型。

关于 ``OLLAMA_BASE_URL``
-----------------------
``agents/llm.py`` 里的 ``ChatOllama(model=MODEL_NAME)`` **没有传 base_url**，
因此实际地址由 ollama 客户端的环境变量/默认值决定（默认
``http://localhost:11434``）。后端不修改 Agent 代码，所以这里的常量
**必须与那个默认值一致**；它只用于 ``GET /api/health`` 的连通性探测，
不参与模型调用。若将来要改地址，得改 ``agents/llm.py``（不属本次范围）。
"""

from __future__ import annotations

import os
from pathlib import Path

from agents.llm import MODEL_NAME as CHAT_MODEL

__all__ = [
    "REPO_ROOT", "RUNS_DIR", "KNOWLEDGE_DIR",
    "SKILLS_DIR", "PROJECTS_DIR",
    "CHAT_MODEL", "OLLAMA_BASE_URL", "OLLAMA_TAGS_PATH",
    "HOST", "PORT", "CORS_ORIGINS", "EVIDENCE_MAX_LENGTH", "TASK_WORKERS",
    "NOTE_MAX_CHARS",
    "TASK_RECORD_CAP", "TASK_QUEUE_CAP",
    "AUTH_DB_PATH", "JWT_SECRET_PATH", "JWT_ALGORITHM", "JWT_TTL_HOURS",
    "USERNAME_MIN_LENGTH", "USERNAME_MAX_LENGTH", "PASSWORD_MIN_LENGTH",
    "AUTH_FAILURE_LIMIT", "AUTH_FAILURE_WINDOW_SECONDS",
    "REGISTER_LIMIT", "REGISTER_WINDOW_SECONDS",
    "users_data_root", "user_data_dir", "legacy_store_path",
]

# ── 路径 ────────────────────────────────────────────────────────────────
# 与 agents/*_store.py 同口径：以本文件位置反推仓库根，**与 cwd 无关**。
REPO_ROOT = Path(__file__).resolve().parent.parent
RUNS_DIR = REPO_ROOT / "runs"
KNOWLEDGE_DIR = REPO_ROOT / "knowledge"
SKILLS_DIR = KNOWLEDGE_DIR / "skills"
PROJECTS_DIR = KNOWLEDGE_DIR / "projects"

# ── .env ────────────────────────────────────────────────────────────────
# 不引入 python-dotenv：这里只做最小解析，够本项目这几个 KEY=VALUE 用。
# **已有环境变量优先**，所以 shell 里的临时覆盖不会被 .env 反盖掉。
ENV_FILE = REPO_ROOT / ".env"


def _load_env_file(path: Path) -> None:
    """把 .env 里的键值补进 ``os.environ``；文件不存在时静默跳过。

    支持空行、``#`` 注释、可选 ``export`` 前缀和成对引号；不支持变量展开、
    多行值与行尾注释 —— 本项目的 .env 只有地址、key、模型名，不需要这些。
    """
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key.isidentifier() or key in os.environ:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        os.environ[key] = value


# 在 backend 启动（以及测试导入 config）时加载。agents.llm 的 cloud_settings()
# 是运行时读 os.environ 的，所以这里先导入 llm 再加载也不影响结果。
_load_env_file(ENV_FILE)

# ── 模型（从 Agent 侧导入，见模块 docstring）─────────────────────────────
# CHAT_MODEL 由上方 import 提供。

# ── Ollama ─────────────────────────────────────────────────────────────
OLLAMA_BASE_URL = "http://localhost:11434"
OLLAMA_TAGS_PATH = "/api/tags"

# ── 服务 ────────────────────────────────────────────────────────────────
HOST = "127.0.0.1"
PORT = 8000

# Vite 开发服务器默认端口（前端重建后用这个地址联调）
CORS_ORIGINS = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
]

# ── 业务约束 ────────────────────────────────────────────────────────────
# 任务证据的字符上限：空白拒绝、超长拒绝，由 schemas.CompleteTaskRequest 执行。
EVIDENCE_MAX_LENGTH = 5000

# 任务完成时的可选心得笔记。同样跟着证据进 loop.json（全量读-改-写），
# 所以设上限；证据+心得的**合计**仍受 EVIDENCE_MAX_LENGTH 约束
# （由 schemas.CompleteTaskRequest 的 model_validator 执行）。
NOTE_MAX_CHARS = 1000

# 手动补录成果（/api/growth/manual-event）的输入上限。与任务证据同一套动机：
# loop.json 是全量读-改-写的，任何无界写入都会让所有后续查询变慢。
MANUAL_TITLE_MAX_CHARS = 200
MANUAL_SKILLS_MAX_ITEMS = 50
MANUAL_SKILL_MAX_CHARS = 100
# 分析请求里 growth_events 的条数上限（内容仍由业务层校验，这里只限数量）。
GROWTH_EVENTS_MAX_ITEMS = 200
# 目标岗位的字符上限。
TARGET_JOB_MAX_CHARS = 200
# 用户粘贴的招聘要求原文上限。原文要整段进提示词，而模型的上下文是
# CONTEXT_TOKENS（8192）—— 6000 字中文约占 4000~5000 token，留得下系统提示与
# 输出。超长由前端提示用户截短，不在后端静默裁剪（悄悄裁掉会让用户以为
# "后半段它也读过了"）。原文切成几条证据句子由 agents/user_jd.py 自己决定。
JD_TEXT_MAX_CHARS = 6000

# 后台任务的并发上限。**必须是 1**：runs/growth.json 与 runs/loop.json 的
# 读-改-写只在进程内串行安全（见 tests/test_readonly_boundary.py），
# 且分析链路的成长存储隔离会临时改写进程级全局 store。
TASK_WORKERS = 1

# 后台运行记录的保留上限。记录只存在内存里（进程级单例），原先没有任何上限，
# 一次分析产出的完整 state 会一直挂在 ``TaskManager._records`` 上。
# 淘汰只发生在**已终态**的最旧记录上（queued / running 永不淘汰），
# 被淘汰的 run_id 之后查询返回 404。
TASK_RECORD_CAP = 200

# 同时"在跑或排队"的任务数上限。单 worker + 一次七节点推理要几分钟，
# 没有上限时双击"开始分析"会排出一串重复任务，白等也白算。
# 超出后 ``POST`` 直接 503（不是 202），前端按错误提示处理。
TASK_QUEUE_CAP = 3

# ── 认证（登录 / 用户数据库）─────────────────────────────────────────────
# SQLite 与 JWT 密钥都放在 runs/ 下：那里本就是"可再生成的运行数据"目录，
# 删掉库文件最多是重新注册，不会伤到 knowledge/ 与业务代码。
AUTH_DB_PATH = RUNS_DIR / "app.db"
JWT_SECRET_PATH = RUNS_DIR / "jwt_secret.key"
JWT_ALGORITHM = "HS256"
JWT_TTL_HOURS = 24

USERNAME_MIN_LENGTH = 2
USERNAME_MAX_LENGTH = 32
PASSWORD_MIN_LENGTH = 6

# 认证限速（进程内滑动窗口，见 backend/auth.py）。
# 登录：同一 IP+用户名 在窗口内**失败**超过次数 → 429；成功登录清零计数。
AUTH_FAILURE_LIMIT = 10
AUTH_FAILURE_WINDOW_SECONDS = 900.0
# 注册：同一 IP 在窗口内最多注册这么多账号（防批量灌库；阈值放得宽，
# 正常使用与测试套件都不会碰到）。
REGISTER_LIMIT = 60
REGISTER_WINDOW_SECONDS = 3600.0

# ── 按用户隔离的业务数据目录 ──────────────────────────────────────────────
# 单用户时代（v1.x）业务数据直接躺在 runs/ 根下；多账号之后每个用户一份：
#   runs/users/user_{id}/loop.json
#   runs/users/user_{id}/growth.json
# 根目录**锚定在认证库所在目录**（默认 runs/）：测试把 AUTH_DB_PATH 指到
# 临时目录时，per-user 目录与旧数据迁移会自动跟着隔离，不会碰项目 runs/。
# 旧数据的归属迁移见 backend/migration.py。


def users_data_root() -> Path:
    """per-user 业务数据的根目录（动态读取，测试可整体隔离）。"""
    return Path(AUTH_DB_PATH).parent / "users"


def user_data_dir(user_id: int) -> Path:
    """某个用户专属的 store 目录。"""
    return users_data_root() / f"user_{int(user_id)}"


def legacy_store_path(filename: str) -> Path:
    """单用户时代留在数据根目录下的旧 store 路径（loop.json / growth.json）。"""
    return Path(AUTH_DB_PATH).parent / filename
