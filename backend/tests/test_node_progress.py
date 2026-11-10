"""节点级进度：run 记录要能如实说清跑到哪一步、断在哪一步。"""

from __future__ import annotations

import threading
import time
from unittest import mock

import pytest
from fastapi.testclient import TestClient

from backend import analyze
from backend.app import app
from backend.auth import get_current_user
from backend.deps import create_task_manager
from backend.jobs import (
    STATUS_FAILED,
    STATUS_RUNNING,
    STATUS_SUCCEEDED,
    TaskManager,
    report_node_finished,
    report_node_interrupted,
    report_node_skipped,
    report_node_started,
)


def _wait_until(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("等待超时")


def _node_statuses(snapshot: dict) -> dict[str, str]:
    return {item["node"]: item["status"] for item in snapshot["progress"]}


class TestManagerProgress:
    def test_node_lifecycle_is_visible_in_snapshot(self):
        manager = TaskManager(max_workers=1)
        started = threading.Event()
        release = threading.Event()
        snapshot_holder = {}

        def job():
            report_node_started("profile")
            started.set()
            release.wait(5)
            report_node_finished("profile")
            report_node_started("growth")
            report_node_finished("growth")
            report_node_skipped("retrieval")
            return {"ok": True}

        run_id = manager.submit(job)
        try:
            assert started.wait(5)
            running = manager.snapshot(run_id)
            assert running["status"] == STATUS_RUNNING
            assert running["current_node"] == "profile"
            assert _node_statuses(running) == {"profile": "running"}

            release.set()
            _wait_until(lambda: manager.snapshot(run_id)["status"] == STATUS_SUCCEEDED)
            done = manager.snapshot(run_id)
            assert done["current_node"] is None
            assert _node_statuses(done) == {
                "profile": "done",
                "growth": "done",
                "retrieval": "skipped",
            }
            profile = done["progress"][0]
            assert profile["started_at"]
            assert profile["finished_at"]
            assert profile["duration_ms"] is not None
            assert profile["duration_ms"] >= 0
        finally:
            release.set()
            manager.shutdown()

    def test_node_failure_is_attributed_to_that_node(self):
        manager = TaskManager(max_workers=1)

        def job():
            report_node_started("job")
            raise ValueError("模型返回了空要求")

        run_id = manager.submit(job)
        try:
            _wait_until(lambda: manager.snapshot(run_id)["status"] == STATUS_FAILED)
            snapshot = manager.snapshot(run_id)
            assert snapshot["current_node"] is None
            assert _node_statuses(snapshot) == {"job": "failed"}
            assert "空要求" in snapshot["progress"][0]["error"]
        finally:
            manager.shutdown()

    def test_interrupt_helper_marks_the_running_node(self):
        manager = TaskManager(max_workers=1)

        def job():
            report_node_started("assessment")
            report_node_interrupted("流式中断")
            return {}

        run_id = manager.submit(job)
        try:
            _wait_until(lambda: manager.snapshot(run_id)["status"] == STATUS_SUCCEEDED)
            snapshot = manager.snapshot(run_id)
            assert _node_statuses(snapshot) == {"assessment": "failed"}
            assert snapshot["progress"][0]["error"] == "流式中断"
        finally:
            manager.shutdown()

    def test_cancel_closes_the_running_node(self):
        manager = TaskManager(max_workers=1)
        started = threading.Event()
        release = threading.Event()

        def job():
            report_node_started("learning")
            started.set()
            release.wait(5)
            return {}

        run_id = manager.submit(job, kind="initial", owner=7, plan_id="p1")
        try:
            assert started.wait(5)
            manager.cancel_run(run_id, plan_id="p1", owner=7)
            snapshot = manager.snapshot(run_id)
            assert _node_statuses(snapshot) == {"learning": "canceled"}
            assert snapshot["current_node"] is None
        finally:
            release.set()
            manager.shutdown()

    def test_progress_outside_a_task_is_a_noop(self):
        report_node_started("profile")
        report_node_finished("profile")
        report_node_interrupted("无上下文")
        report_node_skipped("growth")


class _FakeGraph:
    def __init__(self, events=None, error=None):
        self.events = events or []
        self.error = error
        self.calls = []

    def stream(self, state, stream_mode=None):
        self.calls.append((state, stream_mode))
        for item in self.events:
            yield item
        if self.error is not None:
            raise self.error

    def invoke(self, state):  # pragma: no cover - 有流式时不该走这里
        raise AssertionError("后台运行必须走流式，才能回报节点进度")


def _event(kind: str, node: str, error=None):
    return ("debug", {"type": kind, "payload": {"name": node, "error": error}})


class TestAnalyzeStream:
    def test_stream_records_each_node_and_marks_unvisited_as_skipped(self):
        events = [
            ("values", {"target_job": "会计"}),
            _event("task", "profile"),
            _event("task_result", "profile"),
            _event("task", "growth"),
            _event("task_result", "growth"),
            _event("task", "retrieval"),
            _event("task_result", "retrieval"),
            _event("task", "job"),
            _event("task_result", "job"),
            _event("task", "assessment"),
            _event("task_result", "assessment"),
            ("values", {"target_job": "会计", "skill_gaps": []}),
        ]
        fake = _FakeGraph(events)
        manager = TaskManager(max_workers=1)

        with mock.patch.object(analyze, "workflow", fake):
            run_id = manager.submit(analyze._run_workflow, {"target_job": "会计"})
            try:
                _wait_until(lambda: manager.snapshot(run_id)["status"] == STATUS_SUCCEEDED)
                snapshot = manager.snapshot(run_id)
                assert snapshot["result"] == {"target_job": "会计", "skill_gaps": []}
                assert fake.calls[0][1] == ["debug", "values"]
                statuses = _node_statuses(snapshot)
                for node in ("profile", "growth", "retrieval", "job", "assessment"):
                    assert statuses[node] == "done"
                assert statuses["learning"] == "skipped"
                assert statuses["project"] == "skipped"
            finally:
                manager.shutdown()

    def test_stream_exception_marks_the_node_that_was_running(self):
        events = [
            ("values", {"target_job": "会计"}),
            _event("task", "profile"),
            _event("task_result", "profile"),
            _event("task", "job"),
        ]
        fake = _FakeGraph(events, error=RuntimeError("模型超时"))
        manager = TaskManager(max_workers=1)

        with mock.patch.object(analyze, "workflow", fake):
            run_id = manager.submit(analyze._run_workflow, {"target_job": "会计"})
            try:
                _wait_until(lambda: manager.snapshot(run_id)["status"] == STATUS_FAILED)
                snapshot = manager.snapshot(run_id)
                assert _node_statuses(snapshot)["profile"] == "done"
                assert _node_statuses(snapshot)["job"] == "failed"
                assert "模型超时" in snapshot["progress"][1]["error"]
                assert "模型超时" in snapshot["error"]
            finally:
                manager.shutdown()

    def test_task_result_error_fails_the_run_at_that_node(self):
        events = [
            ("values", {"target_job": "会计"}),
            _event("task", "profile"),
            _event("task_result", "profile"),
            _event("task", "job"),
            _event("task_result", "job", error="ValueError: 空要求"),
        ]
        fake = _FakeGraph(events)
        manager = TaskManager(max_workers=1)

        with mock.patch.object(analyze, "workflow", fake):
            run_id = manager.submit(analyze._run_workflow, {"target_job": "会计"})
            try:
                _wait_until(lambda: manager.snapshot(run_id)["status"] == STATUS_FAILED)
                snapshot = manager.snapshot(run_id)
                statuses = _node_statuses(snapshot)
                assert statuses["profile"] == "done"
                assert statuses["job"] == "failed"
                # 失败后没执行到的节点不能标成 done，也不冒充 skipped
                assert "assessment" not in statuses
                assert "空要求" in snapshot["error"]
            finally:
                manager.shutdown()


class TestRunApiExposesProgress:
    """HTTP 查询接口原样带出节点进度（前端轮询的落点）。"""

    def test_get_run_returns_progress_and_current_node(self):
        manager = TaskManager(max_workers=1)
        app.dependency_overrides[create_task_manager] = lambda: manager
        app.dependency_overrides[get_current_user] = lambda: {"id": 7, "username": "u"}
        started = threading.Event()
        release = threading.Event()

        def job():
            report_node_started("profile")
            started.set()
            release.wait(5)
            report_node_finished("profile")
            return {"ok": True}

        run_id = manager.submit(job, kind="initial", owner=7, plan_id="p1")
        try:
            assert started.wait(5)
            response = TestClient(app).get(f"/api/runs/{run_id}")
            assert response.status_code == 200, response.text
            body = response.json()
            assert body["current_node"] == "profile"
            assert body["progress"][0]["node"] == "profile"
            assert body["progress"][0]["status"] == "running"

            release.set()
            _wait_until(lambda: manager.snapshot(run_id)["status"] == STATUS_SUCCEEDED)
            done = TestClient(app).get(f"/api/runs/{run_id}").json()
            assert done["current_node"] is None
            assert done["progress"][0]["status"] == "done"
        finally:
            release.set()
            manager.shutdown()
            app.dependency_overrides.pop(create_task_manager, None)
            app.dependency_overrides.pop(get_current_user, None)


class TestInvokeFallback:
    def test_outside_a_task_run_still_uses_invoke(self):
        """CLI / 旧测试直接调用 run_workflow 时不需要进度，仍走一次性 invoke。"""
        fake = mock.Mock()
        fake.invoke.return_value = {"target_job": "会计"}
        with mock.patch.object(analyze, "workflow", fake):
            state = analyze._run_workflow({"target_job": "会计"})
        assert state == {"target_job": "会计"}
        fake.invoke.assert_called_once_with({"target_job": "会计"})
