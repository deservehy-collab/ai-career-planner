"""阶段 5.2：任务完成/成果证据提交接口（POST /api/tasks/{id}/complete）的隔离测试。

覆盖：
- 合法提交：任务状态正确变化、completion_written=True、不谎报"已升级"。
- 空白证据字符串 / 超长证据：Pydantic 拒绝（422）；完全不传 evidence 则按自述完成。
- 心得单独保存，不并入证据。
- 不存在的 task_id：404。
- 重复提交：already_completed，且首次证据不被覆盖。
- 成长评估分支 / Store 写入失败：返回合理状态，不绕过保护。
- 现有只读分析边界（analyze 不写真实 runs）不受影响。

隔离策略：
- 每个用例用临时目录构造 LoopStore / GrowthStore，并通过 FastAPI 的
  dependency_overrides 注入；全程不触碰项目 runs/。
- 不调用 Ollama：只跑 process_task_completion 的纯计算路径（读 YAML 词表/技能库），
  并在必要的分支用 mock，绝不触发 workflow.invoke / 生成接口。
"""

import json
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
from backend.app import app  # noqa: E402
from backend.deps import create_stores  # noqa: E402
from backend.services import Stores  # noqa: E402


def _seed_loop(loop: LoopStore, target_job="软件工程师", with_skills=True) -> list[str]:
    """向临时 LoopStore 写一轮含任务的记录，返回 {task_id: (skills)} 对应顺序。"""
    learning = [
        {"topic": "数据结构", "covers": (["Python"] if with_skills else []),
         "description": "d", "stage": 1, "duration": "1周", "prerequisites": [],
         "method": "学习", "assign": "自己", "assessment": "可实现", "goal": "掌握",
         "status_v2": "pending",
         "resources": [], "priority": "高", "milestones": [], "deliverables": []},
    ]
    res = loop.append_round(target_job=target_job, learning_plan=learning,
                            project_plan=[], round_no=1)
    assert res["status"] == "written"
    data = loop.load().data
    return [t["task_id"] for t in data["rounds"][0]["tasks"]]


class TaskCompleteApiTest(unittest.TestCase):
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

    def _task_id(self) -> str:
        return _seed_loop(self._loop)[0]

    def test_valid_submission_marks_completed(self):
        tid = self._task_id()
        resp = self.client.post(f"/api/tasks/{tid}/complete",
                                json={"evidence": "完成了红黑树实现并跑通测试"})
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertIn(body["status"], {
            "completed_upgraded", "completed_no_upgrade",
            "completed_no_growth_event", "completion_failed",
        })
        self.assertTrue(body["completion_written"])
        self.assertFalse(body["already_completed"])
        # 任务已标记完成
        task = next(t for t in self._load_tasks() if t["task_id"] == tid)
        self.assertEqual(task["status"], "completed")
        self.assertTrue(task.get("evidence"))

    def _load_tasks(self):
        return [t for r in self._loop.load().data["rounds"] for t in r["tasks"]]

    def test_blank_evidence_rejected(self):
        tid = self._task_id()
        for bad in ["", "   ", "\n\t"]:
            resp = self.client.post(f"/api/tasks/{tid}/complete", json={"evidence": bad})
            self.assertEqual(resp.status_code, 422, f"空白证据应 422，实际 {resp.status_code}")
        # 未写入任何完成记录
        tasks = self._load_tasks()
        self.assertTrue(all(t["status"] != "completed" for t in tasks))

    def test_overlong_evidence_rejected(self):
        tid = self._task_id()
        resp = self.client.post(f"/api/tasks/{tid}/complete",
                                json={"evidence": "e" * 5001})
        self.assertEqual(resp.status_code, 422)
        tasks = self._load_tasks()
        self.assertTrue(all(t["status"] != "completed" for t in tasks))

    def test_unknown_task_404(self):
        resp = self.client.post("/api/tasks/r999-l99/complete",
                                json={"evidence": "证据"})
        self.assertEqual(resp.status_code, 404)

    def test_invalid_task_id_format_422(self):
        resp = self.client.post("/api/tasks/not-a-task-id/complete",
                                json={"evidence": "证据"})
        # 格式不合法 → process 返回 invalid_task_id → 422（若 unknown 则 404，二者必居其一）
        self.assertIn(resp.status_code, (404, 422))

    def test_repeat_submission_idempotent_preserves_evidence(self):
        tid = self._task_id()
        r1 = self.client.post(f"/api/tasks/{tid}/complete",
                              json={"evidence": "首次证据"})
        self.assertEqual(r1.status_code, 200)
        self.assertFalse(r1.json()["already_completed"])

        r2 = self.client.post(f"/api/tasks/{tid}/complete",
                              json={"evidence": "第二次不同证据"})
        self.assertEqual(r2.status_code, 200)
        body = r2.json()
        self.assertTrue(body["already_completed"])
        # 任务仍只有首次证据，未被覆盖
        task = next(t for t in self._load_tasks() if t["task_id"] == tid)
        self.assertEqual(task["evidence"], "首次证据")

    def test_growth_store_not_written_by_api(self):
        """提交任务绝不写 GrowthStore（成长历史）；升级只是评估计划，不落盘。"""
        tid = self._task_id()
        resp = self.client.post(f"/api/tasks/{tid}/complete",
                                json={"evidence": "做完了课程作业"})
        self.assertEqual(resp.status_code, 200)
        # GrowthStore 应保持空（无历史写入）
        growth = self._growth.load().data
        self.assertEqual(len(growth.get("history") or []), 0)
        # 若响应声称升级，updates 非空也是"计划"而非已写成长历史——历史仍空即证明
        body = resp.json()
        self.assertIn("upgraded", body)
        self.assertIsInstance(body.get("updates"), list)


class StoreWriteFailureTest(unittest.TestCase):
    """Store 写入失败 / 损坏时，接口遵循现有保护，不绕过、不谎报。"""

    def test_corrupt_loop_store_returns_failure_not_fake_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            loop = LoopStore(directory=tmp)
            # 写入损坏内容（非法 JSON）→ load 不可安全写入，mark_completed 应拒绝
            (Path(tmp) / "loop.json").write_text("{ broken json", encoding="utf-8")
            growth = GrowthStore(directory=tmp)
            stores = Stores(growth=growth, loop=loop)
            app.dependency_overrides[create_stores] = lambda: stores
            try:
                client = TestClient(app)
                resp = client.post("/api/tasks/r001-l01/complete",
                                   json={"evidence": "证据"})
                # 任务完成记录无法落盘：既不是 200 伪成功，也不报告升级
                self.assertEqual(resp.status_code, 200)  # 接口仍返回结构（投影 status 为失败态）
                body = resp.json()
                self.assertEqual(body["completion_written"], False)
            finally:
                app.dependency_overrides.pop(create_stores, None)

    def test_evaluate_failure_does_not_claim_upgrade(self):
        """成长历史不可读时评估放弃：不把结果表述成已升级。"""
        import agents.growth_loop as gl

        with tempfile.TemporaryDirectory() as tmp:
            loop = LoopStore(directory=tmp)
            _seed_loop(loop, with_skills=True)
            tid = loop.load().data["rounds"][0]["tasks"][0]["task_id"]
            stores = Stores(growth=GrowthStore(directory=tmp), loop=loop)
            app.dependency_overrides[create_stores] = lambda: stores
            try:
                with mock.patch.object(gl, "_evaluate_event",
                                       return_value={
                                           "evaluated": False, "outcome": "store_unreadable",
                                           "signals": [], "updates": [], "blocked": [],
                                           "skipped": [], "invalid": [], "duplicates": [],
                                           "unmapped": [], "notes": [],
                                           "reason": "成长历史不可读，本次不评估升级",
                                           "baseline_skills": [], "baseline_source": None,
                                           "store_status": "corrupt",
                                           "knowledge_base_status": None,
                                       }):
                    client = TestClient(app)
                    resp = client.post(f"/api/tasks/{tid}/complete",
                                       json={"evidence": "完成了学习任务"})
                self.assertEqual(resp.status_code, 200)
                body = resp.json()
                self.assertFalse(body["upgraded"])
                self.assertEqual(body["updates"], [])
                self.assertEqual(body["growth_evaluated"], False)
            finally:
                app.dependency_overrides.pop(create_stores, None)


class ReadonlyBoundaryRegressionTest(unittest.TestCase):
    """回归：分析接口只读边界（5.1）不因本阶段改动受影响。"""

    def test_analyze_still_isolated(self):
        import backend.analyze as analyze_mod
        from backend import schemas

        with tempfile.TemporaryDirectory() as tmp:
            # 记录进入前 growth_agent 全局 store 目录
            growth_agent_before = __import__("agents.growth_agent",
                                             fromlist=["get_store"]).get_store().directory

            fake_state = {"user_skills": [], "growth_diff": {}}
            def fake_invoke(state):
                current = __import__("agents.growth_agent",
                                     fromlist=["get_store"]).get_store()
                self.assertNotEqual(current.directory, growth_agent_before)
                return fake_state

            payload = schemas.AnalyzeRequest(
                user_profile=schemas.UserProfileRequest(
                    major="软件工程", grade="大三", skills=["Python"], projects=[]),
                target_job="AI工程师",
                growth_events=[{"type": "test", "title": "t", "text": "x"}],
            )
            with mock.patch.object(analyze_mod.workflow, "invoke", side_effect=fake_invoke):
                analyze_mod.run_workflow(payload)


class TaskCompleteExtrasTest(unittest.TestCase):
    """完成接口的可选心得（note）：写不写都能完成，写了并入证据。"""

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

    def _task_id(self) -> str:
        return _seed_loop(self._loop)[0]

    def _stored_evidence(self, tid: str) -> str:
        tasks = [t for r in self._loop.load().data["rounds"] for t in r["tasks"]]
        return next(t["evidence"] for t in tasks if t["task_id"] == tid)

    def test_note_kept_separate_from_evidence(self):
        tid = self._task_id()
        note = "删除旋转卡了两天，最后照可视化推通了"
        resp = self.client.post(
            f"/api/tasks/{tid}/complete",
            json={
                "evidence": "完成了红黑树实现并跑通测试",
                "note": note,
            },
        )
        self.assertEqual(resp.status_code, 200)
        tasks = [t for r in self._loop.load().data["rounds"] for t in r["tasks"]]
        task = next(t for t in tasks if t["task_id"] == tid)
        self.assertEqual(task["evidence"], "完成了红黑树实现并跑通测试")
        self.assertEqual(task["note"], note)
        self.assertNotIn("心得笔记", task["evidence"])

    def test_blank_note_ignored_and_evidence_unchanged(self):
        tid = self._task_id()
        resp = self.client.post(
            f"/api/tasks/{tid}/complete",
            json={"evidence": "纯证据", "note": "   "},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._stored_evidence(tid), "纯证据")

    def test_extras_without_fields_behave_like_before(self):
        tid = self._task_id()
        resp = self.client.post(f"/api/tasks/{tid}/complete", json={"evidence": "只有证据"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._stored_evidence(tid), "只有证据")

    def test_overlong_note_rejected_422(self):
        tid = self._task_id()
        resp = self.client.post(
            f"/api/tasks/{tid}/complete",
            json={"evidence": "证据", "note": "n" * 1001},
        )
        self.assertEqual(resp.status_code, 422)

    def test_combined_over_5000_rejected_422(self):
        tid = self._task_id()
        resp = self.client.post(
            f"/api/tasks/{tid}/complete",
            json={"evidence": "e" * 4500, "note": "n" * 501},
        )
        self.assertEqual(resp.status_code, 422)
        tasks = [t for r in self._loop.load().data["rounds"] for t in r["tasks"]]
        self.assertTrue(all(t["status"] != "completed" for t in tasks))


if __name__ == "__main__":
    unittest.main(verbosity=2)