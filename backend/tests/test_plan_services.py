"""服务层档案隔离：分析、待办与下一轮都只碰当前档案。

这里不测 HTTP，也不跑模型：直接注入 ``Stores``，把工作流换成假的返回值，
验证 ``run_analyze_and_save`` / ``pending_growth_events`` / ``_run_next_round``
在 SQL 档案存储上的作用域。
"""

from __future__ import annotations

import uuid
from unittest import mock

import pytest
from sqlalchemy import event, func, select

from agents import growth_agent
from agents.growth_loop import event_fingerprint, pending_growth_events
from backend import db
from backend.db import User
from backend.models import AnalysisRound, CareerPlan, GrowthRecord, PlanTask
from backend.plan_repository import delete_plan, list_plans
from backend.plan_stores import SqlGrowthStore, SqlLoopStore
from backend.schemas import AnalyzeRequest
from backend.services import Stores, _run_next_round, list_pending, run_analyze_and_save

PROFILE_A = {
    "major": "软件工程", "grade": "大三", "skills": ["Python"], "projects": [],
}
PROFILE_B = {
    "major": "会计学", "grade": "大二", "skills": ["Excel"], "projects": [],
}


@pytest.fixture()
def archive(tmp_path):
    url = f"sqlite:///{tmp_path / 'services.db'}"
    db.init_db(url)
    factory = db.session_factory(url)
    with factory.begin() as session:
        user = User(username="alice", password_hash="x")
        session.add(user)
        session.flush()
        uid = user.id
        plans = {}
        for key, profile, target, jd in (
            ("A", PROFILE_A, "后端工程师", "JD-A：熟悉 Python"),
            ("B", PROFILE_B, "会计", "JD-B：熟悉 Excel"),
        ):
            plan = CareerPlan(
                id=uuid.uuid4().hex, user_id=uid, request_key=key,
                title=key, target_job=target, jd_text=jd, state="draft",
                profile_snapshot_json=dict(profile),
                current_skills_json=[], next_round_no=1, version=0,
            )
            session.add(plan)
            plans[key] = plan.id
    return factory, uid, plans


def _stores(factory, uid, plan_id) -> Stores:
    with factory() as session:
        plan = session.get(CareerPlan, plan_id)
        assert plan is not None
        return Stores(
            growth=SqlGrowthStore(factory, uid, plan_id),
            loop=SqlLoopStore(factory, uid, plan_id),
            plan_id=plan_id,
            profile_snapshot=dict(plan.profile_snapshot_json or {}),
            target_job=plan.target_job,
            jd_text=plan.jd_text,
        )


def _learning_plan(topic="动态规划"):
    return [{
        "topic": topic, "covers": ["Python"], "description": "学习",
        "stage": 1, "duration": "1周", "prerequisites": [],
        "method": "学习", "assign": "自己", "assessment": "可实现", "goal": "掌握",
        "status_v2": "pending", "resources": [], "priority": "高",
        "milestones": [], "deliverables": [],
    }]


def _seed_round(stores, target="后端工程师"):
    written = stores.loop.append_round(target, _learning_plan(), [], round_no=1)
    assert written["status"] == "written", written
    return stores.loop.tasks()[0]


def _base_state(diff=None, with_plan=True):
    return {
        "user_skills": [{"skill": "Python", "level": "进阶", "evidence": ""}],
        "skill_updates": [{"skill": "Python", "from": "基础", "to": "进阶"}],
        "growth_diff": {
            "history_written": True,
            "history_write_status": "ok",
            "outcome": "upgraded",
            "events_received": 1,
            "duplicates": [],
            "skipped": [],
            "invalid_events": [],
            "level_changes": [{"skill": "Python", "from": "基础", "to": "进阶"}],
            "warnings": [],
            "blocked": [],
            **(diff or {}),
        },
        "learning_plan": _learning_plan("算法") if with_plan else [],
        "project_plan": [],
        "job_requirements": [],
        "skill_gaps": [],
    }


def test_initial_analysis_uses_frozen_snapshot_and_only_touches_plan_a(archive):
    factory, uid, plans = archive
    a, b = _stores(factory, uid, plans["A"]), _stores(factory, uid, plans["B"])
    seen = {}

    def fake_workflow(payload, **kwargs):
        seen["target_job"] = payload.target_job
        seen["jd_text"] = payload.jd_text
        seen["profile"] = payload.user_profile.model_dump()
        return {
            "learning_plan": _learning_plan("Python 后端"),
            "project_plan": [],
            "skill_updates": [],
            "final_plan": {"goal": {}},
        }

    payload = AnalyzeRequest(
        user_profile={"major": "请求里的专业", "skills": ["请求里的技能"]},
        target_job="请求里的另一个岗位",
        jd_text="请求里的另一段 JD",
    )
    with mock.patch("backend.analyze.run_workflow", side_effect=fake_workflow):
        result = run_analyze_and_save(a, payload)

    assert result["round_created"] is True
    assert seen["target_job"] == "后端工程师"
    assert seen["jd_text"] == "JD-A：熟悉 Python"
    assert seen["profile"]["major"] == "软件工程"
    assert seen["profile"]["skills"] == ["Python"]

    rounds_a = a.loop.load().data["rounds"]
    assert len(rounds_a) == 1
    assert rounds_a[0]["target_job"] == "后端工程师"
    assert len(a.loop.tasks()) == 1
    assert b.loop.load().data["rounds"] == []
    assert b.loop.tasks() == []


def test_pending_events_do_not_cross_plans(archive):
    factory, uid, plans = archive
    a, b = _stores(factory, uid, plans["A"]), _stores(factory, uid, plans["B"])
    task = _seed_round(a)
    completed = a.loop.mark_completed(task["task_id"], evidence="完成动态规划专题并跑通")
    assert completed["status"] == "completed"

    pending_a = list_pending(a)
    pending_b = list_pending(b)
    assert [item["task_id"] for item in pending_a["tasks"]] == [task["task_id"]]
    assert pending_b["tasks"] == []
    assert pending_b["events"] == []


def test_next_round_writes_only_plan_a_with_frozen_profile(archive):
    factory, uid, plans = archive
    a, b = _stores(factory, uid, plans["A"]), _stores(factory, uid, plans["B"])
    task = _seed_round(a)
    a.loop.mark_completed(task["task_id"], evidence="完成并跑通")
    seen = {}

    def fake_invoke(state):
        seen["profile"] = state["user_profile"]
        store = growth_agent.get_store()
        fp = event_fingerprint("learning", "动态规划", ["Python"])
        history = store.load().data.get("history") or []
        seen_fp = {event.get("fingerprint") for entry in history
                   for event in entry.get("events") or []}
        if fp not in seen_fp:
            assert store.append({
                "version": "v1",
                "events": [{"type": "learning", "title": "动态规划",
                            "skills": ["Python"], "evidence": "证据", "fingerprint": fp}],
                "user_skills_after": [{"skill": "Python", "level": "进阶"}],
            })["status"] == "written"
        return _base_state()

    with mock.patch("backend.services.workflow.invoke", side_effect=fake_invoke):
        result = _run_next_round(a, user_profile={"major": "后来改过的资料"})

    assert result["round_created"] is True
    assert seen["profile"]["major"] == "软件工程"

    assert len(a.loop.load().data["rounds"]) == 2
    assert len(a.growth.load().data["history"]) == 1
    assert b.loop.load().data["rounds"] == []
    assert b.growth.load().data["history"] == []
    assert b.growth.seen_fingerprints() == set()

    with factory() as session:
        assert session.scalar(
            select(func.count()).select_from(AnalysisRound).where(
                AnalysisRound.plan_id == plans["B"]
            )
        ) == 0
        assert session.scalar(
            select(func.count()).select_from(GrowthRecord).where(
                GrowthRecord.plan_id == plans["B"]
            )
        ) == 0


def test_deleted_plan_discards_late_next_round(archive):
    factory, uid, plans = archive
    a = _stores(factory, uid, plans["A"])
    task = _seed_round(a)
    a.loop.mark_completed(task["task_id"], evidence="完成并跑通")

    def fake_invoke(_state):
        # 模拟分析期间用户删掉了整份档案。
        delete_plan(factory, uid, plans["A"])
        return _base_state()

    with mock.patch("backend.services.workflow.invoke", side_effect=fake_invoke):
        result = _run_next_round(a)

    assert result["round_created"] is False
    with factory() as session:
        assert session.get(CareerPlan, plans["A"]) is None
        for model in (AnalysisRound, PlanTask, GrowthRecord):
            assert session.scalar(
                select(func.count()).select_from(model).where(
                    model.plan_id == plans["A"]
                )
            ) == 0


def test_list_plans_does_not_select_large_columns(archive):
    """分页只取摘要字段：JD 正文、资料快照与旧数据 payload 不随列表下发。"""
    factory, uid, _plans = archive
    statements: list[str] = []

    def capture(_conn, _cursor, statement, _params, _context, _executemany):
        if "FROM career_plans" in statement:
            statements.append(statement)

    engine = factory.kw["bind"]
    event.listen(engine, "before_cursor_execute", capture)
    try:
        result = list_plans(factory, uid)
    finally:
        event.remove(engine, "before_cursor_execute", capture)

    assert result["total"] == 2
    page = next(s for s in statements if "ORDER BY career_plans.updated_at DESC" in s)
    for column in ("jd_text", "profile_snapshot_json", "legacy_payload_json",
                   "job_snapshot_json", "pending_next_round_json"):
        assert column not in page
