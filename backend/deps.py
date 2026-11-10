"""FastAPI 依赖注入点。

两个函数都是**可被 ``app.dependency_overrides`` 覆盖**的普通函数 ——
tests/test_growth_*_api.py 正是靠覆盖它们把 store 与任务管理器换成临时实例，
从而全程不触碰项目 ``runs/``。所以路由里必须写
``Depends(create_stores)`` / ``Depends(create_task_manager)``，
不能把实例直接抓在模块级变量上。

档案上下文
----------
业务接口接受可选的 ``plan_id`` 查询参数。**传了**就构造数据库档案级 store，
并校验档案属于当前登录账号；**没传**时保留旧的账号级 JSON 路径，供旧测试与
迁移期只读场景使用。前端所有业务请求都会显式带 ``plan_id``。
"""

from __future__ import annotations

from functools import lru_cache

from fastapi import Depends, HTTPException, Query
from sqlalchemy import select

from backend import config
from backend.auth import get_current_user
from backend.jobs import TaskManager
from backend.models import CareerPlan
from backend.services import Stores

__all__ = [
    "create_stores", "create_task_manager", "create_archive_factory",
    "stores_for_plan",
]


def create_archive_factory():
    """档案/资料接口使用的 Session 工厂。

    可被 ``dependency_overrides`` 替换；生产读 ``CAREER_DB_URL``。
    """
    from backend import db

    return db.session_factory(db.database_url())


def stores_for_plan(factory, user_id: int, plan_id: str) -> Stores:
    """按档案构造数据库 store；不存在/已删除/不是本人一律 404。"""
    from backend.plan_stores import SqlGrowthStore, SqlLoopStore

    with factory() as session:
        plan = session.scalar(
            select(CareerPlan).where(
                CareerPlan.id == plan_id, CareerPlan.user_id == int(user_id)
            )
        )
        if plan is None:
            raise HTTPException(status_code=404, detail="找不到这份档案")
        snapshot = dict(plan.profile_snapshot_json or {})
        target_job = plan.target_job
        jd_text = plan.jd_text
    return Stores(
        growth=SqlGrowthStore(factory, int(user_id), plan_id),
        loop=SqlLoopStore(factory, int(user_id), plan_id),
        plan_id=plan_id,
        profile_snapshot=snapshot,
        target_job=target_job,
        jd_text=jd_text,
    )


def create_stores(
    plan_id: str | None = Query(default=None, max_length=36),
    current: dict = Depends(get_current_user),
) -> Stores:
    """构造当前登录用户的 store。

    ``plan_id`` 非空时走数据库档案边界（轮次/任务/成长只属于该档案）；
    为空时沿用账号级 JSON。档案不存在、已删除或属于别人一律 404。
    """
    user_id = int(current["id"])

    if plan_id:
        from backend import db

        return stores_for_plan(
            db.session_factory(db.database_url()), user_id, plan_id
        )

    from agents.growth_store import GrowthStore
    from agents.loop_store import LoopStore

    directory = config.user_data_dir(user_id)
    return Stores(
        growth=GrowthStore(directory=directory, user_id=f"user_{user_id}"),
        loop=LoopStore(directory=directory, user_id=f"user_{user_id}"),
    )


@lru_cache(maxsize=1)
def _default_task_manager() -> TaskManager:
    """进程级唯一实例。

    必须是单例：``GET /api/runs/{run_id}`` 要能查到 ``POST`` 时提交的那个 run，
    如果每次依赖注入都新建一个 TaskManager，记录就丢了。
    """
    return TaskManager(
        max_workers=config.TASK_WORKERS,
        max_records=config.TASK_RECORD_CAP,
        max_pending=config.TASK_QUEUE_CAP,
    )


def create_task_manager() -> TaskManager:
    return _default_task_manager()
