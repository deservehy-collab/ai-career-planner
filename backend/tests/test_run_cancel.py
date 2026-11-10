"""单次分析可中止：保留档案，迟到结果不许写回。

为什么需要这一层
----------------
模型调用无法安全中断，所以"停止分析"真正要保证的不是立刻掐断线程，而是：

1. 排队中的任务不再执行；
2. 正在跑的任务**跑完也不落盘**（轮次/任务/成长都不许新增）；
3. 运行记录变成终态（failed + canceled），前端不用继续等；
4. 档案本身原样保留 —— 只有用户明确选择「停止并删除」时才删。

这里锁住的就是这四条。接口层另测归属与"已经跑完"的竞态。
"""

from __future__ import annotations

import tempfile
import threading
import time
import uuid
from unittest import mock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from agents.growth_store import GrowthStore
from agents.loop_store import LoopStore
from backend import db, services
from backend.app import app
from backend.auth import get_current_user
from backend.db import User
from backend.deps import create_archive_factory, create_task_manager
from backend.jobs import STATUS_FAILED, TaskManager, current_run_canceled
from backend.models import CareerPlan
from backend.schemas import AnalyzeRequest, UserProfileRequest
from backend.services import Stores


def _wait_until(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("等待超时")


class TestTaskManagerCancel:
    """管理器本身：取消后记录终止、迟到结果丢弃。"""

    def test_cancel_running_run_marks_failed_and_discards_late_result(self):
        manager = TaskManager(max_workers=1)
        started = threading.Event()
        release = threading.Event()
        writes: list[str] = []

        def slow_job():
            started.set()
            release.wait(5)
            writes.append("late")
            return {"ok": True}

        run_id = manager.submit(slow_job, kind="initial", owner=7, plan_id="p1")
        try:
            assert started.wait(5)
            outcome = manager.cancel_run(run_id, plan_id="p1", owner=7)
            assert outcome["canceled"] is True

            snapshot = manager.snapshot(run_id)
            assert snapshot["status"] == STATUS_FAILED
            assert "停止" in snapshot["error"]

            release.set()
            _wait_until(lambda: writes == ["late"])
            # 记录一旦被取消，执行完也不许再改回 succeeded
            assert manager.snapshot(run_id)["status"] == STATUS_FAILED
        finally:
            release.set()
            manager.shutdown()

    def test_cancel_running_run_reports_canceled_to_the_job(self):
        manager = TaskManager(max_workers=1)
        started = threading.Event()
        release = threading.Event()
        observed: list[bool] = []

        def slow_job():
            started.set()
            release.wait(5)
            observed.append(current_run_canceled())
            return {}

        run_id = manager.submit(slow_job, kind="initial", owner=7, plan_id="p1")
        try:
            assert started.wait(5)
            manager.cancel_run(run_id, plan_id="p1", owner=7)
            release.set()
            _wait_until(lambda: observed)
            assert observed == [True], "执行中的任务必须能读到自己被取消"
        finally:
            release.set()
            manager.shutdown()

    def test_cancel_queued_run_never_executes(self):
        manager = TaskManager(max_workers=1)
        first_started = threading.Event()
        release_first = threading.Event()
        second_ran = threading.Event()

        def first_job():
            first_started.set()
            release_first.wait(5)
            return {}

        def second_job():
            second_ran.set()
            return {}

        first_id = manager.submit(first_job, kind="initial", owner=7, plan_id="p1")
        second_id = manager.submit(second_job, kind="initial", owner=7, plan_id="p1")
        try:
            assert first_started.wait(5)
            assert manager.cancel_run(second_id, plan_id="p1", owner=7)["canceled"] is True
            release_first.set()
            _wait_until(lambda: manager.get(first_id).finished_at is not None)
            time.sleep(0.05)
            assert not second_ran.is_set(), "排队中被取消的任务不得再执行"
            assert manager.snapshot(second_id)["status"] == STATUS_FAILED
        finally:
            release_first.set()
            manager.shutdown()

    def test_cancel_finished_or_unknown_run_is_refused(self):
        manager = TaskManager(max_workers=1)
        run_id = manager.submit(lambda: {}, kind="initial", owner=7, plan_id="p1")
        try:
            _wait_until(lambda: manager.get(run_id).done)
            finished = manager.cancel_run(run_id, plan_id="p1", owner=7)
            assert finished["canceled"] is False
            assert finished["reason"] == "finished"

            unknown = manager.cancel_run("nope", plan_id="p1", owner=7)
            assert unknown["canceled"] is False
            assert unknown["reason"] == "not_found"

            other_plan = manager.cancel_run(run_id, plan_id="p2", owner=7)
            assert other_plan["reason"] == "not_found"
        finally:
            manager.shutdown()


class TestCanceledAnalysisDoesNotWrite:
    """业务层：取消发生在推理之后、落盘之前，也一个字都不许写。"""

    FAKE_RESULT = {
        "learning_plan": [{"topic": "新知识点", "covers": ["Python"], "goal": "掌握"}],
        "project_plan": [],
        "growth_diff": {"outcome": "upgraded", "history_written": True},
        "user_skills": [],
        "skill_updates": [],
    }

    def _payload(self):
        return AnalyzeRequest(
            user_profile=UserProfileRequest(
                major="计算机", grade="大三", skills=["Python"], projects=[]
            ),
            target_job="软件工程师",
        )

    def test_cancel_during_inference_skips_round_and_profile_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            loop = LoopStore(directory=tmp)
            growth = GrowthStore(directory=tmp)
            stores = Stores(growth=growth, loop=loop)
            manager = TaskManager(max_workers=1)
            invoked = threading.Event()
            release = threading.Event()

            def slow_stream(state, stream_mode=None):
                # 后台运行改走流式（后端要回报节点进度），所以"推理中"这个
                # 阻塞点落在 stream 上，而不是 invoke。
                invoked.set()
                release.wait(5)
                yield ("values", dict(self.FAKE_RESULT))

            with mock.patch.object(services.workflow, "stream", side_effect=slow_stream):
                run_id = manager.submit(
                    services.run_analyze_and_save, stores, self._payload(),
                    kind="initial", owner=9, plan_id="p1",
                )
                try:
                    assert invoked.wait(5)
                    assert manager.cancel_run(run_id, plan_id="p1", owner=9)["canceled"] is True
                    release.set()
                    _wait_until(lambda: manager.get(run_id).done)

                    assert loop.latest_round() is None, "取消后不得写出轮次"
                    assert not growth.path.exists(), "取消后不得写成长文件"
                    assert manager.snapshot(run_id)["status"] == STATUS_FAILED
                finally:
                    release.set()
                    manager.shutdown()


class TestCancelEndpoint:
    """HTTP 边界：归属、已完成竞态、未知 run。"""

    @pytest.fixture()
    def archive(self, tmp_path):
        url = f"sqlite:///{tmp_path / 'plans.db'}"
        db.init_db(url)
        factory = db.session_factory(url)
        with factory.begin() as session:
            alice = User(username="alice", password_hash="x")
            bob = User(username="bob", password_hash="x")
            session.add_all([alice, bob])
            session.flush()
            ids = {"alice": alice.id, "bob": bob.id}
        app.dependency_overrides[create_archive_factory] = lambda: factory
        yield factory, ids
        app.dependency_overrides.pop(create_archive_factory, None)

    def _login(self, uid: int) -> None:
        app.dependency_overrides[get_current_user] = lambda: {"id": uid, "username": "u"}

    def _plan(self, uid: int) -> str:
        self._login(uid)
        response = TestClient(app).post("/api/plans", json={
            "request_key": uuid.uuid4().hex,
            "title": "后端开发",
            "target_job": "后端工程师",
            "jd_text": "熟悉 Python",
            "user_profile": {
                "major": "软件工程", "grade": "大三",
                "skills": ["Python"], "projects": ["课程项目"],
            },
        })
        assert response.status_code == 201, response.text
        return response.json()["plan_id"]

    def test_cancel_endpoint_stops_run_and_keeps_the_plan(self, archive):
        factory, ids = archive
        plan_id = self._plan(ids["alice"])
        manager = TaskManager(max_workers=1)
        app.dependency_overrides[create_task_manager] = lambda: manager
        started = threading.Event()
        release = threading.Event()

        def slow_job():
            started.set()
            release.wait(5)
            return {}

        run_id = manager.submit(slow_job, kind="initial", owner=ids["alice"], plan_id=plan_id)
        try:
            assert started.wait(5)
            response = TestClient(app).post(f"/api/plans/{plan_id}/runs/{run_id}/cancel")
            assert response.status_code == 200, response.text
            assert response.json()["canceled"] is True

            release.set()
            _wait_until(lambda: manager.get(run_id).done)
            with factory() as session:
                assert session.scalar(
                    select(CareerPlan).where(CareerPlan.id == plan_id)
                ) is not None, "只停止分析不能删档案"
        finally:
            release.set()
            manager.shutdown()
            app.dependency_overrides.pop(create_task_manager, None)

    def test_cancel_endpoint_rejects_foreign_plan_and_finished_run(self, archive):
        factory, ids = archive
        plan_id = self._plan(ids["alice"])
        manager = TaskManager(max_workers=1)
        app.dependency_overrides[create_task_manager] = lambda: manager
        try:
            run_id = manager.submit(lambda: {}, kind="initial", owner=ids["alice"], plan_id=plan_id)
            _wait_until(lambda: manager.get(run_id).done)

            self._login(ids["alice"])
            finished = TestClient(app).post(f"/api/plans/{plan_id}/runs/{run_id}/cancel")
            assert finished.status_code == 200
            assert finished.json()["canceled"] is False

            self._login(ids["bob"])
            foreign = TestClient(app).post(f"/api/plans/{plan_id}/runs/{run_id}/cancel")
            assert foreign.status_code == 404

            self._login(ids["alice"])
            unknown = TestClient(app).post(f"/api/plans/{plan_id}/runs/nope/cancel")
            assert unknown.status_code == 404
        finally:
            manager.shutdown()
            app.dependency_overrides.pop(create_task_manager, None)
