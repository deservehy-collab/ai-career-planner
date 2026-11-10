"""Project Agent 的岗位领域适配：非技术岗不塞软件技术栈，stage 统一成「阶段 N」。"""

import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agents import project_agent as PA  # noqa: E402


def learning_plan():
    return [
        {"stage": 1, "topic": "会计准则与报表", "covers": ["会计准则", "财务报表分析"]},
        {"stage": 2, "topic": "审计与税务", "covers": ["审计", "税务管理"]},
    ]


def accounting_gaps():
    return [
        {"skill": "会计准则", "status": "明显不足", "priority": "高"},
        {"skill": "财务报表分析", "status": "明显不足", "priority": "高"},
        {"skill": "审计", "status": "明显不足", "priority": "高"},
        {"skill": "税务管理", "status": "明显不足", "priority": "高"},
        {"skill": "Excel", "status": "已满足", "priority": "低"},
    ]


def make_project(stage, tech_stack):
    return PA.ProjectItem(
        name="财务核算模拟",
        goal="补会计准则与报表分析",
        covers_gaps=["会计准则", "财务报表分析"],
        tech_stack=tech_stack,
        stage=stage,
        deliverable="一套核算底稿与财务报表分析说明",
    )


class TestSoftwareTechGrounding(unittest.TestCase):
    def test_non_technical_job_strips_ungrounded_python(self):
        projects = [{
            "name": "财务核算模拟",
            "tech_stack": ["Python", "Excel", "审计"],
        }]
        notes = PA.strip_ungrounded_software_tech(
            projects, "会计", accounting_gaps(), learning_plan(), [{"skill": "Excel"}]
        )
        self.assertEqual(projects[0]["tech_stack"], ["Excel", "审计"])
        self.assertTrue(notes)

    def test_technical_job_keeps_stack_even_if_not_in_gaps(self):
        projects = [{
            "name": "RAG 问答系统",
            "tech_stack": ["Python", "LangChain"],
        }]
        notes = PA.strip_ungrounded_software_tech(
            projects, "AI 应用开发工程师", [{"skill": "RAG"}], [], []
        )
        self.assertEqual(projects[0]["tech_stack"], ["Python", "LangChain"])
        self.assertEqual(notes, [])

    def test_grounded_software_kept_for_non_technical_job(self):
        projects = [{"name": "会计信息化", "tech_stack": ["Python", "Excel"]}]
        gaps = [{"skill": "Python"}, {"skill": "会计准则"}]
        notes = PA.strip_ungrounded_software_tech(projects, "会计", gaps, [], [])
        # 岗位/差距里明确要求 Python，按技术岗处理，不动技术栈
        self.assertEqual(projects[0]["tech_stack"], ["Python", "Excel"])
        self.assertEqual(notes, [])

    def test_all_removed_leaves_empty_and_explains(self):
        projects = [{"name": "财务核算模拟", "tech_stack": ["Python", "Git"]}]
        notes = PA.strip_ungrounded_software_tech(
            projects, "会计", [{"skill": "会计准则"}], [], []
        )
        self.assertEqual(projects[0]["tech_stack"], [])
        self.assertTrue(any("已无技术栈" in note for note in notes))


class TestStageNormalization(unittest.TestCase):
    def test_english_stage_mapped_by_number(self):
        projects = [{"name": "A", "stage": "sync with stage 1", "covers_gaps": ["会计准则"]}]
        notes = PA.normalize_project_stages(projects, learning_plan())
        self.assertEqual(projects[0]["stage"], "阶段 1")
        self.assertTrue(notes)

    def test_stage_without_space_is_normalized(self):
        projects = [{"name": "B", "stage": "阶段2", "covers_gaps": ["审计"]}]
        PA.normalize_project_stages(projects, learning_plan())
        self.assertEqual(projects[0]["stage"], "阶段 2")

    def test_stage_inferred_from_covers_when_no_number(self):
        projects = [{"name": "C", "stage": "审计阶段", "covers_gaps": ["审计", "税务管理"]}]
        PA.normalize_project_stages(projects, learning_plan())
        self.assertEqual(projects[0]["stage"], "阶段 2")

    def test_stage_falls_back_to_project_order(self):
        projects = [
            {"name": "D", "stage": "第一阶段", "covers_gaps": ["无关技能"]},
            {"name": "E", "stage": "第二阶段", "covers_gaps": ["另一个无关技能"]},
        ]
        PA.normalize_project_stages(projects, learning_plan())
        self.assertEqual([p["stage"] for p in projects], ["阶段 1", "阶段 2"])

    def test_no_learning_plan_leaves_stage_untouched(self):
        projects = [{"name": "F", "stage": "阶段2"}]
        PA.normalize_project_stages(projects, [])
        self.assertEqual(projects[0]["stage"], "阶段2")


class TestProjectNodeAppliesDomainRules(unittest.TestCase):
    def test_accounting_project_is_domain_appropriate(self):
        captured = {}

        def fake(prompt, schema):
            captured["prompt"] = prompt
            return PA.ProjectPlan(projects=[
                make_project("sync with stage 1", ["Python", "Excel", "审计"]),
            ])

        state = {
            "target_job": "会计",
            "skill_gaps": accounting_gaps(),
            "learning_plan": learning_plan(),
            "user_skills": [{"skill": "Excel", "level": "中级"}],
        }
        with mock.patch.object(PA, "structured_invoke", fake):
            out = PA.project_node(state)

        project = out["project_plan"][0]
        self.assertEqual(project["tech_stack"], ["Excel", "审计"])
        self.assertEqual(project["stage"], "阶段 1")
        self.assertIn("非技术岗", captured["prompt"])
        self.assertIn("禁止", captured["prompt"])


if __name__ == "__main__":
    unittest.main()
