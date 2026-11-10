"""多用户数据隔离的回归测试。

锁三件事
--------
1. ``create_stores`` 按"当前用户"把 store 指到 ``users/user_{id}/``，
   不同用户走同一套 HTTP 接口时，任务清单 / 成长历史互不可见，
   拿别人的 task_id 也改不了（unknown_task → 404）。
2. ``TaskManager.snapshot`` 的归属过滤：别人的 run_id 视同不存在（404），
   ``owner=None``（内部/测试直连）不过滤。
3. 旧 JSON 数据不再由启动/注册自动认领；迁移改走显式脚本
   （``scripts/migrate_legacy_plans.py``，见 ``test_legacy_migration.py``）。

隔离策略：``AUTH_DB_PATH`` 指到临时目录 → per-user 目录与迁移自动跟着隔离，
全程不碰项目 ``runs/``，也不调 Ollama。
"""

import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from backend import config  # noqa: E402
from backend.app import app  # noqa: E402
from backend.auth import get_current_user  # noqa: E402
from backend.deps import create_stores  # noqa: E402
from backend.jobs import TaskManager  # noqa: E402


def _noop():
    return {"ok": True}


def _wait_done(manager: TaskManager, run_id: str, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        record = manager.get(run_id)
        if record is not None and record.done:
            return
        time.sleep(0.01)
    raise AssertionError(f"run_id={run_id} 在 {timeout}s 内没进入终态")


class PerUserStoresTest(unittest.TestCase):
    """create_stores 的目录派生：按用户分目录，路径函数锚定认证库所在目录。"""

    def test_stores_live_in_per_user_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(config, "AUTH_DB_PATH", Path(tmp) / "app.db"):
                stores = create_stores(plan_id=None, current={"id": 7, "username": "u7"})
                expected = Path(tmp) / "users" / "user_7"
                self.assertEqual(stores.loop.path.parent, expected)
                self.assertEqual(stores.growth.path.parent, expected)
                self.assertEqual(stores.loop.user_id, "user_7")
                self.assertEqual(stores.growth.user_id, "user_7")

    def test_different_users_get_different_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(config, "AUTH_DB_PATH", Path(tmp) / "app.db"):
                first = create_stores(plan_id=None, current={"id": 1, "username": "u1"})
                second = create_stores(plan_id=None, current={"id": 2, "username": "u2"})
                self.assertNotEqual(first.loop.path.parent, second.loop.path.parent)
                self.assertNotEqual(first.growth.path.parent, second.growth.path.parent)


class IsolationOverHttpTest(unittest.TestCase):
    """两个用户走同一套接口，业务数据互不可见。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._patch = mock.patch.object(config, "AUTH_DB_PATH", Path(self._tmp.name) / "app.db")
        self._patch.start()
        self.client = TestClient(app)

        self.addCleanup(self._patch.stop)
        self.addCleanup(self._safe_cleanup)

    def _safe_cleanup(self):
        try:
            self._tmp.cleanup()
        except PermissionError:
            # Windows 上 SQLite 句柄可能拖到 teardown 之后才释放
            pass

    def _as_user(self, user_id: int):
        """切换"当前用户"（只覆盖 get_current_user，create_stores 走真实实现）。

        结束后**恢复**进入前的覆盖值（conftest 的全局覆盖可能就在那儿），
        而不是 pop 掉 —— 否则会污染同一进程里后面的测试（顺序依赖）。
        """
        saved = app.dependency_overrides.get(get_current_user)
        app.dependency_overrides[get_current_user] = lambda: {
            "id": user_id, "username": f"u{user_id}",
        }

        def restore():
            app.dependency_overrides.pop(get_current_user, None)
            if saved is not None:
                app.dependency_overrides[get_current_user] = saved

        self.addCleanup(restore)

    def test_two_users_cannot_see_each_others_data(self):
        # 用户 1 补录一条成果 → 写进 user_1 自己的 loop.json
        self._as_user(1)
        added = self.client.post("/api/growth/manual-event", json={
            "kind": "learning", "title": "用户1的成果", "skills": ["Python"], "evidence": "e1",
        })
        self.assertEqual(added.status_code, 200, added.text)
        self.assertTrue(added.json()["added"])
        self.assertEqual(self.client.get("/api/tasks").json()["summary"]["total"], 1)

        # 用户 2：同样的接口，什么都看不到
        self._as_user(2)
        tasks = self.client.get("/api/tasks").json()
        self.assertEqual(tasks["summary"]["total"], 0)
        self.assertEqual(tasks["tasks"], [])
        history = self.client.get("/api/growth/history").json()
        self.assertEqual(history["confirmed_count"], 0)

        # 用户 2 拿用户 1 的 task_id 提交完成 → 404（在他的数据里是 unknown_task）
        hijack = self.client.post("/api/tasks/r001-l001/complete", json={"evidence": "越权尝试"})
        self.assertEqual(hijack.status_code, 404)

        # 用户 2 自己补录 → 只影响自己的目录，用户 1 的数据不受牵连
        added2 = self.client.post("/api/growth/manual-event", json={
            "kind": "project", "title": "用户2的成果", "skills": ["Java"], "evidence": "e2",
        })
        self.assertTrue(added2.json()["added"])
        app.dependency_overrides[get_current_user] = lambda: {"id": 1, "username": "u1"}
        after = self.client.get("/api/tasks").json()
        self.assertEqual(after["summary"]["total"], 1, "用户 2 的写入不得影响用户 1")
        self.assertEqual(after["tasks"][0]["title"], "用户1的成果")


class RunOwnershipTest(unittest.TestCase):
    """GET /api/runs/{run_id} 只看本人提交的记录。"""

    def test_snapshot_filters_by_owner(self):
        manager = TaskManager(max_workers=1)
        self.addCleanup(manager.shutdown, True)
        run_id = manager.submit(_noop, owner=1, kind="analyze")
        _wait_done(manager, run_id)

        self.assertIsNotNone(manager.snapshot(run_id, owner=1), "本人可见")
        self.assertIsNone(manager.snapshot(run_id, owner=2), "别人的 run_id 视同不存在")
        self.assertIsNotNone(manager.snapshot(run_id, owner=None), "内部查询不做归属过滤")

    def test_orphan_record_is_visible_to_any_owner_query(self):
        """owner=None 的记录（内部提交/旧数据）对任何查询者可见，不误伤。"""
        manager = TaskManager(max_workers=1)
        self.addCleanup(manager.shutdown, True)
        run_id = manager.submit(_noop, kind="job")
        _wait_done(manager, run_id)
        self.assertIsNotNone(manager.snapshot(run_id, owner=1))
        self.assertIsNotNone(manager.snapshot(run_id, owner=2))

    def test_http_run_query_hides_other_users_runs(self):
        manager = TaskManager(max_workers=1)
        self.addCleanup(manager.shutdown, True)
        run_id = manager.submit(_noop, owner=1, kind="analyze")
        _wait_done(manager, run_id)

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(config, "AUTH_DB_PATH", Path(tmp) / "app.db"):
                saved = app.dependency_overrides.get(get_current_user)
                app.dependency_overrides[get_current_user] = lambda: {"id": 2, "username": "u2"}

                def restore():
                    app.dependency_overrides.pop(get_current_user, None)
                    if saved is not None:
                        app.dependency_overrides[get_current_user] = saved

                self.addCleanup(restore)
                client = TestClient(app)
                response = client.get(f"/api/runs/{run_id}")
                self.assertEqual(response.status_code, 404, "用户 2 查用户 1 的 run 必须 404")


if __name__ == "__main__":
    unittest.main(verbosity=2)
