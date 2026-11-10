"""档案与基础资料接口。

这里只做参数校验、归属映射和事务边界；业务规则在
:mod:`backend.plan_repository` 与 :mod:`backend.profile_service`。
所有路由都要求登录，``owner_id`` 只取登录态，不接受请求体里的 user_id。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from backend import config, profile_service, services
from backend.auth import get_current_user
from backend.deps import (
    create_archive_factory,
    create_task_manager,
    stores_for_plan,
)
from backend.jobs import TaskManager
from backend.models import AnalysisRound, CareerPlan
from backend.plan_repository import (
    PlanNotFound,
    create_plan,
    delete_plan,
    get_plan,
    list_plans,
)
from backend.schemas import (
    AnalyzeRequest,
    AttachEvidenceRequest,
    ClearTasksRequest,
    CompleteTaskRequest,
    ManualGrowthEventRequest,
    UserProfileRequest,
    VoidGrowthEventRequest,
    WithdrawPendingRequest,
)

__all__ = ["router"]

router = APIRouter(tags=["plans"])


class ProfilePayload(UserProfileRequest):
    version: int = Field(default=0, ge=0)


class PlanCreateRequest(BaseModel):
    request_key: str = Field(min_length=1, max_length=64)
    title: str = Field(default="", max_length=200)
    target_job: str = Field(min_length=1, max_length=config.TARGET_JOB_MAX_CHARS)
    jd_text: str = Field(default="", max_length=config.JD_TEXT_MAX_CHARS)
    user_profile: UserProfileRequest


class PlanAnalysisRequest(BaseModel):
    request_key: str = Field(min_length=1, max_length=64)
    kind: str = Field(default="initial", pattern="^(initial|preview)$")


class PlanNextRoundRequest(BaseModel):
    request_key: str = Field(min_length=1, max_length=64)


def _owner(current: dict) -> int:
    return int(current["id"])


@router.get("/profile", summary="读取账号基础资料")
def get_profile(
    current: dict = Depends(get_current_user),
    factory=Depends(create_archive_factory),
) -> dict:
    return profile_service.get_profile(factory, _owner(current))


@router.put("/profile", summary="保存账号基础资料")
def put_profile(
    payload: ProfilePayload,
    current: dict = Depends(get_current_user),
    factory=Depends(create_archive_factory),
) -> dict:
    try:
        return profile_service.put_profile(
            factory, _owner(current), payload.model_dump(), payload.version
        )
    except profile_service.VersionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/plans", status_code=201, summary="创建一份求职档案")
def post_plan(
    payload: PlanCreateRequest,
    current: dict = Depends(get_current_user),
    factory=Depends(create_archive_factory),
) -> dict:
    return create_plan(
        factory,
        _owner(current),
        request_key=payload.request_key,
        title=payload.title,
        target_job=payload.target_job,
        jd_text=payload.jd_text,
        profile=payload.user_profile.model_dump(),
    )


@router.get("/plans", summary="列出本人求职档案")
def get_plans(
    limit: int = Query(default=20, ge=1, le=100),
    before: str | None = Query(default=None, max_length=64),
    current: dict = Depends(get_current_user),
    factory=Depends(create_archive_factory),
) -> dict:
    return list_plans(factory, _owner(current), limit=limit, before=before)


@router.get("/plans/{plan_id}", summary="查看一份档案")
def get_one_plan(
    plan_id: str,
    current: dict = Depends(get_current_user),
    factory=Depends(create_archive_factory),
) -> dict:
    data = get_plan(factory, _owner(current), plan_id)
    if data is None:
        raise HTTPException(status_code=404, detail="找不到这份档案")
    return data


@router.delete(
    "/plans/{plan_id}", status_code=status.HTTP_204_NO_CONTENT,
    summary="永久删除一份档案及其全部在线数据",
)
def delete_one_plan(
    plan_id: str,
    current: dict = Depends(get_current_user),
    factory=Depends(create_archive_factory),
    manager: TaskManager = Depends(create_task_manager),
) -> Response:
    try:
        delete_plan(factory, _owner(current), plan_id)
    except PlanNotFound as exc:
        raise HTTPException(status_code=404, detail="找不到这份档案") from exc
    manager.drop_plan(plan_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ── 档案内的分析与运行 ────────────────────────────────────────────────────
@router.post(
    "/plans/{plan_id}/analyses", status_code=202,
    summary="在档案内发起首次分析或预览",
)
def post_plan_analysis(
    plan_id: str,
    payload: PlanAnalysisRequest,
    current: dict = Depends(get_current_user),
    factory=Depends(create_archive_factory),
    manager: TaskManager = Depends(create_task_manager),
) -> dict:
    stores = stores_for_plan(factory, _owner(current), plan_id)
    has_round = stores.loop.latest_round() is not None
    if payload.kind == "initial" and has_round:
        raise HTTPException(status_code=409, detail="这份档案已经完成首次分析，不能改写首轮基线")
    if payload.kind == "preview" and not has_round:
        raise HTTPException(status_code=409, detail="这份档案还没有成功的首轮分析，不能预览")

    analyze = AnalyzeRequest(
        user_profile=stores.profile_snapshot or {},
        target_job=stores.target_job or "",
        jd_text=stores.jd_text or None,
        new_plan=False,
    )
    run_id = manager.submit(
        services.run_analyze_and_save, stores, analyze,
        kind=payload.kind, owner=_owner(current), plan_id=plan_id,
    )
    with factory.begin() as session:
        plan = session.scalar(
            select(CareerPlan)
            .where(CareerPlan.id == plan_id, CareerPlan.user_id == _owner(current))
            .with_for_update()
        )
        if plan is None:
            manager.drop_plan(plan_id)
            raise HTTPException(status_code=404, detail="找不到这份档案")
        if plan.state == "draft":
            plan.state = "ready"
    return {
        "plan_id": plan_id, "run_id": run_id,
        "status": "queued", "kind": payload.kind,
    }


@router.post(
    "/plans/{plan_id}/runs/{run_id}/cancel",
    summary="停止档案内一次还没结束的分析（档案保留）",
)
def cancel_plan_run(
    plan_id: str,
    run_id: str,
    current: dict = Depends(get_current_user),
    factory=Depends(create_archive_factory),
    manager: TaskManager = Depends(create_task_manager),
) -> dict:
    stores_for_plan(factory, _owner(current), plan_id)  # 归属与存在性
    snapshot = manager.snapshot(run_id, owner=_owner(current))
    if snapshot is None or snapshot.get("plan_id") != plan_id:
        raise HTTPException(status_code=404, detail=f"未知的 run_id：{run_id}")
    if snapshot.get("kind") == "next_round":
        # 下一轮会把成果正式写进成长历史，没有中途停止的安全切口。
        raise HTTPException(
            status_code=409,
            detail="「生成下一轮」正在写入成长记录，不能中途停止；请等它跑完。",
        )
    result = manager.cancel_run(run_id, plan_id=plan_id, owner=_owner(current))
    if result["reason"] == "not_found":
        raise HTTPException(status_code=404, detail=f"未知的 run_id：{run_id}")
    return {
        "plan_id": plan_id,
        "run_id": run_id,
        "canceled": result["canceled"],
        "status": result["status"],
        "reason": result["reason"],
    }


@router.get("/plans/{plan_id}/runs/{run_id}", summary="查询档案内一次运行")
def get_plan_run(
    plan_id: str,
    run_id: str,
    current: dict = Depends(get_current_user),
    factory=Depends(create_archive_factory),
    manager: TaskManager = Depends(create_task_manager),
) -> dict:
    stores_for_plan(factory, _owner(current), plan_id)  # 归属与存在性
    snapshot = manager.snapshot(run_id, owner=_owner(current))
    if snapshot is None or snapshot.get("plan_id") != plan_id:
        raise HTTPException(status_code=404, detail=f"未知的 run_id：{run_id}")
    return snapshot


@router.get("/plans/{plan_id}/rounds", summary="列出档案内的分析轮次")
def get_plan_rounds(
    plan_id: str,
    current: dict = Depends(get_current_user),
    factory=Depends(create_archive_factory),
) -> dict:
    stores = stores_for_plan(factory, _owner(current), plan_id)
    data = services.list_tasks(stores, limit=1)
    return {
        "rounds": data.get("rounds") or [],
        "latest_goal": data.get("latest_goal"),
        "summary": data.get("summary"),
    }


@router.get("/plans/{plan_id}/rounds/{round_id}", summary="查看一轮的冻结输入与结果")
def get_plan_round(
    plan_id: str,
    round_id: str,
    current: dict = Depends(get_current_user),
    factory=Depends(create_archive_factory),
) -> dict:
    stores = stores_for_plan(factory, _owner(current), plan_id)
    with factory() as session:
        row = session.scalar(
            select(AnalysisRound).where(
                AnalysisRound.id == round_id,
                AnalysisRound.plan_id == plan_id,
            )
        )
        if row is None:
            raise HTTPException(status_code=404, detail="找不到这一轮分析")
        round_no = row.round_no
        frozen_input = dict(row.input_json or {})
        frozen_result = dict(row.result_json or {})
    entry = next(
        (item for item in stores.loop.load().data.get("rounds") or []
         if item.get("round") == round_no),
        None,
    )
    if entry is None:
        raise HTTPException(status_code=404, detail="找不到这一轮分析")
    return {
        "round": round_no,
        "entry": entry,
        "tasks": entry.get("tasks") or [],
        "input": frozen_input,
        "result": frozen_result,
    }


# ── 档案内的任务与成长 ────────────────────────────────────────────────────
@router.get("/plans/{plan_id}/tasks", summary="查询档案内的任务")
def get_plan_tasks(
    plan_id: str,
    round_no: int | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=500, ge=1, le=2000),
    offset: int = Query(default=0, ge=0),
    current: dict = Depends(get_current_user),
    factory=Depends(create_archive_factory),
) -> dict:
    stores = stores_for_plan(factory, _owner(current), plan_id)
    try:
        return services.list_tasks(
            stores, round_no=round_no, status=status_filter,
            limit=limit, offset=offset,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post(
    "/plans/{plan_id}/tasks/{task_id}/complete", summary="提交档案内任务完成证据",
)
def post_plan_task_complete(
    plan_id: str,
    task_id: str,
    payload: CompleteTaskRequest,
    current: dict = Depends(get_current_user),
    factory=Depends(create_archive_factory),
) -> dict:
    stores = stores_for_plan(factory, _owner(current), plan_id)
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


@router.post(
    "/plans/{plan_id}/tasks/{task_id}/evidence", summary="给档案内已完成任务补凭证",
)
def post_plan_task_evidence(
    plan_id: str,
    task_id: str,
    payload: AttachEvidenceRequest,
    current: dict = Depends(get_current_user),
    factory=Depends(create_archive_factory),
) -> dict:
    stores = stores_for_plan(factory, _owner(current), plan_id)
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


@router.post("/plans/{plan_id}/tasks/clear", summary="清空档案内任务清单")
def post_plan_clear_tasks(
    plan_id: str,
    payload: ClearTasksRequest | None = None,
    current: dict = Depends(get_current_user),
    factory=Depends(create_archive_factory),
) -> dict:
    stores = stores_for_plan(factory, _owner(current), plan_id)
    return services.clear_tasks(
        stores, drop_pending_events=bool(payload and payload.drop_pending_events),
    )


@router.get("/plans/{plan_id}/growth/pending", summary="档案内待处理成长成果")
def get_plan_pending(
    plan_id: str,
    current: dict = Depends(get_current_user),
    factory=Depends(create_archive_factory),
) -> dict:
    return services.list_pending(stores_for_plan(factory, _owner(current), plan_id))


@router.post("/plans/{plan_id}/growth/pending/withdraw", summary="撤回档案内待处理成果")
def post_plan_withdraw_pending(
    plan_id: str,
    payload: WithdrawPendingRequest,
    current: dict = Depends(get_current_user),
    factory=Depends(create_archive_factory),
) -> dict:
    stores = stores_for_plan(factory, _owner(current), plan_id)
    result = services.withdraw_pending_event(stores, payload.task_id, payload.round_no)
    if not result.get("withdrawn"):
        raise HTTPException(status_code=422, detail={
            "withdrawn": False,
            "reason": result.get("reason"),
        })
    return result


@router.post("/plans/{plan_id}/growth/manual-event", summary="档案内手动补录成果")
def post_plan_manual_event(
    plan_id: str,
    payload: ManualGrowthEventRequest,
    current: dict = Depends(get_current_user),
    factory=Depends(create_archive_factory),
) -> dict:
    stores = stores_for_plan(factory, _owner(current), plan_id)
    result = services.add_manual_event(
        stores,
        kind=payload.kind,
        title=payload.title,
        skills=payload.skills,
        evidence=payload.evidence,
        target_job=stores.target_job,
    )
    if not result.get("added"):
        raise HTTPException(status_code=422, detail={
            "added": False,
            "reason": result.get("reason"),
        })
    return services.jsonable(result)


@router.get("/plans/{plan_id}/growth/history", summary="查询档案内成长历史")
def get_plan_growth_history(
    plan_id: str,
    current: dict = Depends(get_current_user),
    factory=Depends(create_archive_factory),
) -> dict:
    return services.growth_history(
        stores_for_plan(factory, _owner(current), plan_id)
    )


@router.post("/plans/{plan_id}/growth/history/void", summary="作废档案内一条成果")
def post_plan_void_growth_event(
    plan_id: str,
    payload: VoidGrowthEventRequest,
    current: dict = Depends(get_current_user),
    factory=Depends(create_archive_factory),
) -> dict:
    stores = stores_for_plan(factory, _owner(current), plan_id)
    result = services.void_growth_event(
        stores, payload.entry_index, payload.event_index, payload.reason
    )
    if not result.get("voided"):
        raise HTTPException(status_code=422, detail={
            "voided": False,
            "reason": result.get("reason"),
        })
    return result


@router.post(
    "/plans/{plan_id}/growth/next-round", status_code=202,
    summary="在档案内生成下一轮计划",
)
def post_plan_next_round(
    plan_id: str,
    payload: PlanNextRoundRequest,
    current: dict = Depends(get_current_user),
    factory=Depends(create_archive_factory),
    manager: TaskManager = Depends(create_task_manager),
) -> dict:
    stores = stores_for_plan(factory, _owner(current), plan_id)
    run_id = manager.submit(
        services.run_next_round, stores,
        user_profile=stores.profile_snapshot,
        target_job=stores.target_job,
        jd_text=stores.jd_text,
        kind="next_round", owner=_owner(current), plan_id=plan_id,
    )
    return {
        "plan_id": plan_id, "run_id": run_id, "status": "queued",
        "kind": "next_round",
    }
