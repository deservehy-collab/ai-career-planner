"""阶段 5.4：下一轮成长计划生成接口（POST /api/growth/next-round）的隔离测试。

覆盖：
- 正常执行：成长历史与下一轮计划都写入。
- 没有待处理事件：不虚构成长记录 / 轮次。
- 成长评估未产生升级：如实记录，不误报升级。
- 成长记录写入失败：如实报告，不谎报成功。
- 新一轮计划保存失败：如实暴露部分成功（成长已写但轮次未存）。
- 重复触发：依赖 growth_node 指纹去重，不产生重复升级（幂等）。
- 并发：TaskManager 并发上限 1，串行避免重复执行（由 jobs 语义保证，单一队列）。
- 运行结果查询：成功 / 失败均可经 /api/runs/{run_id} 查询。

隔离：
- 每个用例用临时目录构造 LoopStore / GrowthStore，注入 dependency_overrides；
  不触碰项目 runs/。
- 用 mock 替换 workflow 的 invoke（graph.workflow.workflow），避免调用真实 Ollama。
  对"成长历史真实写入 / 指纹去重"用例，用 side_effect 在 mock 内部模拟 growth_node
  的落盘与判重逻辑（基于真实 event_fingerprint），保证毫秒级且不触发真实子代理。
"""

import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from agents import llm  # noqa: E402
from agents.loop_store import LoopStore  # noqa: E402
from agents.growth_store import GrowthStore  # noqa: E402
from agents.growth_rules import event_fingerprint  # noqa: E402
from backend.app import app  # noqa: E402
from backend.deps import create_stores, create_task_manager  # noqa: E402
from backend.jobs import TaskManager  # noqa: E402
from backend.services import Stores, _run_next_round  # noqa: E402


def _seed_loop(loop: LoopStore) -> list[dict]:
    learning = [{
        "topic": "动态规划", "covers": ["Python"], "description": "d",
        "stage": 1, "duration": "1周", "prerequisites": [],
        "method": "学习", "assign": "自己", "assessment": "可实现", "goal": "掌握",
        "status_v2": "pending", "resources": [], "priority": "高",
        "milestones": [], "deliverables": [],
    }]
    res = loop.append_round(target_job="软件工程师", learning_plan=learning,
                            project_plan=[], round_no=1)
    assert res["status"] == "written"
    return loop.load().data["rounds"][0]["tasks"]


def _complete_task(loop: LoopStore, task: dict, evidence="完成动态规划专题并跑通") -> None:
    res = loop.mark_completed(task["task_id"], evidence=evidence)
    assert res["status"] == "completed"


def _base_state(diff=None, with_upgrade=False, with_plan=True):
    """以 mock 支撑 _run_next_round 下探所需字段的伪 workflow 返回值。"""
    return {
        "user_skills": [{"skill": "Python", "level": "进阶", "evidence": ""}],
        "skill_updates": [{"skill": "Python", "from": "基础", "to": "进阶"}] if with_upgrade else [],
        "growth_diff": {
            "history_written": True,
            "history_write_status": "ok",
            "outcome": "upgraded" if with_upgrade else "no_upgrade",
            "events_received": 1,
            "duplicates": [],
            "skipped": [],
            "invalid_events": [],
            "level_changes": ([{"skill": "Python", "from": "基础", "to": "进阶"}]
                               if with_upgrade else []),
            "warnings": [],
            "blocked": [],
            **(diff or {}),
        },
        "learning_plan": [{"topic": "算法", "description": "学习", "stage": 1, "duration": "1周"}]
        if with_plan else [],
        "project_plan": [],
        "job_requirements": [],
        "skill_gaps": [],
    }


def _achieved_state(basis="user_pasted", grounded=True):
    """所有岗位要求都已有直接证据 —— 下一轮应直接收尾，不产出空任务。"""
    state = _base_state(with_plan=False)
    state.update({
        "job_evidence": [],
        "job_requirements": [{
            "skill": "Python", "importance": 4, "level": "中级",
            "evidence_status": "supported",
        }],
        "skill_gaps": [{
            "skill": "Python", "status": "已满足", "display_status": "已满足",
            "verification": "direct", "priority": "低", "importance": 4,
            "supported_by": "Python", "current_level": "中级", "required_level": "中级",
            "gap": "", "evidence_kind": "growth", "evidence_ref": "成长记录",
            "evidence_note": "",
        }],
        "job_requirements_basis": basis,
        "job_requirements_grounded": grounded,
    })
    return state


class NextRoundServiceTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._loop = LoopStore(directory=self._tmp.name)
        self._growth = GrowthStore(directory=self._tmp.name)
        self._stores = Stores(growth=self._growth, loop=self._loop)

    def tearDown(self):
        self._tmp.cleanup()

    def test_empty_pending_no_fake_round(self):
        raw = _run_next_round(self._stores)
        self.assertTrue(raw["ok"])
        self.assertFalse(raw["round_created"])
        self.assertFalse(raw["history_written"])
        self.assertEqual(raw["growth_outcome"], "no_events")
        self.assertFalse(raw["growth_upgraded"])
        self.assertIn("未生成", raw["warning"])
        self.assertEqual(len(self._loop.load().data.get("rounds") or []), 0)
        self.assertEqual(len(self._growth.load().data.get("history") or []), 0)

    def test_normal_round_created_and_history_written(self):
        """mock invoke 内模拟 growth_node 对真实 store 落盘 + 返回升级判定。"""
        task = _seed_loop(self._loop)[0]
        _complete_task(self._loop, task, evidence="证据")

        def fake_invoke(state):
            fp = event_fingerprint("learning", "动态规划", ["Python"])
            cur = self._growth.load().data
            seen = {e.get("fingerprint") for h in (cur.get("history") or [])
                    for e in h["events"]}
            if fp not in seen:
                self._growth.append({
                    "version": "v1",
                    "events": [{"type": "learning", "title": "动态规划",
                                "skills": ["Python"], "evidence": "证据", "fingerprint": fp}],
                    "user_skills_after": [{"skill": "Python", "level": "进阶"}],
                })
            return _base_state(with_upgrade=True)

        with mock.patch("graph.workflow.workflow.invoke", side_effect=fake_invoke):
            raw = _run_next_round(self._stores)
        self.assertTrue(raw["round_created"])
        self.assertIsNotNone(raw["round_no"])
        self.assertTrue(raw["history_written"])
        self.assertEqual(raw["growth_outcome"], "upgraded")
        self.assertTrue(raw["growth_upgraded"])
        self.assertEqual(len(self._growth.load().data.get("history") or []), 1)

    def test_llm_fallback_warning_is_surfaced(self):
        """云端降级到本地时，下一轮结果的 warnings 里必须能看见。"""
        task = _seed_loop(self._loop)[0]
        _complete_task(self._loop, task, evidence="证据")

        def fake_invoke(_state):
            llm._warn_once("云端模型被限流（429），本次已改用本地模型 qwen2.5:3b。")
            return _base_state()

        with mock.patch("graph.workflow.workflow.invoke", side_effect=fake_invoke):
            raw = _run_next_round(self._stores)

        self.assertTrue(any("本地模型" in str(item) for item in raw["warnings"]))

    def test_no_upgrade_not_misreported(self):
        task = _seed_loop(self._loop)[0]
        _complete_task(self._loop, task, evidence="证据")
        with mock.patch("graph.workflow.workflow.invoke",
                        return_value=_base_state(with_upgrade=False)):
            raw = _run_next_round(self._stores)
        self.assertFalse(raw["growth_upgraded"])
        self.assertEqual(raw["growth_outcome"], "no_upgrade")

    def test_growth_history_write_failure(self):
        task = _seed_loop(self._loop)[0]
        _complete_task(self._loop, task, evidence="证据")
        state = _base_state(diff={"history_written": False, "history_write_status": "refused",
                                  "outcome": "store_unreadable"}, with_upgrade=False)
        with mock.patch("graph.workflow.workflow.invoke", return_value=state):
            raw = _run_next_round(self._stores)
        self.assertFalse(raw["history_written"])
        self.assertEqual(raw["history_write_status"], "refused")
        self.assertEqual(len(self._growth.load().data.get("history") or []), 0)

    def test_round_save_failure_partial_success(self):
        task = _seed_loop(self._loop)[0]
        _complete_task(self._loop, task, evidence="证据")
        # 只让 append_round 失败（loop.json 保持可读，否则 pending 重建会提前短路）
        with mock.patch("graph.workflow.workflow.invoke",
                        return_value=_base_state(with_upgrade=False)), \
             mock.patch.object(self._loop, "append_round",
                               return_value={"status": "refused", "warning": "写入被拒"}):
            raw = _run_next_round(self._stores)
        self.assertFalse(raw["round_created"])
        self.assertIsNone(raw["round_no"])
        self.assertTrue(raw["history_written"])
        self.assertIn("部分成功", raw["warning"])

    def test_duplicate_trigger_no_reupgrade(self):
        """同一事件处理一次后再触发 → 指纹已入历史，pending 为空，不重复升级/建轮次。

        这正体现真实闭环的幂等：pending_growth_events 依据指纹对比把"已进成长历史"的
        事件排除在待办之外，因此第二次触发不会再次升级、也不会凭空新建轮次。
        """
        task = _seed_loop(self._loop)[0]
        _complete_task(self._loop, task, evidence="证据")

        fp = event_fingerprint("learning", "动态规划", ["Python"])

        def fake_invoke(state):
            # 模拟 growth_node：指纹未在历史则落盘
            cur = self._growth.load().data
            seen = {e.get("fingerprint") for h in (cur.get("history") or [])
                    for e in h["events"]}
            if fp not in seen:
                self._growth.append({
                    "version": "v1",
                    "events": [{"type": "learning", "title": "动态规划",
                                "skills": ["Python"], "evidence": "证据", "fingerprint": fp}],
                    "user_skills_after": [{"skill": "Python", "level": "进阶"}],
                })
                return _base_state(with_upgrade=True)
            return _base_state(diff={"outcome": "all_duplicates",
                                     "history_written": False,
                                     "history_write_status": None}, with_upgrade=False)

        with mock.patch("graph.workflow.workflow.invoke", side_effect=fake_invoke):
            # 第一次：正常处理
            first = _run_next_round(self._stores)
            # 第二次：事件已成为历史已处理 → 不重复升级/不新建轮次
            second = _run_next_round(self._stores)

        self.assertTrue(first["round_created"])
        self.assertTrue(first["history_written"])
        self.assertEqual(first["growth_outcome"], "upgraded")

        self.assertEqual(second["growth_outcome"], "no_events")
        self.assertFalse(second["round_created"])
        self.assertFalse(second["history_written"])
        self.assertFalse(second["growth_upgraded"])
        # 历史只多一条（第一次写入），第二次未再追加
        self.assertEqual(len(self._growth.load().data.get("history") or []), 1)

    def test_goal_achieved_returns_closure_not_empty_round(self):
        task = _seed_loop(self._loop)[0]
        _complete_task(self._loop, task, evidence="证据")
        with mock.patch("graph.workflow.workflow.invoke",
                        return_value=_achieved_state()):
            raw = _run_next_round(self._stores)

        self.assertTrue(raw["ok"])
        self.assertEqual(raw["goal_status"], "achieved")
        self.assertIn("直接证据", raw["goal"]["message"])
        self.assertFalse(raw["round_created"])
        self.assertIsNone(raw["warning"])
        self.assertEqual(raw["learning_plan"], [])
        self.assertEqual(raw["project_plan"], [])
        # 只有种子轮，没有新建空轮次
        self.assertEqual(len(self._loop.load().data.get("rounds") or []), 1)

    def test_basis_insufficient_is_reported_not_hidden(self):
        task = _seed_loop(self._loop)[0]
        _complete_task(self._loop, task, evidence="证据")
        with mock.patch("graph.workflow.workflow.invoke",
                        return_value=_achieved_state(basis="model_only", grounded=False)):
            raw = _run_next_round(self._stores)

        self.assertEqual(raw["goal_status"], "basis_insufficient")
        self.assertFalse(raw["round_created"])
        self.assertIn("模型推断", raw["warning"])
        self.assertEqual(len(self._loop.load().data.get("rounds") or []), 1)


class NextRoundApiTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._loop = LoopStore(directory=self._tmp.name)
        self._growth = GrowthStore(directory=self._tmp.name)
        self._stores = Stores(growth=self._growth, loop=self._loop)
        self._tm = TaskManager(max_workers=1)
        app.dependency_overrides[create_stores] = lambda: self._stores
        app.dependency_overrides[create_task_manager] = lambda: self._tm
        self.client = TestClient(app)

    def tearDown(self):
        try:
            self._patcher.stop()
        except Exception:
            pass
        app.dependency_overrides.pop(create_stores, None)
        app.dependency_overrides.pop(create_task_manager, None)
        self._tm.shutdown(wait=True)
        self._tmp.cleanup()

    def _wait(self, run_id, timeout=8.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._tm.get(run_id).status in ("succeeded", "failed"):
                return self._tm.get(run_id)
            time.sleep(0.05)
        raise AssertionError("后台任务超时未结束")

    def test_accepted_and_queryable(self):
        task = _seed_loop(self._loop)[0]
        _complete_task(self._loop, task, evidence="证据")
        # mock 需在后台线程执行期间保持生效 —— 直到 _wait 返回后再 stop
        self._patcher = mock.patch("graph.workflow.workflow.invoke",
                                   return_value=_base_state(with_upgrade=False))
        self._patcher.start()
        resp = self.client.post("/api/growth/next-round")
        self.assertEqual(resp.status_code, 202)
        self.assertEqual(resp.json()["status"], "queued")
        run_id = resp.json()["run_id"]
        rec = self._wait(run_id)
        self._patcher.stop()  # 后台执行完毕，安全释放
        self.assertEqual(rec.status, "succeeded")
        q = self.client.get(f"/api/runs/{run_id}")
        self.assertEqual(q.status_code, 200)
        result = q.json()["result"]
        self.assertTrue(result["round_created"])
        self.assertTrue(result["history_written"])

    def test_empty_pending_returns_round_not_created(self):
        self._patcher = mock.patch("graph.workflow.workflow.invoke",
                                   return_value=_base_state(with_upgrade=False))
        self._patcher.start()
        resp = self.client.post("/api/growth/next-round")
        run_id = resp.json()["run_id"]
        rec = self._wait(run_id)
        self._patcher.stop()
        self.assertEqual(rec.status, "succeeded")
        result = self.client.get(f"/api/runs/{run_id}").json()["result"]
        self.assertFalse(result["round_created"])
        self.assertFalse(result["history_written"])
        self.assertEqual(result["growth_outcome"], "no_events")

    def test_run_status_failed_on_internal_error(self):
        task = _seed_loop(self._loop)[0]
        _complete_task(self._loop, task, evidence="证据")
        def boom(*a, **k):
            raise RuntimeError("模拟的 workflow 故障")
        # mock 保持到后台线程失败结束，确保确定为 failed
        self._patcher = mock.patch("graph.workflow.workflow.invoke", side_effect=boom)
        self._patcher.start()
        resp = self.client.post("/api/growth/next-round")
        run_id = resp.json()["run_id"]
        rec = self._wait(run_id)
        self._patcher.stop()
        self.assertEqual(rec.status, "failed")
        self.assertIsNotNone(rec.error)
        q = self.client.get(f"/api/runs/{run_id}")
        self.assertEqual(q.status_code, 200)
        bad = self.client.get(f"/api/runs/{run_id}0")
        self.assertEqual(bad.status_code, 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)