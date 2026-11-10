"""档案 API：创建、列表、越权与级联删除。"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from backend import db
from backend.app import app
from backend.auth import get_current_user
from backend.db import User
from backend.deps import create_archive_factory
from backend.models import AnalysisRound, CareerPlan, GrowthRecord, PlanTask


@pytest.fixture()
def archive(tmp_path):
    url = f"sqlite:///{tmp_path / 'plans.db'}"
    db.init_db(url)
    factory = db.session_factory(url)
    with factory.begin() as session:
        alice = User(username="alice", password_hash="x")
        bob = User(username="bob", password_hash="x")
        session.add_all([alice, bob])
        session.flush()
        ids = {"alice": alice.id, "bob": bob.id}
    original_auth = app.dependency_overrides.get(get_current_user)
    app.dependency_overrides[create_archive_factory] = lambda: factory
    yield factory, ids
    app.dependency_overrides.pop(create_archive_factory, None)
    if original_auth is None:
        app.dependency_overrides.pop(get_current_user, None)
    else:
        app.dependency_overrides[get_current_user] = original_auth


def _login(uid: int) -> None:
    app.dependency_overrides[get_current_user] = lambda: {"id": uid, "username": "u"}


def _payload(key: str | None = None, *, title: str = "后端开发", target: str = "后端工程师",
             jd: str = "熟悉 Python", major: str = "软件工程") -> dict:
    return {
        "request_key": key or uuid.uuid4().hex,
        "title": title,
        "target_job": target,
        "jd_text": jd,
        "user_profile": {
            "major": major, "grade": "大三", "skills": ["Python"], "projects": ["课程项目"],
        },
    }


def test_create_is_idempotent_and_freezes_snapshot(archive):
    factory, ids = archive
    _login(ids["alice"])
    client = TestClient(app)

    first = client.post("/api/plans", json=_payload("key-1"))
    assert first.status_code == 201, first.text
    plan_id = first.json()["plan_id"]

    again = client.post("/api/plans", json=_payload("key-1", title="不同标题"))
    assert again.status_code == 201
    assert again.json()["plan_id"] == plan_id

    with factory() as session:
        plans = session.scalars(select(CareerPlan)).all()
        assert len(plans) == 1
        assert plans[0].profile_snapshot_json["major"] == "软件工程"
        assert plans[0].user_id == ids["alice"]

    # 改基础资料不追溯旧档案
    put = client.put("/api/profile", json={
        "major": "会计", "grade": "大三", "skills": [], "projects": [], "version": 1,
    })
    assert put.status_code == 200, put.text
    detail = client.get(f"/api/plans/{plan_id}")
    assert detail.json()["profile_snapshot"]["major"] == "软件工程"


def test_same_job_different_jd_creates_two_plans(archive):
    factory, ids = archive
    _login(ids["alice"])
    client = TestClient(app)

    a = client.post("/api/plans", json=_payload("a", jd="会 Python"))
    b = client.post("/api/plans", json=_payload("b", jd="会做账"))
    assert a.json()["plan_id"] != b.json()["plan_id"]

    listing = client.get("/api/plans").json()
    assert listing["total"] == 2
    assert {item["id"] for item in listing["items"]} == {
        a.json()["plan_id"], b.json()["plan_id"],
    }


def test_other_user_gets_404_and_plain_404(archive):
    factory, ids = archive
    client = TestClient(app)

    _login(ids["alice"])
    plan_id = client.post("/api/plans", json=_payload()).json()["plan_id"]

    _login(ids["bob"])
    assert client.get(f"/api/plans/{plan_id}").status_code == 404
    assert client.delete(f"/api/plans/{plan_id}").status_code == 404
    assert client.get("/api/plans/not-exist").status_code == 404


def test_delete_cascades_and_keeps_profile_and_other_plans(archive):
    factory, ids = archive
    _login(ids["alice"])
    client = TestClient(app)

    keep_id = client.post("/api/plans", json=_payload("keep")).json()["plan_id"]
    drop_id = client.post("/api/plans", json=_payload("drop")).json()["plan_id"]

    with factory.begin() as session:
        round_id = uuid.uuid4().hex
        session.add(AnalysisRound(
            id=round_id, plan_id=drop_id, request_key="r",
            round_no=1, kind="initial", status="succeeded",
            input_version=0, input_json={}, result_json={}, payload_json={},
        ))
        session.flush()
        session.add_all([
            PlanTask(plan_id=drop_id, id="l1-1", round_id=round_id, task_no=1),
            GrowthRecord(
                id=uuid.uuid4().hex, plan_id=drop_id, round_id=round_id,
                operation_key="op", kind="normal", entry_json={},
            ),
        ])

    deleted = client.delete(f"/api/plans/{drop_id}")
    assert deleted.status_code == 204, deleted.text
    assert deleted.content == b""

    with factory() as session:
        assert session.get(CareerPlan, drop_id) is None
        for model in (AnalysisRound, PlanTask, GrowthRecord):
            assert session.scalar(
                select(func.count()).select_from(model).where(model.plan_id == drop_id)
            ) == 0
        assert session.get(CareerPlan, keep_id) is not None
        assert client.get("/api/profile").json()["major"] == "软件工程"

    assert client.delete(f"/api/plans/{drop_id}").status_code == 404
    assert client.get(f"/api/plans/{drop_id}").status_code == 404


def test_profile_version_conflict(archive):
    factory, ids = archive
    _login(ids["alice"])
    client = TestClient(app)

    body = {"major": "软件工程", "grade": "大三", "skills": [], "projects": [], "version": 0}
    assert client.put("/api/profile", json=body).status_code == 200
    stale = client.put("/api/profile", json={**body, "major": "会计"})
    assert stale.status_code == 409
    assert "刷新" in stale.json()["detail"]


def test_plan_scoped_task_and_growth_routes_are_isolated(archive):
    factory, ids = archive
    _login(ids["alice"])
    client = TestClient(app)
    a_id = client.post("/api/plans", json=_payload("a")).json()["plan_id"]
    b_id = client.post("/api/plans", json=_payload("b", target="会计", jd="会做账")).json()["plan_id"]

    from backend.deps import stores_for_plan
    a = stores_for_plan(factory, ids["alice"], a_id)
    written = a.loop.append_round(
        "后端工程师",
        [{"topic": "动态规划", "covers": ["Python"], "description": "d"}],
        [],
        round_no=1,
    )
    assert written["status"] == "written"
    task_id = a.loop.tasks()[0]["task_id"]
    assert a.loop.mark_completed(task_id, evidence="完成并跑通")["status"] == "completed"

    pending_a = client.get(f"/api/plans/{a_id}/growth/pending").json()
    pending_b = client.get(f"/api/plans/{b_id}/growth/pending").json()
    assert [item["task_id"] for item in pending_a["tasks"]] == [task_id]
    assert pending_b["tasks"] == []
    assert pending_b["events"] == []

    assert a.growth.append({
        "kind": "normal", "round": 1,
        "events": [{"title": "完成项目"}],
        "user_skills_after": [{"skill": "Python", "level": "进阶"}],
        "level_changes": [], "at": "2026-11-06T10:00:00",
    })["status"] == "written"

    tasks_a = client.get(f"/api/plans/{a_id}/tasks").json()
    tasks_b = client.get(f"/api/plans/{b_id}/tasks").json()
    assert tasks_a["pagination"]["total"] == 1
    assert tasks_b["pagination"]["total"] == 0
    assert len(tasks_a["rounds"]) == 1
    assert tasks_b["rounds"] == []

    history_a = client.get(f"/api/plans/{a_id}/growth/history").json()
    history_b = client.get(f"/api/plans/{b_id}/growth/history").json()
    assert len(history_a["history"]) == 1
    assert history_b["history"] == []

    rounds_a = client.get(f"/api/plans/{a_id}/rounds").json()
    assert rounds_a["rounds"][0]["round"] == 1


def test_plan_scoped_routes_reject_other_users_and_deleted_plans(archive):
    factory, ids = archive
    client = TestClient(app)
    _login(ids["alice"])
    plan_id = client.post("/api/plans", json=_payload()).json()["plan_id"]

    _login(ids["bob"])
    assert client.get(f"/api/plans/{plan_id}/tasks").status_code == 404
    assert client.get(f"/api/plans/{plan_id}/growth/pending").status_code == 404
    assert client.get(f"/api/plans/{plan_id}/growth/history").status_code == 404
    assert client.get(f"/api/plans/{plan_id}/rounds").status_code == 404
    assert client.post(
        f"/api/plans/{plan_id}/growth/next-round", json={"request_key": "n"}
    ).status_code == 404

    _login(ids["alice"])
    assert client.delete(f"/api/plans/{plan_id}").status_code == 204
    assert client.get(f"/api/plans/{plan_id}/tasks").status_code == 404
    assert client.post(
        f"/api/plans/{plan_id}/growth/next-round", json={"request_key": "n"}
    ).status_code == 404
