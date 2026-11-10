"""求职档案六张核心表的 ORM 模型。

``users`` 仍定义在 :mod:`backend.db`（认证层依赖它），这里放其余五表。
所有业务表以 ``plan_id`` 为档案边界：轮次、任务、成长记录只能属于一份
档案，删除档案由数据库外键级联清理，不靠应用逐表猜。

数据库方言由 ``CAREER_DB_URL`` 决定；模型本身不依赖 MySQL 专有类型，
测试可在临时 SQLite 上跑，生产指向 MySQL 8。
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from backend.db import Base

__all__ = ["UserProfile", "CareerPlan", "AnalysisRound", "PlanTask", "GrowthRecord"]

PLAN_STATE_DRAFT = "draft"
PLAN_STATE_READY = "ready"
PLAN_STATE_LEGACY = "legacy_readonly"

ROUND_STATUS_QUEUED = "queued"
ROUND_STATUS_RUNNING = "running"
ROUND_STATUS_COMPUTED = "computed"
ROUND_STATUS_SUCCEEDED = "succeeded"
ROUND_STATUS_FAILED = "failed"
ROUND_STATUS_INTERRUPTED = "interrupted"

TASK_PENDING = "pending"
TASK_COMPLETED = "completed"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _table_args_mysql() -> dict:
    return {
        "mysql_engine": "InnoDB",
        "mysql_charset": "utf8mb4",
        "mysql_collate": "utf8mb4_unicode_ci",
    }


class UserProfile(Base):
    __tablename__ = "user_profiles"
    __table_args__ = (_table_args_mysql(),)

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    major: Mapped[str] = mapped_column(String(64), default="")
    grade: Mapped[str] = mapped_column(String(32), default="")
    skills_json: Mapped[list] = mapped_column(JSON, default=list)
    projects_json: Mapped[list] = mapped_column(JSON, default=list)
    version: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, onupdate=_utc_now
    )
    legacy_receipt_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class CareerPlan(Base):
    __tablename__ = "career_plans"
    __table_args__ = (
        UniqueConstraint("user_id", "request_key", name="uq_plans_owner_request"),
        Index("ix_plans_owner_updated", "user_id", "updated_at", "id"),
        _table_args_mysql(),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    request_key: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(200), default="")
    target_job: Mapped[str] = mapped_column(String(200))
    jd_text: Mapped[str] = mapped_column(Text, default="")
    profile_snapshot_json: Mapped[dict] = mapped_column(JSON, default=dict)
    initial_skills_json: Mapped[list | None] = mapped_column(JSON, nullable=True)
    current_skills_json: Mapped[list] = mapped_column(JSON, default=list)
    job_snapshot_json: Mapped[dict] = mapped_column(JSON, default=dict)
    goal_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    pending_next_round_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=0)
    next_round_no: Mapped[int] = mapped_column(Integer, default=1)
    active_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    state: Mapped[str] = mapped_column(String(20), default=PLAN_STATE_DRAFT)
    legacy_payload_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, onupdate=_utc_now
    )


class AnalysisRound(Base):
    __tablename__ = "analysis_rounds"
    __table_args__ = (
        UniqueConstraint("plan_id", "round_no", name="uq_rounds_plan_no"),
        UniqueConstraint("plan_id", "request_key", name="uq_rounds_plan_request"),
        UniqueConstraint("id", "plan_id", name="uq_rounds_id_plan"),
        Index("ix_rounds_status_created", "status", "created_at"),
        _table_args_mysql(),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    plan_id: Mapped[str] = mapped_column(
        ForeignKey("career_plans.id", ondelete="CASCADE"), index=True
    )
    request_key: Mapped[str] = mapped_column(String(64))
    round_no: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(20), default="initial")
    status: Mapped[str] = mapped_column(String(20), default=ROUND_STATUS_QUEUED)
    attempt_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    input_version: Mapped[int] = mapped_column(Integer, default=0)
    input_json: Mapped[dict] = mapped_column(JSON, default=dict)
    result_json: Mapped[dict] = mapped_column(JSON, default=dict)
    payload_json: Mapped[dict] = mapped_column(JSON, default=dict)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PlanTask(Base):
    __tablename__ = "tasks"
    __table_args__ = (
        ForeignKeyConstraint(
            ["round_id", "plan_id"],
            ["analysis_rounds.id", "analysis_rounds.plan_id"],
            ondelete="CASCADE",
            name="fk_tasks_round_plan",
        ),
        Index("ix_tasks_plan_status", "plan_id", "status", "id"),
        Index("ix_tasks_plan_round", "plan_id", "round_id", "id"),
        _table_args_mysql(),
    )

    plan_id: Mapped[str] = mapped_column(
        ForeignKey("career_plans.id", ondelete="CASCADE"), primary_key=True
    )
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    round_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    task_no: Mapped[int] = mapped_column(Integer, default=0)
    kind: Mapped[str] = mapped_column(String(20), default="learning")
    source: Mapped[str] = mapped_column(String(20), default="round")
    title: Mapped[str] = mapped_column(String(500), default="")
    skills_json: Mapped[list] = mapped_column(JSON, default=list)
    details_json: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(20), default=TASK_PENDING)
    evidence_json: Mapped[dict] = mapped_column(JSON, default=dict)
    version: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class GrowthRecord(Base):
    __tablename__ = "growth_records"
    __table_args__ = (
        ForeignKeyConstraint(
            ["round_id", "plan_id"],
            ["analysis_rounds.id", "analysis_rounds.plan_id"],
            ondelete="CASCADE",
            name="fk_growth_round_plan",
        ),
        UniqueConstraint("plan_id", "operation_key", name="uq_growth_plan_operation"),
        UniqueConstraint("plan_id", "void_of", name="uq_growth_plan_void"),
        Index("ix_growth_plan_seq", "plan_id", "seq"),
        _table_args_mysql(),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    plan_id: Mapped[str] = mapped_column(
        ForeignKey("career_plans.id", ondelete="CASCADE"), index=True
    )
    round_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    operation_key: Mapped[str] = mapped_column(String(128))
    kind: Mapped[str] = mapped_column(String(20), default="normal")
    entry_json: Mapped[dict] = mapped_column(JSON, default=dict)
    void_of: Mapped[str | None] = mapped_column(String(128), nullable=True)
    seq: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)
