"""非 AI 岗位能否记进成长记录的回归测试。

背景（这一条链原来整条是断的）：技能词表 ``agents/skill_dict.py`` 只收录
「AI 应用工程师」那十几项技能名，而 covers 折算与成长记账**两道**都拿它当过滤器用。
结果是电气、护理这类岗位的每一条成果都被判成"任务没有关联技能"或"技能名无法归一"，
学习任务提交多少次都写不进成长记录。

覆盖：
- ``normalize_skills``：词表没收录 → 按原名记账并如实报告；有歧义 → 仍然丢弃。
- 电气岗位的一轮任务（学习 + 项目）→ 能构成成长成果、能算出升级。
- AI 岗位的既有口径不回归（唯一命中折算规范名、Python 照旧）。
- 词表加载失败（vocabulary=None）时仍然全部列进未识别清单，不放行。

隔离：临时目录构造 LoopStore / GrowthStore，不调模型、不连 Ollama、不碰项目 runs/。
"""

import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agents import growth_loop as GL  # noqa: E402
from agents.growth_rules import normalize_skills  # noqa: E402
from agents.growth_store import GrowthStore  # noqa: E402
from agents.loop_store import LoopStore  # noqa: E402
from agents.skill_dict import SkillVocabulary  # noqa: E402

VOCAB = SkillVocabulary()

# 词表里没有、但确实是某个真实岗位缺口的技能名
ELEC = "电气控制与自动化"
CIRCUIT = "电路设计与分析"
# 同时沾上 Prompt Engineering 与 Embedding：认不准，必须继续丢弃
AMBIGUOUS = "提示词与向量检索"


def _learning(topic, covers):
    return {
        "topic": topic, "covers": covers, "description": "d", "stage": 1,
        "duration": "1周", "prerequisites": [], "method": "学习", "assign": "自己",
        "assessment": "可实现", "goal": "掌握", "status_v2": "pending",
        "resources": [], "priority": "高", "milestones": [], "deliverables": [],
    }


def _project(name, covers_gaps):
    return {
        "name": name, "covers_gaps": covers_gaps, "description": "做一个闭环",
        "deliverables": ["可运行的设计说明书"],
    }


def _seed(loop, learning, projects=(), target_job="电气工程师"):
    res = loop.append_round(target_job=target_job, learning_plan=learning,
                            project_plan=list(projects), round_no=1)
    assert res["status"] == "written", res
    return loop.load().data["rounds"][0]["tasks"]


class NormalizeSkillsTest(unittest.TestCase):
    def test_skill_outside_vocabulary_is_kept_by_original_name(self):
        canonical, unmapped = normalize_skills([ELEC, CIRCUIT], VOCAB)
        self.assertEqual(canonical, [ELEC, CIRCUIT])
        self.assertEqual(unmapped, [ELEC, CIRCUIT])   # 照常报告，不是悄悄放行

    def test_ambiguous_skill_is_still_dropped(self):
        canonical, unmapped = normalize_skills([AMBIGUOUS], VOCAB)
        self.assertEqual(canonical, [])
        self.assertEqual(unmapped, [AMBIGUOUS])

    def test_unique_vocabulary_hit_is_unchanged(self):
        canonical, unmapped = normalize_skills(["后端服务", "Python"], VOCAB)
        self.assertEqual(canonical, ["后端 API 开发", "Python"])
        self.assertEqual(unmapped, [])

    def test_missing_vocabulary_books_nothing(self):
        """词表都拿不到时不放宽 —— 与以往一致，全部进未识别清单。"""
        canonical, unmapped = normalize_skills([ELEC, "Python"], None)
        self.assertEqual(canonical, [])
        self.assertEqual(unmapped, [ELEC, "Python"])

    def test_gap_number_is_never_a_skill_name(self):
        """模型把编号当技能名传下来时不记账，别给人记一档叫"1"的技能。"""
        canonical, unmapped = normalize_skills(["1", "2"], VOCAB)
        self.assertEqual(canonical, [])
        self.assertEqual(unmapped, ["1", "2"])

    def test_mixed_keeps_resolvable_and_reports_the_rest(self):
        canonical, unmapped = normalize_skills([AMBIGUOUS, ELEC], VOCAB)
        self.assertEqual(canonical, [ELEC])
        self.assertEqual(unmapped, [AMBIGUOUS, ELEC])


class ElectricalRoundTripTest(unittest.TestCase):
    """非 AI 岗位走完整闭环：完成任务 → 构成成果 → 算出升级。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.loop = LoopStore(directory=self._tmp.name)
        self.growth = GrowthStore(directory=self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_learning_task_forms_a_growth_event(self):
        tasks = _seed(self.loop, [_learning("电气一次系统设计", [ELEC, CIRCUIT])])
        res = GL.process_task_completion(tasks[0]["task_id"], evidence="画完了一份接线图",
                                         loop_store=self.loop, growth_store=self.growth,
                                         vocabulary=VOCAB)
        self.assertEqual(res["growth_event_status"], "ok")
        self.assertIn(res["status"], ("completed_upgraded", "completed_no_upgrade"))
        self.assertEqual(res["growth_event"]["skills"], [ELEC, CIRCUIT])
        self.assertTrue(res["growth_evaluated"])

    def test_unlinked_learning_task_still_refuses_to_book(self):
        """covers 真的空（C 门禁之前的历史数据）时，仍旧一条也构不成。"""
        tasks = _seed(self.loop, [_learning("没有技能的任务", [])])
        res = GL.process_task_completion(tasks[0]["task_id"], evidence="做完了",
                                         loop_store=self.loop, growth_store=self.growth,
                                         vocabulary=VOCAB)
        self.assertIsNone(res["growth_event"])
        self.assertEqual(res["growth_event_status"], "no_skills")

    def test_electrical_skills_can_be_upgraded(self):
        tasks = _seed(self.loop, [_learning("电气一次系统设计", [ELEC])])
        res = GL.process_task_completion(tasks[0]["task_id"], evidence="接线图通过老师审核",
                                         loop_store=self.loop, growth_store=self.growth,
                                         vocabulary=VOCAB)
        updates = {item["skill"]: item for item in res["updates"]}
        self.assertIn(ELEC, updates)
        self.assertEqual(updates[ELEC]["from"], None)     # 此前缺席，绝不写"无"
        self.assertEqual(updates[ELEC]["to"], "入门")      # 学习成果封顶初级，先升一档
        self.assertFalse(updates[ELEC]["covered_by_knowledge_base"])
        self.assertEqual(updates[ELEC]["gate_status"], "not_covered")

    def test_pending_events_list_electrical_round(self):
        tasks = _seed(self.loop,
                      [_learning("电气一次系统设计", [ELEC]), _learning("继保", [CIRCUIT])],
                      [_project("智能家居控制系统", [ELEC])])
        for task in tasks:
            self.loop.mark_completed(task["task_id"], evidence="做完了并自测通过")
        pending = GL.pending_growth_events(loop_store=self.loop, growth_store=self.growth,
                                           vocabulary=VOCAB)
        self.assertEqual(len(pending["events"]), 3, pending["skipped"])
        self.assertEqual(pending["skipped"], [])

    def test_ambiguous_task_skill_is_not_booked(self):
        tasks = _seed(self.loop, [_learning("含混说法", [AMBIGUOUS])])
        self.loop.mark_completed(tasks[0]["task_id"], evidence="做完了")
        pending = GL.pending_growth_events(loop_store=self.loop, growth_store=self.growth,
                                           vocabulary=VOCAB)
        self.assertEqual(pending["events"], [])
        self.assertEqual(pending["skipped"][0]["status"], "skills_unmapped")
        self.assertIn("歧义", pending["skipped"][0]["reason"])


class AiTargetRegressionTest(unittest.TestCase):
    def test_ai_target_pending_uses_canonical_names(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        loop = LoopStore(directory=tmp.name)
        tasks = _seed(loop, [_learning("红黑树", ["数据结构与算法"])],
                      target_job="AI 应用工程师")
        loop.mark_completed(tasks[0]["task_id"], evidence="实现并跑通 20 条测试")
        pending = GL.pending_growth_events(loop_store=loop,
                                           growth_store=GrowthStore(directory=tmp.name),
                                           vocabulary=VOCAB)
        self.assertEqual(pending["events"][0]["skills"], ["数据结构与算法"])
        self.assertEqual(pending["skipped"], [])


if __name__ == "__main__":
    unittest.main()
