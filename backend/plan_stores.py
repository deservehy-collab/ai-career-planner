"""把档案级读写接到既有的 LoopStore / GrowthStore 算法上。

不重写成长规则：子类只替换 ``load()`` 与 ``_write()``，把原来 ``loop.json`` /
``growth.json`` 的内容物化成原有的 dict 结构，底层落盘换成六张表。
因此 ``complete_task``、``pending_growth_events``、``growth_node`` 等既有
确定性逻辑可以原样复用，同时天然按 ``plan_id`` 隔离。

删除竞态
--------
每次写入都在事务里重新锁定父档案；档案已删除就抛 :class:`PlanGone`，
调用方不会因为手里拿着旧对象而把整份档案重建回来。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path

from sqlalchemy import delete, select, update
from sqlalchemy.orm import sessionmaker

from agents.growth_store import (
    STATUS_EMPTY,
    STATUS_UNREADABLE,
    GrowthStore,
    LoadResult as GrowthLoadResult,
    _empty_data as _growth_empty,
)
from agents.loop_store import (
    STATUS_EMPTY as LOOP_EMPTY,
    STATUS_UNREADABLE as LOOP_UNREADABLE,
    LoopStore,
    LoadResult as LoopLoadResult,
    _empty_data as _loop_empty,
)
from backend.models import (
    PLAN_STATE_LEGACY,
    AnalysisRound,
    CareerPlan,
    GrowthRecord,
    PlanTask,
)

__all__ = ["PlanGone", "SqlLoopStore", "SqlGrowthStore"]


class PlanGone(RuntimeError):
    """档案已删除或不可写；迟到结果必须丢弃。"""


def _parse_dt(value, fallback: datetime | None = None) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return fallback
    return fallback


def _iso(value) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else (value or None)


class _PlanStoreMixin:
    db_backed = True

    def __init__(self, factory: sessionmaker, owner_id: int, plan_id: str):
        from backend import config

        self.factory = factory
        self.owner_id = int(owner_id)
        self.plan_id = str(plan_id)
        # 岗位画像缓存仍按用户目录隔离；它不是业务真相，随旧数据一起可清理。
        self.directory = config.user_data_dir(self.owner_id) / "plans" / self.plan_id
        self.filename = self._filename
        self.user_id = f"plan_{self.plan_id}"

    @property
    def path(self) -> Path:
        # 仅用于健康检查/日志展示；真正状态在数据库里。
        return self.directory / self.filename

    def _plan(self, session) -> CareerPlan | None:
        return session.scalar(
            select(CareerPlan).where(
                CareerPlan.id == self.plan_id,
                CareerPlan.user_id == self.owner_id,
            )
        )

    def _lock_plan(self, session) -> CareerPlan:
        plan = session.scalar(
            select(CareerPlan)
            .where(
                CareerPlan.id == self.plan_id,
                CareerPlan.user_id == self.owner_id,
            )
            .with_for_update()
        )
        if plan is None:
            raise PlanGone("这份档案已被删除，迟到结果已丢弃")
        if plan.state == PLAN_STATE_LEGACY:
            raise PlanGone("旧数据档案是只读的，不能写入")
        return plan


class SqlLoopStore(_PlanStoreMixin, LoopStore):
    _filename = "loop.json"

    def load(self) -> LoopLoadResult:
        with self.factory() as session:
            plan = self._plan(session)
        if plan is None:
            return LoopLoadResult(
                data=_loop_empty(self.user_id),
                status=LOOP_UNREADABLE,
                warning="这份档案已被删除",
                path=self.path,
            )

        data = _loop_empty(self.user_id)
        data["created_at"] = _iso(plan.created_at)
        data["updated_at"] = _iso(plan.updated_at)

        if plan.state == PLAN_STATE_LEGACY:
            legacy = (plan.legacy_payload_json or {}).get("loop")
            if isinstance(legacy, dict):
                data.update(legacy)
            return LoopLoadResult(data=data, status=STATUS_EMPTY, path=self.path)

        with self.factory() as session:
            rows = session.scalars(
                select(AnalysisRound)
                .where(AnalysisRound.plan_id == self.plan_id)
                .order_by(AnalysisRound.round_no)
            ).all()
            tasks = session.scalars(
                select(PlanTask)
                .where(PlanTask.plan_id == self.plan_id)
                .order_by(PlanTask.task_no, PlanTask.id)
            ).all()

        by_round: dict[str, list[dict]] = {}
        for task in tasks:
            by_round.setdefault(task.round_id or "", []).append({
                "task_id": task.id,
                "kind": task.kind,
                "round": next(
                    (row.round_no for row in rows if row.id == task.round_id), 0
                ),
                "index": task.task_no,
                "title": task.title,
                "skills": list(task.skills_json or []),
                "status": task.status,
                "created_at": _iso(task.created_at),
                "completed_at": _iso(task.completed_at),
                "evidence": (task.evidence_json or {}).get("text", ""),
                "evidence_kind": (task.evidence_json or {}).get("kind", ""),
                "note": (task.evidence_json or {}).get("note", ""),
            })

        rounds: list[dict] = []
        for row in rows:
            entry = dict(row.payload_json or {})
            entry.setdefault("round", row.round_no)
            entry.setdefault("created_at", _iso(row.created_at))
            entry.setdefault("target_job", plan.target_job)
            entry.setdefault("learning_plan", [])
            entry.setdefault("project_plan", [])
            entry["tasks"] = by_round.get(row.id, [])
            rounds.append(entry)

        data["rounds"] = rounds
        if plan.goal_json:
            data["latest_goal"] = plan.goal_json
        if plan.pending_next_round_json:
            data["pending_next_round"] = plan.pending_next_round_json
        return LoopLoadResult(data=data, status=STATUS_EMPTY, path=self.path)

    def _write(self, data: dict) -> None:
        now = datetime.now()
        with self.factory.begin() as session:
            plan = self._lock_plan(session)
            rounds = [item for item in (data.get("rounds") or []) if isinstance(item, dict)]
            existing = {
                row.round_no: row
                for row in session.scalars(
                    select(AnalysisRound).where(AnalysisRound.plan_id == self.plan_id)
                )
            }
            desired = {int(item.get("round") or 0) for item in rounds}

            removed_ids = [row.id for no, row in existing.items() if no not in desired]
            if removed_ids:
                # 清任务/轮次不能带走已入账成长：先解除引用，再删轮次。
                session.execute(
                    update(GrowthRecord)
                    .where(
                        GrowthRecord.plan_id == self.plan_id,
                        GrowthRecord.round_id.in_(removed_ids),
                    )
                    .values(round_id=None)
                )

            session.execute(delete(PlanTask).where(PlanTask.plan_id == self.plan_id))

            for entry in rounds:
                no = int(entry.get("round") or 0)
                if no <= 0:
                    raise ValueError("轮次号必须是正整数")
                payload = {key: value for key, value in entry.items() if key != "tasks"}
                row = existing.get(no)
                if row is None:
                    row = AnalysisRound(
                        id=str(entry.get("run_id") or uuid.uuid4().hex),
                        plan_id=self.plan_id,
                        request_key=str(entry.get("request_key") or f"round:{no}"),
                        round_no=no,
                        kind=str(entry.get("source") or "initial"),
                        status="succeeded",
                        input_version=int(plan.version or 0),
                        input_json={},
                        result_json={},
                        payload_json=payload,
                    )
                    session.add(row)
                    session.flush()
                else:
                    row.payload_json = payload
                    if row.status in ("failed", "interrupted", "queued", "running"):
                        row.status = "succeeded"

                for task in entry.get("tasks") or []:
                    session.add(PlanTask(
                        plan_id=self.plan_id,
                        id=str(task.get("task_id")),
                        round_id=row.id,
                        task_no=int(task.get("index") or 0),
                        kind=str(task.get("kind") or "learning"),
                        source="round",
                        title=str(task.get("title") or "")[:500],
                        skills_json=list(task.get("skills") or []),
                        details_json={},
                        status=str(task.get("status") or "pending"),
                        evidence_json={
                            "text": str(task.get("evidence") or ""),
                            "kind": str(task.get("evidence_kind") or ""),
                            "note": str(task.get("note") or ""),
                        },
                        version=0,
                        created_at=_parse_dt(task.get("created_at"), now),
                        completed_at=_parse_dt(task.get("completed_at")),
                    ))

            for no, row in existing.items():
                if no not in desired:
                    session.delete(row)

            plan.goal_json = data.get("latest_goal")
            plan.pending_next_round_json = data.get("pending_next_round")
            if desired:
                plan.next_round_no = max(int(plan.next_round_no or 1), max(desired) + 1)
            plan.updated_at = now

    # 数据库模式没有“损坏文件备份”概念；显式拒绝即可。
    def backup_corrupt(self, now: datetime | None = None, then_reset: bool = False) -> dict:
        return {"status": "refused", "warning": "档案存储使用数据库，不存在可备份的损坏文件"}

    def initialize(self, now: datetime | None = None) -> dict:
        self._write(_loop_empty(self.user_id))
        return {"status": "written", "path": str(self.path)}

    def clear(self, now: datetime | None = None) -> dict:
        loaded = self.load()
        rounds = loaded.data.get("rounds") or []
        task_count = sum(len(item.get("tasks") or []) for item in rounds)
        self._write(_loop_empty(self.user_id))
        return {
            "status": "written", "warning": None, "path": str(self.path),
            "backup": None, "rounds_removed": len(rounds),
            "tasks_removed": task_count,
        }


class SqlGrowthStore(_PlanStoreMixin, GrowthStore):
    _filename = "growth.json"

    def __init__(self, factory: sessionmaker, owner_id: int, plan_id: str,
                 read_only: bool = False):
        super().__init__(factory, owner_id, plan_id)
        self.read_only = bool(read_only)

    def isolated(self) -> "SqlGrowthStore":
        """只读视图：分析流程照常读历史，但任何成长写入都不会落库。"""
        return SqlGrowthStore(self.factory, self.owner_id, self.plan_id, read_only=True)

    def load(self) -> GrowthLoadResult:
        with self.factory() as session:
            plan = self._plan(session)
            if plan is None:
                return GrowthLoadResult(
                    data=_growth_empty(self.user_id),
                    status=STATUS_UNREADABLE,
                    warning="这份档案已被删除",
                    path=self.path,
                )
            rows = session.scalars(
                select(GrowthRecord)
                .where(GrowthRecord.plan_id == self.plan_id)
                .order_by(GrowthRecord.seq, GrowthRecord.id)
            ).all()

            data = _growth_empty(self.user_id)
            data["created_at"] = _iso(plan.created_at)
            data["updated_at"] = _iso(plan.updated_at)
            if plan.initial_skills_json is not None:
                data["baseline"] = {
                    "source": "user_profile",
                    "at": _iso(plan.created_at),
                    "user_skills": list(plan.initial_skills_json or []),
                }
            data["history"] = [dict(row.entry_json or {}) for row in rows]

            if plan.state == PLAN_STATE_LEGACY:
                legacy = (plan.legacy_payload_json or {}).get("growth")
                if isinstance(legacy, dict):
                    data = legacy
                    data.setdefault("history", [])
            return GrowthLoadResult(data=data, status=STATUS_EMPTY, path=self.path)

    def _write(self, data: dict) -> None:
        if self.read_only:
            return  # 只读分析流程：候选结果只用于本次计算，不落库
        now = datetime.now()
        with self.factory.begin() as session:
            plan = self._lock_plan(session)
            rounds = {
                row.round_no: row.id
                for row in session.scalars(
                    select(AnalysisRound).where(AnalysisRound.plan_id == self.plan_id)
                )
            }
            history = [item for item in (data.get("history") or []) if isinstance(item, dict)]

            session.execute(delete(GrowthRecord).where(GrowthRecord.plan_id == self.plan_id))
            for seq, entry in enumerate(history):
                round_no = entry.get("round")
                round_id = rounds.get(int(round_no)) if isinstance(round_no, int) else None
                session.add(GrowthRecord(
                    id=uuid.uuid4().hex,
                    plan_id=self.plan_id,
                    round_id=round_id,
                    operation_key=str(entry.get("operation_key") or f"h{seq}")[:128],
                    kind=str(entry.get("kind") or "normal"),
                    entry_json=entry,
                    void_of=(str(entry["void_of"])[:128] if entry.get("void_of") else None),
                    seq=seq,
                    created_at=_parse_dt(entry.get("at"), now),
                ))

            baseline = data.get("baseline") or {}
            if plan.initial_skills_json is None and isinstance(baseline.get("user_skills"), list):
                plan.initial_skills_json = list(baseline["user_skills"])
            plan.current_skills_json = self.latest_skills(data)
            plan.updated_at = now

    def backup_corrupt(self, now: datetime | None = None, then_reset: bool = False) -> dict:
        return {"status": "refused", "warning": "档案存储使用数据库，不存在可备份的损坏文件"}
