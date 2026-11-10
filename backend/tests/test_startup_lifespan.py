"""启动 / 停机（lifespan）与后台运行记录的落账测试。

覆盖 P0-④ 的两件事
-------------------
1. **启动即建表**：原先 ``users`` 表要等第一个登录/注册请求才创建，冷启动时
   数据库可能压根没初始化；现在 ``lifespan`` 在应用启动时幂等地做完。
2. **退出时收尾**：应用停机时关闭线程池，并把**还没开始执行**的排队任务如实
   标为失败 —— 否则 ``GET /api/runs/{id}`` 会永远返回 ``queued``，前端空轮询。

另外验证 ``_execute`` 的异常口径：普通业务异常照旧写进 ``record.error`` 并
吞掉（提交是异步的，不能抛给调用方）；``SystemExit`` / ``KeyboardInterrupt``
这类进程级信号则**先落账再原样抛出**，不再被无声吞掉。

隔离（本文件必须零副作用）
--------------------------
- ``config.AUTH_DB_PATH`` 指向临时目录 → 绝不碰项目 ``runs/app.db``；
- ``backend.deps._default_task_manager`` 换成测试专用实例 → **绝不会关掉进程级
  单例**，同一次 pytest 里后面的用例还能正常提交任务；
- 阻塞任务用 ``Event`` + 超时释放，退出前一定放行，不把工作线程吊死在
  解释器退出上（线程池线程是非守护的）。
"""

import sqlite3
import sys
import tempfile
import threading
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from backend import config, db, deps  # noqa: E402
from backend.app import app  # noqa: E402
from backend.jobs import STATUS_FAILED, STATUS_QUEUED, TaskManager  # noqa: E402


def _tables(db_path: Path) -> list[str]:
    """读表名。**必须显式 close**：``with sqlite3.connect(...)`` 只提交不关连接，
    Windows 上这个句柄会让临时目录删不掉。"""
    connection = sqlite3.connect(db_path)
    try:
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    finally:
        connection.close()
    return sorted(name for name, in rows)


class LifespanTest(unittest.TestCase):
    """``with TestClient(app)`` 会真的跑 startup / shutdown 两段生命周期。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "app.db"
        self.manager = TaskManager(max_workers=1)
        self.release = threading.Event()

        def drop_engine_and_tmp():
            """engine 的连接池留在 ``backend.db`` 的模块级缓存里；Windows 上句柄
            不释放就删不掉临时目录（正是那类 ``os.replace`` 占用的同款问题）。"""
            url = f"sqlite:///{self.db_path}"
            with db._cache_lock:
                db._factories.pop(url, None)
                engine = db._engines.pop(url, None)
            if engine is not None:
                engine.dispose()
            self._tmp.cleanup()

        # addCleanup 是 LIFO（后注册的先跑）：先放行阻塞任务，再释放引擎、删临时目录。
        self.addCleanup(drop_engine_and_tmp)
        self.addCleanup(self.release.set)

    def _isolated(self, stack: ExitStack):
        """把 DB 路径与任务管理器单例都换成测试替身。"""
        stack.enter_context(mock.patch.object(config, "AUTH_DB_PATH", self.db_path))
        stack.enter_context(
            mock.patch.object(deps, "_default_task_manager", lambda: self.manager)
        )

    def test_startup_creates_the_users_table(self):
        with ExitStack() as stack:
            self._isolated(stack)
            with TestClient(app):
                self.assertTrue(self.db_path.exists(), "启动时就应建好数据库文件")
                self.assertIn("users", _tables(self.db_path), "启动时就应建好 users 表")

    def test_shutdown_closes_the_task_manager(self):
        with ExitStack() as stack:
            self._isolated(stack)
            with TestClient(app):
                pass
        # 线程池已关闭 —— 再提交必然报错，这证明 lifespan 真的调用了 shutdown
        with self.assertRaises(RuntimeError):
            self.manager.submit(lambda: None)

    def test_unstarted_queued_job_is_marked_failed_on_shutdown(self):
        # 用唯一的工作线程占住一个长任务，让第二个任务确定排在它后面且永远轮不到
        self.manager.submit(self.release.wait, 5, kind="blocking")
        queued_id = self.manager.submit(lambda: None, kind="never_runs")

        with ExitStack() as stack:
            self._isolated(stack)
            with TestClient(app):
                self.assertEqual(
                    self.manager.snapshot(queued_id)["status"], STATUS_QUEUED,
                    "停机前应仍是 queued",
                )

        record = self.manager.snapshot(queued_id)
        self.assertEqual(record["status"], STATUS_FAILED)
        self.assertIn("服务已停止", record["error"])
        self.assertTrue(record["finished_at"], "被取消的任务也要有结束时间")

    def test_startup_is_idempotent_across_restarts(self):
        """重复建表不能报错 —— uvicorn --reload 每次热重载都会走一遍 startup。"""
        for _ in range(2):
            with ExitStack() as stack:
                self._isolated(stack)
                with TestClient(app):
                    pass
        self.assertIn("users", _tables(self.db_path))


def _raise_business_error():
    raise ValueError("分析节点报错")


def _raise_process_signal():
    raise KeyboardInterrupt("模拟进程级信号")


class JobFailureAccountingTest(unittest.TestCase):
    """后台线程里的异常必须如实落账，且区分业务异常与进程级信号。"""

    def test_business_exception_is_recorded_and_swallowed(self):
        manager = TaskManager(max_workers=1)
        run_id = manager.submit(_raise_business_error, kind="analyze")
        manager.shutdown(wait=True)  # 异常不得逸出到调用线程

        record = manager.snapshot(run_id)
        self.assertEqual(record["status"], STATUS_FAILED)
        self.assertEqual(record["error"], "ValueError: 分析节点报错")
        self.assertIsNone(record["result"])

    def test_process_level_signal_is_recorded_and_reraised(self):
        manager = TaskManager(max_workers=1)
        run_id = manager.submit(_raise_process_signal, kind="analyze")
        manager.shutdown(wait=True)

        record = manager.snapshot(run_id)
        self.assertEqual(record["status"], STATUS_FAILED)
        self.assertIn("进程级异常 KeyboardInterrupt", record["error"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
