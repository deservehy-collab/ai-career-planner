"""六表底座、基础资料与级联删除的隔离测试。

只用临时 SQLite 文件：模型与 SQL 由 SQLAlchemy 生成，生产可通过
``CAREER_DB_URL`` 指向 MySQL。测试不读项目 ``runs/``，不调用模型。
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, inspect, select

from backend import db, profile_service
from backend.db import User
from backend.models import AnalysisRound, CareerPlan, GrowthRecord, PlanTask, UserProfile


@pytest.fixture()
def db_url(tmp_path):
    return f"sqlite:///{tmp_path / 'archive.db'}"


@pytest.fixture()
def factory(db_url):
    db.init_db(db_url)
    return db.session_factory(db_url)


def _make_user(factory, username: str = "alice") -> int:
    with factory.begin() as session:
        user = User(username=username, password_hash="x")
        session.add(user)
        session.flush()
        return user.id


def test_init_db_creates_six_tables_and_is_idempotent(factory, db_url):
    db.init_db(db_url)
    names = set(inspect(db.engine_for(db_url)).get_table_names())
    assert {
        "users", "user_profiles", "career_plans",
        "analysis_rounds", "tasks", "growth_records",
    } <= names


def test_profile_defaults_and_optimistic_version(factory):
    uid = _make_user(factory, "bob")

    empty = profile_service.get_profile(factory, uid)
    assert empty == {"major": "", "grade": "", "skills": [], "projects": [], "version": 0}

    first = profile_service.put_profile(
        factory, uid,
        {"major": "软件工程", "grade": "大三", "skills": ["Python"], "projects": []},
        expected_version=0,
    )
    assert first["version"] == 1
    assert profile_service.get_profile(factory, uid)["skills"] == ["Python"]

    with pytest.raises(profile_service.VersionConflict):
        profile_service.put_profile(
            factory, uid,
            {"major": "会计", "grade": "大三", "skills": [], "projects": []},
            expected_version=0,
        )

    second = profile_service.put_profile(
        factory, uid,
        {"major": "会计", "grade": "大三", "skills": [], "projects": []},
        expected_version=1,
    )
    assert second["version"] == 2
    assert profile_service.get_profile(factory, uid)["major"] == "会计"


def test_deleting_plan_cascades_only_its_children(factory):
    uid = _make_user(factory)
    with factory.begin() as session:
        profile = UserProfile(
            user_id=uid, major="软件工程", grade="大三",
            skills_json=["Python"], projects_json=[], version=1,
        )
        session.add(profile)
        keep = CareerPlan(
            id=uuid.uuid4().hex, user_id=uid, request_key="keep",
            title="保留", target_job="后端", state="draft",
            profile_snapshot_json={"major": "软件工程"},
            current_skills_json=[], next_round_no=1, version=0,
        )
        drop = CareerPlan(
            id=uuid.uuid4().hex, user_id=uid, request_key="drop",
            title="删除", target_job="前端", state="draft",
            profile_snapshot_json={"major": "软件工程"},
            current_skills_json=[], next_round_no=1, version=0,
        )
        session.add_all([keep, drop])
        session.flush()

        round_id = uuid.uuid4().hex
        session.add(AnalysisRound(
            id=round_id, plan_id=drop.id, request_key="r1",
            round_no=1, kind="initial", status="succeeded",
            input_version=0, input_json={}, result_json={}, payload_json={},
        ))
        session.flush()
        session.add_all([
            PlanTask(
                plan_id=drop.id, id="l1-1", round_id=round_id, task_no=1,
                kind="learning", title="任务", skills_json=[],
                status="pending", version=0,
            ),
            GrowthRecord(
                id=uuid.uuid4().hex, plan_id=drop.id, round_id=round_id,
                operation_key="op-1", kind="normal", entry_json={},
            ),
        ])
        drop_id = drop.id

    with factory.begin() as session:
        plan = session.get(CareerPlan, drop_id)
        assert plan is not None
        session.delete(plan)

    with factory() as session:
        assert session.get(CareerPlan, drop_id) is None
        assert session.scalar(
            select(func.count()).select_from(AnalysisRound).where(AnalysisRound.plan_id == drop_id)
        ) == 0
        assert session.scalar(
            select(func.count()).select_from(PlanTask).where(PlanTask.plan_id == drop_id)
        ) == 0
        assert session.scalar(
            select(func.count()).select_from(GrowthRecord).where(GrowthRecord.plan_id == drop_id)
        ) == 0
        assert session.get(User, uid) is not None
        assert session.get(UserProfile, uid) is not None
        assert session.get(CareerPlan, keep.id) is not None
