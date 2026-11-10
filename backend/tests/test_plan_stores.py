"""档案级存储适配：A/B 隔离、清任务保留成长、删除后丢弃迟到写入。"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select

from agents.growth_loop import pending_growth_events
from backend import db
from backend.db import User
from backend.models import CareerPlan, GrowthRecord, PlanTask
from backend.plan_repository import delete_plan
from backend.plan_stores import SqlGrowthStore, SqlLoopStore
from backend.services import Stores


@pytest.fixture()
def archive(tmp_path):
    url = f"sqlite:///{tmp_path / 'store.db'}"
    db.init_db(url)
    factory = db.session_factory(url)
    with factory.begin() as session:
        user = User(username="alice", password_hash="x")
        session.add(user)
        session.flush()
        uid = user.id
        plans = {}
        for key in ("A", "B"):
            plan = CareerPlan(
                id=uuid.uuid4().hex, user_id=uid, request_key=key,
                title=key, target_job=f"{key}岗位", state="draft",
                profile_snapshot_json={"major": "软件工程", "skills": ["Python"]},
                current_skills_json=[], next_round_no=1, version=0,
            )
            session.add(plan)
            plans[key] = plan.id
    return factory, uid, plans


def _stores(factory, uid, plan_id) -> Stores:
    return Stores(
        growth=SqlGrowthStore(factory, uid, plan_id),
        loop=SqlLoopStore(factory, uid, plan_id),
        plan_id=plan_id,
    )


def _seed_round(stores, target="后端工程师"):
    written = stores.loop.append_round(
        target,
        [{"topic": "动态规划", "covers": ["Python"], "description": "d"}],
        [],
        round_no=1,
    )
    assert written["status"] == "written", written
    return stores.loop.tasks()[0]


def test_rounds_and_tasks_do_not_leak_between_plans(archive):
    factory, uid, plans = archive
    a, b = _stores(factory, uid, plans["A"]), _stores(factory, uid, plans["B"])

    task = _seed_round(a)

    assert a.loop.latest_round()["round"] == 1
    assert b.loop.latest_round() is None
    assert b.loop.tasks() == []
    assert a.loop.find_task(task["task_id"])["title"] == "动态规划"

    done = a.loop.mark_completed(task["task_id"], evidence="完成并跑通")
    assert done["status"] == "completed"

    pending_a = pending_growth_events(loop_store=a.loop, growth_store=a.growth)
    pending_b = pending_growth_events(loop_store=b.loop, growth_store=b.growth)
    assert len(pending_a["events"]) == 1
    assert pending_b["events"] == []

    with factory() as session:
        assert session.scalar(
            select(func.count()).select_from(PlanTask).where(PlanTask.plan_id == plans["B"])
        ) == 0


def test_growth_history_is_plan_scoped(archive):
    factory, uid, plans = archive
    a, b = _stores(factory, uid, plans["A"]), _stores(factory, uid, plans["B"])

    entry = {
        "kind": "normal", "round": 1, "events": [{"title": "完成项目"}],
        "user_skills_after": [{"skill": "Python", "level": "working"}],
        "level_changes": [], "at": "2026-11-06T10:00:00",
    }
    assert a.growth.append(entry)["status"] == "written"

    assert len(a.growth.load().data["history"]) == 1
    assert b.growth.load().data["history"] == []
    assert b.growth.latest_skills() == []
    assert b.growth.seen_fingerprints() == set()

    with factory() as session:
        assert session.scalar(
            select(func.count()).select_from(GrowthRecord).where(
                GrowthRecord.plan_id == plans["B"]
            )
        ) == 0


def test_clear_tasks_keeps_booked_growth(archive):
    factory, uid, plans = archive
    a = _stores(factory, uid, plans["A"])
    task = _seed_round(a)
    a.loop.mark_completed(task["task_id"], evidence="完成")

    entry = {
        "kind": "normal", "round": 1, "events": [],
        "user_skills_after": [{"skill": "Python", "level": "working"}],
        "level_changes": [], "at": "2026-11-06T10:00:00",
    }
    a.growth.append(entry)

    cleared = a.loop.clear()
    assert cleared["rounds_removed"] == 1
    assert a.loop.latest_round() is None

    history = a.growth.load().data["history"]
    assert len(history) == 1
    with factory() as session:
        row = session.scalars(
            select(GrowthRecord).where(GrowthRecord.plan_id == plans["A"])
        ).one()
        assert row.round_id is None


def test_deleted_plan_rejects_late_writes(archive):
    factory, uid, plans = archive
    a = _stores(factory, uid, plans["A"])
    _seed_round(a)

    delete_plan(factory, uid, plans["A"])

    late = a.loop.append_round(
        "后端工程师",
        [{"topic": "迟到", "covers": ["Python"]}],
        [],
        round_no=2,
    )
    assert late["status"] != "written"
    refused = a.growth.append({"kind": "normal", "events": [], "user_skills_after": []})
    assert refused["status"] != "written"

    with factory() as session:
        assert session.get(CareerPlan, plans["A"]) is None
        assert session.scalar(
            select(func.count()).select_from(PlanTask).where(PlanTask.plan_id == plans["A"])
        ) == 0
