"""FastAPI 应用与路由。

接口总览
--------
=============================  ==========================================  ============  ================================
方法 / 路径                    作用                                        需要登录？    写盘？
=============================  ==========================================  ============  ================================
GET  /api/ping                 无鉴权探活（不探测任何依赖）                 ❌ 公开       ❌ 只读
POST /api/auth/register        注册账号（首个账号认领旧业务数据）           ❌ 公开       ✅ users 表 + 一次性迁移
POST /api/auth/login           登录，换 JWT（失败限速）                     ❌ 公开       ❌ 只读 users 表
GET  /api/auth/me              当前登录用户                                ✅           ❌ 只读
POST /api/auth/logout          退出登录（前端丢弃 token）                   ✅           ❌ 只读
POST /api/chat/stream          AI 助手流式对话（SSE，按当前通道走本地或云端）  ✅           ❌ 不写业务数据
POST /api/analyze              只读分析（202 + run_id；队列满 503）         ✅           ❌ 只读
POST /api/analyze-and-save     分析并保存第一轮任务（202；队列满 503）      ✅           ✅ 只写本人 loop.json
GET  /api/runs/{id}            查后台运行状态（只看本人提交的）             ✅           ❌ 只读
POST /api/tasks/{id}/complete  登记任务完成 + 升级预览                     ✅           ✅ 只写本人 loop.json
GET  /api/growth/pending       待处理成长成果                              ✅           ❌ 只读
POST /api/growth/pending/withdraw 撤回一条待处理成果                       ✅           ✅ 本人 loop.json
POST /api/growth/manual-event  手动补录一条成长成果                        ✅           ✅ 只写本人 loop.json
POST /api/growth/next-round    生成下一轮计划（202；队列满 503）           ✅           ✅ 本人 growth/loop.json
GET  /api/growth/history       成长历史                                    ✅           ❌ 只读
POST /api/growth/history/void  作废一条已入账成果（追加调整记录）           ✅           ✅ 本人 growth.json
GET  /api/tasks                轮次 / 任务清单（limit/offset 分页）         ✅           ❌ 只读
GET  /api/health               系统状态（全量探测 Ollama/知识库/store）    ✅           ❌ 只读
GET  /api/status/llm-channel   查询模型通道（local / cloud）               ✅           ❌ 只读
PUT  /api/status/llm-channel   切换模型通道                                ✅           ✅ settings.json
POST /api/resume/docx          导出简历 Word（不写业务数据）                ✅           ❌ 只生成文件
=============================  ==========================================  ============  ================================

除 ``/api/ping`` 与注册/登录两个入口外，**所有接口都要求 Bearer token**，
包括 ``/api/health``：它回报本机模型与知识库的可达性，属于运维读数，不该让
未登录的请求白拿一次全量探测（一次 health 会连 Ollama 并读知识库）。登录页的
"后端未连接"提示不依赖这个接口 —— 那类前置检查用 ``/api/ping`` 或请求失败
本身来判断。

**多用户隔离**：业务数据按登录用户隔离 —— store 目录是
``runs/users/user_{id}/``（见 ``backend.deps.create_stores``），后台 run
记录按提交者归属（``GET /api/runs/{id}`` 只看本人提交的，别人的 run_id
视同不存在）。单用户时代的旧 ``runs/loop.json`` / ``runs/growth.json`` 由
旧 JSON 数据不再启动时自动认领；需要时用 ``scripts/migrate_legacy_plans.py`` 显式导入。

**成长历史只有两条写入路径**：正常记账走 ``POST /api/growth/next-round``
（内部经 ``growth_node`` 走阶段五正式写入）；用户显式作废走
``POST /api/growth/history/void``（只追加调整记录，不删改原条目）。
任务完成接口只登记完成与证据，升级只是不落盘的预览 —— 这一点由
tests/test_growth_loop_api.py 强制。

启动（单 worker 是硬要求，见 backend/config.py 的 TASK_WORKERS 说明）::

    .venv/Scripts/python.exe -m uvicorn backend.app:app --host 127.0.0.1 --port 8000 --workers 1
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from agents.llm import (
    CHANNEL_CLOUD, active_model_name, cloud_auth_failed, cloud_configured,
    cloud_settings, get_channel, set_channel,
)
from agents.loop_store import TASK_STATUSES
from backend import config, services, settings
from backend.analyze import run_workflow
from backend.auth import get_current_user, router as auth_router
from backend.chat import router as chat_router
from backend.db import init_db
from backend.deps import create_stores, create_task_manager
from backend.jobs import STATUS_QUEUED, QueueFull, TaskManager
from backend.plans_api import router as plans_router
from backend.resume_api import router as resume_router
from backend.schemas import (
    AnalyzeRequest, AttachEvidenceRequest, ClearTasksRequest, CompleteTaskRequest,
    LlmChannelRequest,
    ManualGrowthEventRequest, NextRoundRequest, VoidGrowthEventRequest,
    WithdrawPendingRequest,
)
from backend.services import Stores

__all__ = ["app"]

logger = logging.getLogger("backend.app")

# 鉴权：除 /api/ping 与 /api/auth/register、/api/auth/login 外，所有业务接口
# （含 /api/health）都要求 Bearer token。``Depends(get_current_user)`` 可被
# 测试全局覆盖（tests/conftest.py）。
auth_required = [Depends(get_current_user)]


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    """启动时把建表做掉，退出时停掉后台线程池。

    建表：原先只有第一个登录/注册请求才会触发建表，冷启动时 ``users`` 表
    可能压根不存在。幂等，重复调用无副作用。

    旧 JSON 数据不在启动时自动认领：应用运行链路只读写六张表，旧数据由
    ``scripts/migrate_legacy_plans.py`` 在人工确认后显式导入。

    停机：``cancel_futures=True`` 取消**还没开始**的排队任务并把它们标为失败；
    正在跑的那一个（一次分析要几分钟推理）不强杀，等它自己结束 —— Python 会在
    解释器退出前 join 工作线程，所以这里不假装能立刻打断它。
    """
    init_db()
    restored_channel = settings.load_channel()
    set_channel(restored_channel)
    logger.info("模型通道已恢复：%s", restored_channel)
    try:
        yield
    finally:
        create_task_manager().shutdown(wait=False, cancel_futures=True)


app = FastAPI(
    title="AI Career Agent API",
    version="1.2.0",
    lifespan=_lifespan,
    # 交互式文档默认关闭：接口已要求登录，Swagger 页面本身没有鉴权入口。
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    description=(
        "AI 大学生成长 / 就业智能规划系统的 Web 接口层。"
        "业务逻辑全部复用现有六阶段实现（agents/ 与 graph/），本层只做适配。"
    ),
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=config.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── 全局异常口径 ─────────────────────────────────────────────────────────
@app.exception_handler(QueueFull)
def handle_queue_full(_request: Request, exc: QueueFull) -> JSONResponse:
    """后台队列已满 —— 三个提交任务的 ``POST`` 由 202 变 503。

    响应体刻意与 FastAPI 的 ``HTTPException`` 同构（``{"detail": "..."}``），
    前端的 ``client.js`` 已经按 ``detail`` 读错误文案，不需要为这个状态码特判。
    """
    return JSONResponse(status_code=503, content={"detail": str(exc)})


# 认证路由：/api/auth/register|login|me|logout（register/login 公开，me/logout 自带鉴权）
app.include_router(auth_router)

# 档案与基础资料：/api/profile、/api/plans（六表档案边界）
app.include_router(plans_router, prefix="/api")

# 简历导出：/api/resume/docx（无状态，只生成文件；登录仍为硬要求）
app.include_router(resume_router, prefix="/api")

# AI 助手聊天路由：/api/chat/stream（SSE 流式）。
# 鉴权与业务接口同口径（可被 tests/conftest.py 覆盖），但**不接入 TaskManager**：
# 聊天是一次直连本地模型的流式请求，与七节点规划流水线、两个 store 完全无关。
app.include_router(chat_router, dependencies=auth_required)


# ── 探活 ────────────────────────────────────────────────────────────────
@app.get("/api/ping", tags=["system"], summary="探活")
def ping() -> dict:
    """无鉴权轻量探活：只回答"进程活着"，**不**连任何外部依赖。

    反向代理 / 运维脚本需要一张不带 token 的健康票；全量探测（Ollama、
    知识库、两个 store）继续留在需要登录的 ``/api/health``，两者职责不同。
    """
    return {"status": "ok", "time": datetime.now().isoformat(timespec="seconds")}


# ── 健康检查 ────────────────────────────────────────────────────────────
@app.get("/api/health", tags=["system"], summary="系统状态", dependencies=auth_required)
def get_health(stores: Stores = Depends(create_stores)) -> dict:
    """报告 Ollama / 知识库 / 两个 store 的可达性与可写性。只读。

    要求登录：未登录的请求不该白拿一次全量探测（本接口会连 Ollama 并读知识库）。
    """
    return services.health(stores)


# ── 模型通道（进程设置，持久化在 settings.json）────────────────────────────
@app.get("/api/status/llm-channel", tags=["system"], summary="查询当前模型通道", dependencies=auth_required)
def get_llm_channel() -> dict:
    """当前生效通道、云端是否已配置、实际模型名与鉴权回落标记。只读。"""
    return {
        "channel": get_channel(),
        "cloud_configured": cloud_configured(),
        "active_model": active_model_name(),
        "cloud_model": cloud_settings()["model"],
        "cloud_auth_failed": cloud_auth_failed(),
    }


@app.put("/api/status/llm-channel", tags=["system"], summary="切换模型通道", dependencies=auth_required)
def put_llm_channel(payload: LlmChannelRequest) -> dict:
    """把模型通道切到 local / cloud，并持久化到 ``runs/settings.json``。

    切云端前先确认 key 已配置：没配时直接 422 并说明（前端也会把云端选项置灰，
    这里是第二道保险）。切换只影响**之后**提交的任务，正在跑的 run 不受影响。
    """
    if payload.channel == CHANNEL_CLOUD and not cloud_configured():
        raise HTTPException(
            status_code=422,
            detail="云端通道需要先在 .env 里配置 CLOUD_API_KEY（本地部署无需任何配置）。",
        )
    set_channel(payload.channel)
    try:
        settings.save_channel(payload.channel)
    except OSError as exc:
        logger.warning("通道设置写盘失败：%s", exc)
        raise HTTPException(
            status_code=500,
            detail="通道已切换，但设置没能写入 settings.json，重启后会回到本地。请检查 runs/ 目录权限。",
        ) from exc
    return {
        "channel": get_channel(),
        "cloud_configured": cloud_configured(),
        "active_model": active_model_name(),
        "cloud_model": cloud_settings()["model"],
    }


# ── 分析（只读，异步）─────────────────────────────────────────────────────
@app.post("/api/analyze", status_code=202, tags=["analysis"], summary="发起岗位分析", dependencies=auth_required)
def post_analyze(
    payload: AnalyzeRequest,
    current: dict = Depends(get_current_user),
    stores: Stores = Depends(create_stores),
    manager: TaskManager = Depends(create_task_manager),
) -> dict:
    """把只读分析排进后台队列，立刻返回 ``run_id``。

    一次分析要跑七个节点的本地模型推理，同步等待会让 HTTP 长时间阻塞，
    所以这里只排队；用 ``GET /api/runs/{run_id}`` 查进度与结果。

    分析基线是**当前用户**的成长历史（``baseline_store=stores.growth``），
    **绝不写**任何用户的 ``runs/users/``（由 ``backend.analyze`` 的隔离机制保证）。

    队列已满（``config.TASK_QUEUE_CAP``）时返回 **503**，不创建 run_id。
    """
    run_id = manager.submit(
        run_workflow, payload, baseline_store=stores.growth,
        enable_job_profile=False,
        kind="analyze", owner=current["id"], plan_id=stores.plan_id,
    )
    return {"status": STATUS_QUEUED, "run_id": run_id, "kind": "analyze"}


@app.post("/api/analyze-and-save", status_code=202, tags=["analysis"], summary="分析并保存第一轮任务", dependencies=auth_required)
def post_analyze_and_save(
    payload: AnalyzeRequest,
    current: dict = Depends(get_current_user),
    manager: TaskManager = Depends(create_task_manager),
    stores: Stores = Depends(create_stores),
) -> dict:
    """跑分析，并在**首次**时把产出的学习/项目计划存成本人的第一轮任务。

    这是根治"分析完还是没有任务"冷启动死锁的入口：第一次分析即生成第 1 轮
    任务，翻开「学习与项目」页就能看到。已有轮次时保持只读语义、不重复追加。

    队列已满时返回 **503**，不创建 run_id。
    """
    run_id = manager.submit(
        services.run_analyze_and_save, stores, payload,
        kind="analyze_and_save", owner=current["id"], plan_id=stores.plan_id,
    )
    return {"status": STATUS_QUEUED, "run_id": run_id, "kind": "analyze_and_save"}


@app.get("/api/runs/{run_id}", tags=["system"], summary="查询后台运行状态", dependencies=auth_required)
def get_run(
    run_id: str,
    current: dict = Depends(get_current_user),
    manager: TaskManager = Depends(create_task_manager),
) -> dict:
    """查一次后台运行的排队 / 成功 / 失败状态、错误信息与结果。

    只能看**本人**提交的 run：别人的 run_id 视同不存在，同样返回 404。
    **失败也返回 200**（接口跑通了，是任务本身失败）。
    """
    snapshot = manager.snapshot(run_id, owner=current["id"])
    if snapshot is None:
        raise HTTPException(status_code=404, detail=f"未知的 run_id：{run_id}")
    return snapshot


# ── 任务完成（只写本人的 loop.json）────────────────────────────────────────
@app.post("/api/tasks/{task_id}/complete", tags=["growth"], summary="提交任务完成证据", dependencies=auth_required)
def post_task_complete(
    task_id: str,
    payload: CompleteTaskRequest,
    stores: Stores = Depends(create_stores),
) -> dict:
    """登记任务完成与证据，并返回一份**尚未落盘**的技能升级预览。

    证据为空白或超过 5000 字由 :class:`CompleteTaskRequest` 拦成 422。

    重复提交同一任务**幂等**：返回 ``already_completed=true``，
    且第一次的证据不被覆盖（由 LoopStore.mark_completed 保证）。
    """
    result = services.complete_task(
        stores, task_id, payload.evidence, payload.profile_skills,
        note=payload.note,
    )
    status_code = services.completion_http_status(result)
    if status_code != 200:
        raise HTTPException(status_code=status_code, detail={
            "task_id": task_id,
            "completion_status": result.get("completion_status"),
            "reason": result.get("reason"),
        })
    return services.jsonable(result)


@app.post("/api/tasks/{task_id}/evidence", tags=["growth"], summary="给已完成任务补凭证", dependencies=auth_required)
def post_task_evidence(
    task_id: str,
    payload: AttachEvidenceRequest,
    stores: Stores = Depends(create_stores),
) -> dict:
    """给一条**已完成**任务补上凭证，把自述升级翻成有凭证。

    任务还没完成 → 409；凭证为空或超长 → 422。
    成长历史里已有这条成果时追加一条 ``verification`` 记录（原记录一条不改）；
    还没有历史时只翻任务证据类型，等下一轮正常入账。
    """
    result = services.attach_task_evidence(stores, task_id, payload.evidence)
    status = result.get("status")
    if status == "not_completed":
        raise HTTPException(status_code=409, detail={
            "task_id": task_id,
            "reason": result.get("reason"),
        })
    if status in ("invalid_task_id", "invalid_evidence"):
        raise HTTPException(status_code=422, detail={
            "task_id": task_id,
            "reason": result.get("reason"),
        })
    if status == "unknown_task":
        raise HTTPException(status_code=404, detail={
            "task_id": task_id,
            "reason": result.get("reason"),
        })
    return result


# ── 待处理成长成果（只读）──────────────────────────────────────────────────
@app.get("/api/growth/pending", tags=["growth"], summary="查询待处理成长成果", dependencies=auth_required)
def get_pending(stores: Stores = Depends(create_stores)) -> dict:
    """已完成、但还没交给成长节点的成果。**逐字节只读。**"""
    return services.list_pending(stores)


@app.post("/api/growth/pending/withdraw", tags=["growth"], summary="撤回一条待处理成果", dependencies=auth_required)
def post_withdraw_pending(
    payload: WithdrawPendingRequest,
    stores: Stores = Depends(create_stores),
) -> dict:
    """把一条**尚未计入成长历史**的成果退回未完成，并清空这条完成记录的证据。

    已入账的成果不能从这里撤回（否则历史里会留下无源升级），请改用
    ``POST /api/growth/history/void``。失败时 422，带 ``detail.reason``。
    """
    result = services.withdraw_pending_event(stores, payload.task_id, payload.round_no)
    if not result.get("withdrawn"):
        raise HTTPException(status_code=422, detail={
            "withdrawn": False,
            "reason": result.get("reason"),
        })
    return result


@app.post("/api/growth/manual-event", tags=["growth"], summary="手动补录一条成长成果", dependencies=auth_required)
def post_manual_event(
    payload: ManualGrowthEventRequest,
    stores: Stores = Depends(create_stores),
) -> dict:
    """把用户手动填写的成果写成一条"已完成任务"进本人的 ``loop.json``。

    用于打破"空系统死锁"：尚无任务的全新用户也能直接录入成果，
    让「生成下一轮」有内容可处理。返回 ``added``；失败时带 ``reason``。

    字段上限（标题/技能条数/证据长度）由 :class:`ManualGrowthEventRequest` 拦成 422。
    """
    result = services.add_manual_event(
        stores,
        kind=payload.kind,
        title=payload.title,
        skills=payload.skills,
        evidence=payload.evidence,
        target_job=payload.target_job,
    )
    if not result.get("added"):
        raise HTTPException(status_code=422, detail={
            "added": False,
            "reason": result.get("reason"),
        })
    return services.jsonable(result)


# ── 下一轮规划（正常记账入口）───────────────────────────────────────────────
@app.post("/api/growth/next-round", status_code=202, tags=["growth"], summary="生成下一轮成长计划", dependencies=auth_required)
def post_next_round(
    payload: NextRoundRequest | None = None,
    current: dict = Depends(get_current_user),
    manager: TaskManager = Depends(create_task_manager),
    stores: Stores = Depends(create_stores),
) -> dict:
    """把「生成下一轮计划」排进后台队列，立刻返回 ``run_id``。

    这是**正常记账**的唯一入口，因此必须在单工作线程里串行执行
    （``TaskManager(max_workers=1)``），否则并发的成长写入会互相覆盖。
    用户显式作废不走这里，见 ``POST /api/growth/history/void``（服务层用同一把
    ``_GROWTH_WRITE_LOCK`` 串行化）。

    没有待处理成果时不会跑模型，也不会凭空新建轮次。

    队列已满时同样返回 **503**，不创建 run_id。
    """
    profile = payload.user_profile.model_dump() if payload and payload.user_profile else None
    target_job = payload.target_job if payload else None
    jd_text = payload.jd_text if payload else None
    run_id = manager.submit(
        services.run_next_round, stores,
        user_profile=profile, target_job=target_job, jd_text=jd_text,
        kind="next_round", owner=current["id"], plan_id=stores.plan_id,
    )
    return {"status": STATUS_QUEUED, "run_id": run_id, "kind": "next_round"}


# ── 只读查询 ────────────────────────────────────────────────────────────
@app.get("/api/tasks", tags=["growth"], summary="查询轮次与任务清单", dependencies=auth_required)
def get_tasks(
    round_no: int | None = Query(default=None, description="只看某一轮"),
    status: str | None = Query(default=None, description=f"任务状态，可选 {TASK_STATUSES}"),
    limit: int = Query(default=500, ge=1, le=2000, description="任务清单分页大小"),
    offset: int = Query(default=0, ge=0, description="任务清单偏移量"),
    stores: Stores = Depends(create_stores),
) -> dict:
    """列出本人的历轮任务与完成情况。只读。

    ``limit`` / ``offset`` 只对 ``tasks`` 数组开窗；``rounds`` 与 ``summary``
    永远是全量口径。不传参数时默认 ``limit=500``，老前端行为不变。
    """
    try:
        return services.list_tasks(stores, round_no=round_no, status=status, limit=limit, offset=offset)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/growth/history", tags=["growth"], summary="查询成长历史", dependencies=auth_required)
def get_growth_history(stores: Stores = Depends(create_stores)) -> dict:
    """返回本人真正落盘的成长历史与最新确认能力。只读，损坏时如实带出 warning。"""
    return services.growth_history(stores)


@app.post("/api/growth/history/void", tags=["growth"], summary="作废一条已入账成果", dependencies=auth_required)
def post_void_growth_event(
    payload: VoidGrowthEventRequest,
    stores: Stores = Depends(create_stores),
) -> dict:
    """作废一条已入账成果：**不删原记录**，追加调整记录并重算能力表。

    原条目、原证据保持不变；调整记录写明作废了哪一条、为什么，以及重算后的
    能力表。作废后的成果不会在下一轮被自动重新入账。失败时 422，带 ``detail.reason``。
    """
    result = services.void_growth_event(
        stores, payload.entry_index, payload.event_index, payload.reason
    )
    if not result.get("voided"):
        raise HTTPException(status_code=422, detail={
            "voided": False,
            "reason": result.get("reason"),
        })
    return result


@app.post("/api/tasks/clear", tags=["growth"], summary="清空任务清单", dependencies=auth_required)
def post_clear_tasks(
    payload: ClearTasksRequest | None = None,
    stores: Stores = Depends(create_stores),
) -> dict:
    """把**本人**的任务清单备份后清成空。成长历史与技能等级不受影响。

    清单里还有没记账的打卡成果时返回 ``needs_confirmation``（HTTP 状态仍是 200，
    用 body 说真相）：前端要先把这件事告诉用户，确认后才带
    ``drop_pending_events: true`` 重试。清空前一定先落一个
    ``loop.json.cleared-<时间戳>`` 备份，路径在返回体的 ``backup`` 里。
    """
    return services.clear_tasks(
        stores, drop_pending_events=bool(payload and payload.drop_pending_events),
    )
