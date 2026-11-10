"""计划快照 / 新计划预览 / 断点恢复 / 零基线路径的隔离测试。

覆盖
----
- ``run_analyze_and_save``：首次建轮次；已有轮次默认只预览；显式 ``new_plan``
  追加新轮次且旧轮次原样保留；每轮都写 ``snapshot`` 与顶层 ``latest_goal``。
- 下一轮沿用上一轮快照：注入的岗位要求 / 原文 / 依据档位不再让模型重算，
  新轮次写回同一份依据。
- 目标或原文与当前计划不一致：**跑模型之前**直接拒绝，磁盘一个字节不动。
- 恢复标记：轮次写盘失败后，再点一次直接补存已算好的计划，不重复跑模型。
- 结局持久化：本轮无需新计划时，``latest_goal`` 仍写入并在 ``list_tasks`` 可见。
- 成长历史不可读时，``clear_tasks`` 拒绝清空。
- 零基础：profile / assessment 在没有任何自述信息时不调模型，产出确定性的
  「明显不足」缺口。
- retrieval / job 节点复用快照，且不触发模型调用。

隔离：全部使用 ``tempfile``；mock 掉 ``workflow.invoke``；不触碰项目 ``runs/``。
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agents.assessment_agent import assessment_node  # noqa: E402
from agents.goal import evaluate_goal  # noqa: E402
from agents.growth_store import GrowthStore  # noqa: E402
from agents.job_agent import job_node  # noqa: E402
from agents.loop_store import LoopStore  # noqa: E402
from agents.profile_agent import profile_node  # noqa: E402
from agents.retrieval_agent import retrieval_node  # noqa: E402
from backend import schemas  # noqa: E402
from backend.services import (  # noqa: E402
    CLEAR_REFUSED,
    Stores,
    _run_next_round,
    clear_tasks,
    list_tasks,
    run_analyze_and_save,
)

JD_TEXT = "岗位要求：熟悉 Python 编程，能够独立完成数据清洗与分析。"


def _learning_plan(topic="动态规划", skill="Python"):
    return [{
        "topic": topic, "covers": [skill], "description": "学习并练习",
        "stage": 1, "duration": "1周", "prerequisites": [],
        "method": "学习", "assign": "自己", "assessment": "可独立实现", "goal": "掌握",
        "status_v2": "pending", "resources": [], "priority": "高",
        "milestones": [], "deliverables": [],
    }]


def _requirements():
    return [{
        "skill": "Python", "importance": 4, "level": "中级",
        "reason": "原文要求熟悉 Python", "evidence_status": "supported",
        "source_notes": ["熟悉 Python 编程"],
    }]


def _snapshot(target_job="AI工程师"):
    return {
        "jd_text": JD_TEXT,
        "job_evidence": [{"skill": "Python", "note": "熟悉 Python 编程",
                          "provenance": "user_jd"}],
        "retrieval_status": "user_pasted",
        "retrieval_diagnostics": {"reason": "招聘方要求的原文，一句一条"},
        "job_requirements": _requirements(),
        "job_requirements_basis": "user_pasted",
        "job_requirements_grounded": True,
        "goal": {"status": "in_progress", "message": "还有 1 项未满足"},
        "goal_status": "in_progress",
        "goal_evaluated_at": "2026-10-23T10:00:00",
    }


def _seed_round(loop: LoopStore, target_job="AI工程师", snapshot=None):
    result = loop.append_round(
        target_job,
        _learning_plan(),
        [],
        snapshot=snapshot if snapshot is not None else _snapshot(target_job),
        source="analyze",
        latest_goal={"status": "in_progress", "message": "还有 1 项未满足"},
    )
    assert result["status"] == "written", result
    return loop.load().data["rounds"][-1]


def _complete(loop: LoopStore, task: dict):
    result = loop.mark_completed(task["task_id"], evidence="完成动态规划专题并跑通")
    assert result["status"] == "completed", result


def _analysis_state(topic="动态规划"):
    return {
        "user_skills": [],
        "user_strengths": [],
        "user_weaknesses": [],
        "skill_updates": [],
        "job_requirements": _requirements(),
        "job_requirements_basis": "user_pasted",
        "job_requirements_grounded": True,
        "job_evidence": [{"skill": "Python", "note": "熟悉 Python 编程",
                          "provenance": "user_jd"}],
        "retrieval_status": "user_pasted",
        "retrieval_diagnostics": {"reason": "招聘方要求的原文，一句一条"},
        "skill_gaps": [{
            "skill": "Python", "status": "明显不足", "display_status": "明显不足",
            "verification": "unverified", "priority": "高", "importance": 4,
            "supported_by": "无", "current_level": "无", "required_level": "中级",
            "gap": "当前完全没有这项能力",
        }],
        "learning_plan": _learning_plan(topic),
        "project_plan": [],
    }


def _next_round_state():
    state = _analysis_state()
    state.update({
        "growth_diff": {
            "history_written": True,
            "history_write_status": "ok",
            "outcome": "no_upgrade",
            "events_received": 1,
            "duplicates": [],
            "skipped": [],
            "invalid_events": [],
            "level_changes": [],
            "warnings": [],
            "blocked": [],
        },
    })
    return state


class AnalyzePreviewTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.loop = LoopStore(directory=self._tmp.name)
        self.growth = GrowthStore(directory=self._tmp.name)
        self.stores = Stores(growth=self.growth, loop=self.loop)

    def tearDown(self):
        self._tmp.cleanup()

    def _payload(self, new_plan=False):
        return schemas.AnalyzeRequest(
            user_profile=schemas.UserProfileRequest(
                major="软件工程", grade="大三", skills=[], projects=[],
            ),
            target_job="AI工程师",
            jd_text=JD_TEXT,
            new_plan=new_plan,
        )

    def test_preview_does_not_touch_rounds_and_new_plan_appends(self):
        with mock.patch("backend.analyze.workflow.invoke",
                        return_value=_analysis_state()):
            first = run_analyze_and_save(self.stores, self._payload())
            self.assertTrue(first["round_created"])
            self.assertEqual(first["round_no"], 1)
            rounds = self.loop.load().data["rounds"]
            self.assertEqual(len(rounds), 1)
            snapshot = rounds[0]["snapshot"]
            self.assertEqual(snapshot["jd_text"], JD_TEXT)
            self.assertEqual(snapshot["job_requirements_basis"], "user_pasted")
            self.assertEqual(snapshot["goal_status"], "in_progress")
            self.assertEqual(rounds[0]["source"], "analyze")

            preview = run_analyze_and_save(self.stores, self._payload())
            self.assertFalse(preview["round_created"])
            self.assertIsNone(preview["round_no"])
            self.assertEqual(preview["existing_round_no"], 1)
            self.assertIn("预览", preview["warning"])
            self.assertEqual(len(self.loop.load().data["rounds"]), 1,
                             "预览不许新增轮次")

            second = run_analyze_and_save(self.stores, self._payload(new_plan=True))

        self.assertTrue(second["round_created"])
        self.assertEqual(second["round_no"], 2)
        rounds = self.loop.load().data["rounds"]
        self.assertEqual(len(rounds), 2, "新计划必须保留旧轮次")
        self.assertEqual(rounds[0]["target_job"], "AI工程师")
        self.assertEqual(rounds[1]["target_job"], "AI工程师")
        self.assertEqual(rounds[1]["source"], "new_plan")
        self.assertEqual(rounds[1]["snapshot"]["jd_text"], JD_TEXT)
        latest_goal = self.loop.latest_goal()
        self.assertEqual(latest_goal["status"], "in_progress")
        self.assertEqual(latest_goal["round"], 2)
        self.assertEqual(list_tasks(self.stores)["latest_goal"]["round"], 2)

    def test_second_round_keeps_tasks_of_first_round(self):
        with mock.patch("backend.analyze.workflow.invoke",
                        return_value=_analysis_state()):
            run_analyze_and_save(self.stores, self._payload())
            run_analyze_and_save(self.stores, self._payload(new_plan=True))

        rounds = self.loop.load().data["rounds"]
        self.assertEqual(len(rounds[0]["tasks"]), 1)
        self.assertEqual(len(rounds[1]["tasks"]), 1)
        self.assertNotEqual(rounds[0]["tasks"][0]["task_id"],
                            rounds[1]["tasks"][0]["task_id"])
        self.assertEqual(rounds[0]["tasks"][0]["status"], "pending")
        self.assertEqual(rounds[1]["tasks"][0]["status"], "pending")


class NextRoundSnapshotTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.loop = LoopStore(directory=self._tmp.name)
        self.growth = GrowthStore(directory=self._tmp.name)
        self.stores = Stores(growth=self.growth, loop=self.loop)
        seeded = _seed_round(self.loop)
        _complete(self.loop, seeded["tasks"][0])

    def tearDown(self):
        self._tmp.cleanup()

    def test_next_round_reuses_snapshot_and_writes_it_back(self):
        captured = {}

        def fake_invoke(state):
            captured.update(state)
            return _next_round_state()

        with mock.patch("backend.services.workflow.invoke", side_effect=fake_invoke):
            result = _run_next_round(self.stores)

        self.assertTrue(result["ok"])
        self.assertTrue(result["round_created"])
        self.assertEqual(result["round_no"], 2)
        self.assertTrue(result["requirements_reused"])
        self.assertEqual(result["plan_snapshot_round"], 1)
        self.assertTrue(any("沿用第 1 轮" in item for item in result["warnings"]))

        self.assertEqual(captured["target_job"], "AI工程师")
        self.assertEqual(captured["jd_text"], JD_TEXT)
        self.assertEqual(captured["snapshot_job_requirements"], _requirements())
        self.assertEqual(captured["snapshot_job_requirements_basis"], "user_pasted")
        self.assertTrue(captured["snapshot_job_requirements_grounded"])
        self.assertEqual(captured["snapshot_job_evidence"][0]["note"], "熟悉 Python 编程")

        rounds = self.loop.load().data["rounds"]
        self.assertEqual(len(rounds), 2)
        snapshot = rounds[1]["snapshot"]
        self.assertEqual(snapshot["jd_text"], JD_TEXT)
        self.assertEqual(snapshot["job_requirements"], _requirements())
        self.assertEqual(snapshot["job_requirements_basis"], "user_pasted")

    def test_target_mismatch_rejected_before_model(self):
        before = self.loop.path.read_bytes()
        with mock.patch("backend.services.workflow.invoke",
                        side_effect=AssertionError("不一致时不该跑模型")) as invoke:
            result = _run_next_round(self.stores, target_job="产品经理")

        self.assertFalse(result["ok"])
        self.assertFalse(result["round_created"])
        self.assertEqual(result["growth_outcome"], "target_mismatch")
        self.assertIn("AI工程师", result["warning"])
        self.assertIn("产品经理", result["warning"])
        self.assertEqual(invoke.call_count, 0)
        self.assertEqual(self.loop.path.read_bytes(), before,
                         "拒绝时一个字节都不许动")
        self.assertIsNone(self.loop.pending_next_round())
        self.assertFalse(self.growth.path.exists())

    def test_jd_mismatch_rejected_before_model(self):
        before = self.loop.path.read_bytes()
        with mock.patch("backend.services.workflow.invoke",
                        side_effect=AssertionError("不一致时不该跑模型")) as invoke:
            result = _run_next_round(
                self.stores, target_job="AI工程师", jd_text="另一份完全不同的招聘要求"
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["growth_outcome"], "jd_mismatch")
        self.assertIn("招聘要求原文", result["warning"])
        self.assertEqual(invoke.call_count, 0)
        self.assertEqual(self.loop.path.read_bytes(), before)
        self.assertIsNone(self.loop.pending_next_round())

    def test_recovery_resaves_plans_without_rerunning_model(self):
        real_append = self.loop.append_round
        append_calls = {"n": 0}
        invoke_calls = {"n": 0}

        def flaky_append(*args, **kwargs):
            append_calls["n"] += 1
            if append_calls["n"] == 1:
                return {"status": "refused", "warning": "模拟磁盘写入失败"}
            return real_append(*args, **kwargs)

        def fake_invoke(_state):
            invoke_calls["n"] += 1
            return _next_round_state()

        with mock.patch.object(self.loop, "append_round", side_effect=flaky_append), \
             mock.patch("backend.services.workflow.invoke", side_effect=fake_invoke):
            first = _run_next_round(self.stores)
            marker = self.loop.pending_next_round()
            second = _run_next_round(self.stores)

        self.assertTrue(first["ok"])
        self.assertFalse(first["round_created"])
        self.assertIn("恢复标记", first["warning"])
        self.assertIsNotNone(marker)
        self.assertTrue(marker.get("plans"))

        self.assertTrue(second["ok"])
        self.assertTrue(second["round_created"])
        self.assertTrue(second["recovered"])
        self.assertEqual(invoke_calls["n"], 1, "补存不许重新跑模型")
        self.assertEqual(len(self.loop.load().data["rounds"]), 2)
        self.assertIsNone(self.loop.pending_next_round())
        self.assertTrue(any("补存成功" in item for item in second["warnings"]))
        self.assertEqual(self.loop.latest_goal()["round"], 2)

    def test_goal_is_persisted_when_no_plan_is_needed(self):
        achieved = _analysis_state()
        achieved.update({
            "learning_plan": [],
            "project_plan": [],
            "skill_gaps": [{
                "skill": "Python", "status": "已满足", "display_status": "已满足",
                "verification": "direct", "priority": "低", "importance": 4,
                "supported_by": "Python", "current_level": "中级",
                "required_level": "中级", "gap": "", "evidence_kind": "growth",
                "evidence_ref": "成长记录", "evidence_note": "",
            }],
            "growth_diff": {"history_written": True, "history_write_status": "ok",
                            "outcome": "no_upgrade"},
        })
        with mock.patch("backend.services.workflow.invoke", return_value=achieved):
            result = _run_next_round(self.stores)

        self.assertEqual(result["goal_status"], "achieved")
        self.assertFalse(result["round_created"])
        self.assertIsNone(result["warning"])
        self.assertEqual(len(self.loop.load().data["rounds"]), 1,
                         "结局分支不许凭空建空轮次")
        latest = self.loop.latest_goal()
        self.assertEqual(latest["status"], "achieved")
        self.assertEqual(latest["round"], 1)
        self.assertEqual(latest["target_job"], "AI工程师")

        listing = list_tasks(self.stores)
        self.assertEqual(listing["latest_goal"]["status"], "achieved")
        self.assertEqual(listing["rounds"][0]["round"], 1)
        self.assertIsNone(listing["pending_next_round"])


class ClearTasksUnreadableGrowthTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.loop = LoopStore(directory=self._tmp.name)
        self.growth = GrowthStore(directory=self._tmp.name)
        self.stores = Stores(growth=self.growth, loop=self.loop)

    def tearDown(self):
        self._tmp.cleanup()

    def test_refuses_when_growth_history_unreadable(self):
        _seed_round(self.loop)
        self.growth.path.write_text("{坏了", encoding="utf-8")
        loop_before = self.loop.path.read_bytes()
        growth_before = self.growth.path.read_bytes()

        result = clear_tasks(self.stores, drop_pending_events=True)

        self.assertEqual(result["status"], CLEAR_REFUSED)
        self.assertEqual(result["rounds_removed"], 0)
        self.assertIsNone(result["backup"])
        self.assertIn("成长历史", result["warning"])
        self.assertEqual(self.loop.path.read_bytes(), loop_before,
                         "判不了记账状态时不许动任务清单")
        self.assertEqual(self.growth.path.read_bytes(), growth_before)


class ZeroBaselineTest(unittest.TestCase):
    def test_profile_does_not_call_model_without_any_input(self):
        with mock.patch("agents.profile_agent.structured_invoke",
                        side_effect=AssertionError("零基础不该调模型")):
            update = profile_node({"user_profile": {
                "major": "", "grade": "", "skills": [], "projects": [],
            }})

        self.assertEqual(update["user_skills"], [])
        self.assertEqual(update["user_strengths"], [])
        self.assertTrue(any("零基础" in item for item in update["user_weaknesses"]))

    def test_assessment_lists_all_requirements_as_insufficient(self):
        requirements = [
            {"skill": "Python", "importance": 5, "level": "中级"},
            {"skill": "数据分析", "importance": 3, "level": "初级"},
            {"skill": "沟通", "importance": 1, "level": "初级"},
        ]
        with mock.patch("agents.assessment_agent.structured_invoke",
                        side_effect=AssertionError("零基础不该调模型")):
            update = assessment_node({"user_skills": [],
                                      "job_requirements": requirements})

        gaps = update["skill_gaps"]
        self.assertEqual(len(gaps), 3)
        self.assertEqual(update["assessment_answers"], [])
        by_skill = {gap["skill"]: gap for gap in gaps}
        for gap in gaps:
            self.assertEqual(gap["status"], "明显不足")
            self.assertEqual(gap["display_status"], "明显不足")
            self.assertEqual(gap["verification"], "unverified")
            self.assertEqual(gap["supported_by"], "无")
            self.assertEqual(gap["current_level"], "无")
            self.assertEqual(gap["evidence_kind"], "none")
            self.assertTrue(gap["gap"])
        self.assertEqual([gap["skill"] for gap in gaps], ["Python", "数据分析", "沟通"],
                         "缺口必须按优先级排序")
        self.assertEqual(by_skill["Python"]["required_level"], "中级")
        self.assertEqual(by_skill["Python"]["priority"], "高")

        goal = evaluate_goal({
            "skill_gaps": gaps,
            "job_requirements": requirements,
            "job_requirements_basis": "user_pasted",
            "job_requirements_grounded": True,
        })
        self.assertEqual(goal["status"], "in_progress")
        self.assertEqual(goal["remaining_count"], 3)


class SnapshotAgentReuseTest(unittest.TestCase):
    def test_retrieval_reuses_snapshot_without_chunking(self):
        with mock.patch("agents.retrieval_agent.chunk_user_jd",
                        side_effect=AssertionError("快照存在时不该重新切句")):
            update = retrieval_node({
                "target_job": "AI工程师",
                "snapshot_job_evidence": [{"skill": "Python", "note": "熟悉 Python"}],
                "snapshot_retrieval_status": "user_pasted",
                "snapshot_retrieval_diagnostics": {"reason": "原话"},
            })

        self.assertEqual(update["job_evidence"], [{"skill": "Python", "note": "熟悉 Python"}])
        self.assertEqual(update["retrieval_status"], "user_pasted")
        self.assertTrue(update["retrieval_diagnostics"]["reused_from_snapshot"])

    def test_job_reuses_snapshot_without_model(self):
        requirements = _requirements()
        with mock.patch("agents.job_agent.structured_invoke",
                        side_effect=AssertionError("快照存在时不该重新生成要求")):
            update = job_node({
                "target_job": "护士",
                "job_evidence": [{"skill": "Python", "provenance": "user_jd"}],
                "retrieval_status": "user_pasted",
                "snapshot_job_requirements": requirements,
                "snapshot_job_requirements_basis": "user_pasted",
                "snapshot_job_requirements_grounded": True,
            })

        self.assertEqual(update["job_requirements"], requirements)
        self.assertEqual(update["job_requirements_basis"], "user_pasted")
        self.assertTrue(update["job_requirements_grounded"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
