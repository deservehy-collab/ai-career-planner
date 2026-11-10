"""求职档案的数据库操作。

所有公开函数都要求显式 ``owner_id``。越权与不存在返回同一个 :class:`PlanNotFound`，
API 层统一映射成 404，避免泄露“这个 ID 是否存在”。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from backend import profile_service
from backend.models import (
    PLAN_STATE_DRAFT,
    PLAN_STATE_LEGACY,
    AnalysisRound,
    CareerPlan,
    GrowthRecord,
    PlanTask,
)

__all__ = [
    "PlanNotFound", "create_plan", "list_plans", "get_plan",
    "plan_detail", "plan_summary", "delete_plan",
]


class PlanNotFound(LookupError):
    """档案不存在，或不属于当前账号。"""


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _owned(session, owner_id: int, plan_id: str) -> CareerPlan:
    plan = session.scalar(
        select(CareerPlan).where(
            CareerPlan.id == plan_id, CareerPlan.user_id == int(owner_id)
        )
    )
    if plan is None:
        raise PlanNotFound(plan_id)
    return plan


def create_plan(
    factory: sessionmaker,
    owner_id: int,
    *,
    request_key: str,
    title: str,
    target_job: str,
    jd_text: str,
    profile: dict,
) -> dict:
    """创建档案；同一账号同一 ``request_key`` 只创建一次。

    资料快照与档案行在同一事务提交：要么都成功，要么都不留下。
    """
    try:
        with factory.begin() as session:
            existing = session.scalar(
                select(CareerPlan)
                .where(
                    CareerPlan.user_id == int(owner_id),
                    CareerPlan.request_key == request_key,
                )
                .with_for_update()
            )
            if existing is not None:
                return {"plan_id": existing.id, "state": existing.state, "created": False}

            profile_service.write_snapshot(session, owner_id, profile)
            plan = CareerPlan(
                id=uuid.uuid4().hex,
                user_id=int(owner_id),
                request_key=request_key,
                title=(title or target_job).strip()[:200],
                target_job=target_job.strip()[:200],
                jd_text=(jd_text or "").strip(),
                profile_snapshot_json=dict(profile),
                current_skills_json=[],
                next_round_no=1,
                state=PLAN_STATE_DRAFT,
            )
            session.add(plan)
            session.flush()
            return {"plan_id": plan.id, "state": plan.state, "created": True}
    except IntegrityError:
        # 并发重试撞上 (user_id, request_key) 唯一键：返回已经创建的那份。
        with factory() as session:
            existing = session.scalar(
                select(CareerPlan).where(
                    CareerPlan.user_id == int(owner_id),
                    CareerPlan.request_key == request_key,
                )
            )
            if existing is None:
                raise
            return {"plan_id": existing.id, "state": existing.state, "created": False}


def _legacy_payload(session, plan) -> dict:
    """旧档案的 payload 只在列表里按需单独查询，避免分页时把大字段整页取回。"""
    payload = getattr(plan, "legacy_payload_json", None)
    if payload is None:
        payload = session.scalar(
            select(CareerPlan.legacy_payload_json).where(CareerPlan.id == plan.id)
        )
    return payload if isinstance(payload, dict) else {}


def _legacy_parts(session, plan) -> tuple[dict, dict]:
    payload = _legacy_payload(session, plan)
    loop = payload.get("loop") if isinstance(payload.get("loop"), dict) else {}
    growth = payload.get("growth") if isinstance(payload.get("growth"), dict) else {}
    return loop, growth


def _counts(session, plan) -> dict:
    if plan.state == PLAN_STATE_LEGACY:
        loop, growth = _legacy_parts(session, plan)
        rounds = loop.get("rounds") if isinstance(loop.get("rounds"), list) else []
        tasks = sum(
            len(entry.get("tasks") or [])
            for entry in rounds
            if isinstance(entry, dict) and isinstance(entry.get("tasks"), list)
        )
        history = growth.get("history") if isinstance(growth.get("history"), list) else []
        return {"rounds": len(rounds), "tasks": int(tasks), "growth": len(history)}

    def count(model) -> int:
        return int(session.scalar(
            select(func.count()).select_from(model).where(model.plan_id == plan.id)
        ) or 0)

    return {
        "rounds": count(AnalysisRound),
        "tasks": count(PlanTask),
        "growth": count(GrowthRecord),
    }


def _legacy_latest_round(session, plan) -> dict | None:
    loop, _ = _legacy_parts(session, plan)
    rounds = loop.get("rounds") if isinstance(loop.get("rounds"), list) else []
    best = None
    for entry in rounds:
        if not isinstance(entry, dict):
            continue
        try:
            no = int(entry.get("round") or 0)
        except (TypeError, ValueError):
            continue
        if best is None or no >= best[0]:
            best = (no, entry)
    if best is None:
        return None
    no, entry = best
    return {
        "id": f"legacy:{plan.id}:{no}",
        "round_no": no,
        "kind": entry.get("source") or "legacy",
        "status": "succeeded",
        "created_at": entry.get("created_at"),
        "finished_at": entry.get("completed_at") or entry.get("created_at"),
    }


def _latest_round(session, plan) -> dict | None:
    if plan.state == PLAN_STATE_LEGACY:
        return _legacy_latest_round(session, plan)
    row = session.execute(
        select(
            AnalysisRound.id,
            AnalysisRound.round_no,
            AnalysisRound.kind,
            AnalysisRound.status,
            AnalysisRound.created_at,
            AnalysisRound.finished_at,
        )
        .where(AnalysisRound.plan_id == plan.id)
        .order_by(AnalysisRound.round_no.desc())
        .limit(1)
    ).first()
    if row is None:
        return None
    return {
        "id": row.id,
        "round_no": row.round_no,
        "kind": row.kind,
        "status": row.status,
        "created_at": _iso(row.created_at),
        "finished_at": _iso(row.finished_at),
    }


def plan_summary(session, plan: CareerPlan) -> dict:
    return {
        "id": plan.id,
        "title": plan.title or plan.target_job,
        "target_job": plan.target_job,
        "state": plan.state,
        "is_legacy": plan.state == "legacy_readonly",
        "created_at": _iso(plan.created_at),
        "updated_at": _iso(plan.updated_at),
        "goal": plan.goal_json,
        "current_skills": list(plan.current_skills_json or []),
        "latest_round": _latest_round(session, plan),
        "counts": _counts(session, plan),
    }


def plan_detail(session, plan: CareerPlan) -> dict:
    data = plan_summary(session, plan)
    data.update({
        "jd_text": plan.jd_text,
        "profile_snapshot": dict(plan.profile_snapshot_json or {}),
        "initial_skills": plan.initial_skills_json,
        "job_snapshot": dict(plan.job_snapshot_json or {}),
        "next_round_no": plan.next_round_no,
        "active_run_id": plan.active_run_id,
        "version": plan.version,
        "has_active_run": bool(plan.active_run_id),
    })
    return data


#: 列表只需要摘要字段；``jd_text`` / 各类快照 / 旧数据 payload 一律不进分页查询。
_SUMMARY_COLUMNS = (
    CareerPlan.id,
    CareerPlan.title,
    CareerPlan.target_job,
    CareerPlan.state,
    CareerPlan.goal_json,
    CareerPlan.current_skills_json,
    CareerPlan.created_at,
    CareerPlan.updated_at,
)


def list_plans(
    factory: sessionmaker,
    owner_id: int,
    *,
    limit: int = 20,
    before: str | None = None,
) -> dict:
    limit = max(1, min(int(limit), 100))
    with factory() as session:
        total = int(session.scalar(
            select(func.count()).select_from(CareerPlan).where(
                CareerPlan.user_id == int(owner_id)
            )
        ) or 0)

        query = select(*_SUMMARY_COLUMNS).where(CareerPlan.user_id == int(owner_id))
        if before:
            cursor = session.execute(
                select(CareerPlan.updated_at, CareerPlan.id).where(
                    CareerPlan.id == before, CareerPlan.user_id == int(owner_id)
                )
            ).first()
            if cursor is not None:
                query = query.where(
                    or_(
                        CareerPlan.updated_at < cursor.updated_at,
                        (CareerPlan.updated_at == cursor.updated_at)
                        & (CareerPlan.id < cursor.id),
                    )
                )
        rows = session.execute(
            query.order_by(CareerPlan.updated_at.desc(), CareerPlan.id.desc())
            .limit(limit)
        ).all()
        items = [plan_summary(session, row) for row in rows]
        next_before = items[-1]["id"] if len(items) == limit else None
        return {"items": items, "total": total, "limit": limit, "next_before": next_before}


def get_plan(factory: sessionmaker, owner_id: int, plan_id: str) -> dict | None:
    with factory() as session:
        try:
            plan = _owned(session, owner_id, plan_id)
        except PlanNotFound:
            return None
        return plan_detail(session, plan)


def delete_plan(factory: sessionmaker, owner_id: int, plan_id: str) -> dict:
    """删除整份档案并返回删除计数；越权与不存在同样抛 ``PlanNotFound``。"""
    with factory.begin() as session:
        plan = session.scalar(
            select(CareerPlan)
            .where(CareerPlan.id == plan_id, CareerPlan.user_id == int(owner_id))
            .with_for_update()
        )
        if plan is None:
            raise PlanNotFound(plan_id)
        removed = _counts(session, plan)
        title = plan.title or plan.target_job
        session.execute(delete(CareerPlan).where(CareerPlan.id == plan.id))
        return {"plan_id": plan_id, "title": title, "removed": removed}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
