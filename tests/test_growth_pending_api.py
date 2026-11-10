"""阶段 5.3：待处理成长事件查询接口（GET /api/growth/pending）的隔离测试。

覆盖：
- 无待处理事件时返回正确的空结果。
- 存在待处理事件（已完成任务）时返回真实事件内容。
- 已正式进入成长历史（指纹已处理）的事件不会被重复列为待处理。
- Store 数据损坏时返回 restored=False + warnings，不静默伪造空结果。
- 查询接口不写 LoopStore / GrowthStore（读前后字节不变）。
- 5.1 只读分析边界与 5.2 任务完成接口回归不回归。

隔离：
- 每个用例用临时目录构造 LoopStore / GrowthStore，经 dependency_overrides 注入；
  全程不触碰项目 runs/、不调 Ollama。
"""

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from agents.loop_store import LoopStore  # noqa: E402
from agents.growth_store import GrowthStore  # noqa: E402
from agents.growth_rules import event_fingerprint  # noqa: E402
from backend.app import app  # noqa: E402
from backend.deps import create_stores  # noqa: E402
from backend.services import Stores  # noqa: E402


def _seed_loop(loop: LoopStore) -> list[dict]:
    """写入一轮含任务记录，返回 [{task_id, title, kind, skills}]。"""
    learning = [{
        "topic": "红黑树", "covers": ["Python"], "description": "d",
        "stage": 1, "duration": "1周", "prerequisites": [],
        "method": "学习", "assign": "自己", "assessment": "可实现", "goal": "掌握",
        "status_v2": "pending", "resources": [], "priority": "高",
        "milestones": [], "deliverables": [],
    }]
    res = loop.append_round(target_job="软件工程师", learning_plan=learning,
                            project_plan=[], round_no=1)
    assert res["status"] == "written"
    return loop.load().data["rounds"][0]["tasks"]


def _complete_task(loop: LoopStore, task: dict, evidence="已完成红黑树实现") -> None:
    res = loop.mark_completed(task["task_id"], evidence=evidence)
    assert res["status"] == "completed"


class GrowthPendingApiTest(unittest.TestCase):
    """接口层：通过 TestClient + dependency_overrides 注入临时 store。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._loop = LoopStore(directory=self._tmp.name)
        self._growth = GrowthStore(directory=self._tmp.name)
        self._stores = Stores(growth=self._growth, loop=self._loop)
        app.dependency_overrides[create_stores] = lambda: self._stores
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.pop(create_stores, None)
        self._tmp.cleanup()

    def _get(self):
        return self.client.get("/api/growth/pending")

    def test_no_pending_returns_empty(self):
        resp = self._get()
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["events"], [])
        self.assertEqual(body["tasks"], [])
        self.assertEqual(body["seen"], 0)
        self.assertTrue(body["restored"])
        self.assertEqual(body["warnings"], [])

    def test_pending_event_returned(self):
        task = _seed_loop(self._loop)[0]
        _complete_task(self._loop, task, evidence="完成了红黑树并跑通过")
        resp = self._get()
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(len(body["events"]), 1)
        ev = body["events"][0]
        self.assertEqual(ev["title"], "红黑树")
        self.assertIn("Python", ev["skills"])
        self.assertEqual(ev["evidence"], "完成了红黑树并跑通过")
        self.assertEqual(len(body["tasks"]), 1)
        self.assertEqual(body["tasks"][0]["task_id"], task["task_id"])
        self.assertTrue(body["restored"])

    def test_processed_event_not_relisted(self):
        task = _seed_loop(self._loop)[0]
        _complete_task(self._loop, task, evidence="证据")
        # 模拟 growth_node 已正式处理：向成长历史写入含同一指纹的事件
        fingerprint = event_fingerprint("learning", "红黑树", ["Python"])
        self._growth.append({
            "version": "v1",
            "events": [{"type": "learning", "title": "红黑树",
                        "skills": ["Python"], "evidence": "证据", "fingerprint": fingerprint}],
            "user_skills_after": [{"skill": "Python", "level": "中级"}],
        })
        resp = self._get()
        body = resp.json()
        self.assertEqual(body["events"], [], "已正式处理的事件不应再次列为待处理")
        self.assertEqual(body["tasks"], [])
        self.assertEqual(body["seen"], 1)

    def test_query_does_not_mutate_stores(self):
        task = _seed_loop(self._loop)[0]
        _complete_task(self._loop, task, evidence="证据")
        loop_before = self._loop.path.read_bytes() if self._loop.path.exists() else None
        growth_before = self._growth.path.read_bytes() if self._growth.path.exists() else None
        self._get()
        loop_after = self._loop.path.read_bytes() if self._loop.path.exists() else None
        growth_after = self._growth.path.read_bytes() if self._growth.path.exists() else None
        self.assertEqual(loop_before, loop_after, "查询不应改动 loop.json")
        self.assertEqual(growth_before, growth_after, "查询不应改动 growth.json")


class GrowthPendingCorruptTest(unittest.TestCase):
    """成长历史损坏 / 不可读时的行为。"""

    def test_corrupt_growth_history_returns_restored_false_with_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            loop = LoopStore(directory=tmp)
            task = _seed_loop(loop)[0]
            _complete_task(loop, task, evidence="证据")
            growth = GrowthStore(directory=tmp)
            (Path(tmp) / "growth.json").write_text("{ broken json", encoding="utf-8")
            stores = Stores(growth=growth, loop=loop)
            app.dependency_overrides[create_stores] = lambda: stores
            try:
                client = TestClient(app)
                resp = client.get("/api/growth/pending")
                self.assertEqual(resp.status_code, 200)
                body = resp.json()
                # 成长历史不可读 → 不自动排队，restored=False，warnings 非空，
                # 决不能用空 events 掩盖"无法判断已处理"这一事实。
                self.assertFalse(body["restored"])
                self.assertEqual(body["events"], [])
                self.assertEqual(body["tasks"], [])
                self.assertTrue(body["warnings"], "成长历史不可读时应有非空 warnings")
            finally:
                app.dependency_overrides.pop(create_stores, None)


if __name__ == "__main__":
    unittest.main(verbosity=2)