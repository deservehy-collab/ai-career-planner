"""多领域项目模板：领域过滤、提示词注入与 project_node 端到端（模拟模型）。

真实知识库只读；模型调用用 mock 替换，不连 Ollama、不写用户目录。
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agents import project_agent as PA  # noqa: E402
from agents.project_knowledge import ProjectKnowledgeBase  # noqa: E402


def _names(items):
    return [p["name"] for p in items]


ACCOUNTING_GAPS = [
    {"skill": "会计准则", "status": "明显不足", "priority": "高"},
    {"skill": "财务报表分析", "status": "明显不足", "priority": "高"},
    {"skill": "成本核算", "status": "部分满足", "priority": "中"},
]
OPERATIONS_GAPS = [
    {"skill": "内容策划", "status": "明显不足", "priority": "高"},
    {"skill": "文案写作", "status": "部分满足", "priority": "中"},
    {"skill": "运营数据分析", "status": "明显不足", "priority": "高"},
]
TEACHING_GAPS = [
    {"skill": "教学设计", "status": "明显不足", "priority": "高"},
    {"skill": "学情分析", "status": "明显不足", "priority": "高"},
    {"skill": "命题与作业设计", "status": "部分满足", "priority": "中"},
]


class TestProjectDomainFilter(unittest.TestCase):
    def setUp(self):
        self.base = ProjectKnowledgeBase()

    def test_accounting_only_gets_accounting_templates(self):
        executable, blocked = self.base.recommend_for_user(
            ["会计准则", "财务报表分析", "成本核算"],
            {"会计准则": "初级", "Excel": "中级"},
            domain="accounting",
        )
        everything = executable + blocked
        self.assertTrue(everything)
        self.assertTrue(all(p.get("domain") == "accounting" for p in everything))
        self.assertIn("月度账务处理与报表编制", _names(executable))
        self.assertNotIn("AI 智能问答助手", _names(executable))
        self.assertNotIn("RAG 知识库问答系统", _names(blocked))

    def test_operations_only_gets_operations_templates(self):
        executable, blocked = self.base.recommend_for_user(
            ["内容策划", "文案写作", "运营数据分析"],
            {"文案写作": "初级", "运营数据分析": "初级", "Excel": "中级"},
            domain="operations",
        )
        everything = executable + blocked
        self.assertTrue(all(p.get("domain") == "operations" for p in everything))
        self.assertIn("账号定位与内容选题库", _names(executable))
        self.assertNotIn("月度账务处理与报表编制", _names(executable))

    def test_teaching_only_gets_teaching_templates(self):
        executable, blocked = self.base.recommend_for_user(
            ["教学设计", "学情分析", "命题与作业设计"],
            {"学科专业知识": "中级", "教育心理学": "初级"},
            domain="teaching",
        )
        everything = executable + blocked
        self.assertTrue(all(p.get("domain") == "teaching" for p in everything))
        self.assertIn("单元教学设计作品集", _names(executable))
        self.assertNotIn("账号定位与内容选题库", _names(executable))

    def test_ai_job_still_gets_ai_templates(self):
        executable, blocked = self.base.recommend_for_user(
            ["Python", "RAG"], {"Python": "中级", "RAG": "中级"}, domain="ai-application"
        )
        everything = executable + blocked
        self.assertTrue(all(p.get("domain") == "ai-application" for p in everything))
        self.assertIn("RAG 知识库问答系统", _names(executable))

    def test_no_domain_keeps_legacy_behavior(self):
        executable, _ = self.base.recommend_for_user(["Python", "RAG"], None)
        self.assertIn("RAG 知识库问答系统", _names(executable))


class TestDomainResolution(unittest.TestCase):
    def test_job_name_wins(self):
        self.assertEqual(PA.resolve_job_domain("会计", []), "accounting")
        self.assertEqual(PA.resolve_job_domain("新媒体运营", []), "operations")
        self.assertEqual(PA.resolve_job_domain("高中教师", []), "teaching")

    def test_skill_vote_when_job_name_unknown(self):
        self.assertEqual(
            PA.resolve_job_domain("财务岗（校招）", ["会计准则"]), "accounting"
        )
        self.assertEqual(
            PA.resolve_job_domain("内容岗", ["用户增长", "活动策划"]), "operations"
        )
        # 内容策划/文案写作 也被传媒语言岗位使用，无法唯一定位领域
        self.assertIsNone(
            PA.resolve_job_domain("内容岗", ["内容策划", "文案写作"])
        )

    def test_cross_domain_skills_do_not_guess(self):
        self.assertIsNone(
            PA.resolve_job_domain("综合岗", ["Python", "会计准则"])
        )

    def test_catalog_failure_degrades_to_no_filter(self):
        with mock.patch.object(PA, "JobCatalog", side_effect=RuntimeError("boom")):
            self.assertIsNone(PA.resolve_job_domain("会计", ["会计准则"]))


class TestTemplateInjectionByDomain(unittest.TestCase):
    def test_accounting_prompt_contains_accounting_only(self):
        block = PA._load_project_templates(
            ["会计准则", "财务报表分析", "成本核算"],
            [{"stage": 1, "topic": "核算基础", "covers": ["会计准则"]}],
            {"会计准则": "初级", "Excel": "中级"},
            domain="accounting",
        )
        self.assertIn("月度账务处理与报表编制", block)
        self.assertIn("增值税与企业所得税申报模拟", block)
        self.assertNotIn("AI 智能问答助手", block)
        self.assertNotIn("RAG 知识库问答系统", block)

    def test_operations_prompt_contains_operations_only(self):
        block = PA._load_project_templates(
            ["内容策划", "文案写作", "运营数据分析"],
            [{"stage": 1, "topic": "内容基础", "covers": ["文案写作"]}],
            {"文案写作": "初级", "运营数据分析": "初级", "Excel": "中级"},
            domain="operations",
        )
        self.assertIn("账号定位与内容选题库", block)
        self.assertIn("社群拉新与活动复盘", block)
        self.assertNotIn("月度账务处理与报表编制", block)
        self.assertNotIn("AI 智能问答助手", block)

    def test_teaching_prompt_contains_teaching_only(self):
        block = PA._load_project_templates(
            ["教学设计", "学情分析"],
            [{"stage": 1, "topic": "教学基础", "covers": ["教学设计"]}],
            {"学科专业知识": "中级", "教育心理学": "初级"},
            domain="teaching",
        )
        self.assertIn("单元教学设计作品集", block)
        self.assertIn("学情分析与分层作业设计", block)
        self.assertNotIn("AI 智能问答助手", block)


class TestProjectNodeUsesDomain(unittest.TestCase):
    def test_accounting_node_gets_accounting_candidates(self):
        captured = {}

        def fake(prompt, schema):
            captured["prompt"] = prompt
            return PA.ProjectPlan(projects=[
                PA.ProjectItem(
                    name="月度核算模拟",
                    goal="补齐会计准则与报表分析",
                    covers_gaps=["会计准则", "财务报表分析"],
                    tech_stack=["Excel", "用友"],
                    stage="阶段1",
                    deliverable="一套核算底稿与报表",
                )
            ])

        state = {
            "target_job": "会计",
            "skill_gaps": ACCOUNTING_GAPS,
            "learning_plan": [
                {"stage": 1, "topic": "核算基础", "goal": "掌握准则", "covers": ["会计准则"]},
            ],
            "user_skills": [
                {"skill": "会计准则", "level": "初级"},
                {"skill": "Excel", "level": "中级"},
            ],
        }
        with mock.patch.object(PA, "structured_invoke", fake):
            out = PA.project_node(state)

        self.assertIn("月度账务处理与报表编制", captured["prompt"])
        self.assertNotIn("AI 智能问答助手", captured["prompt"])
        self.assertEqual(out["project_plan"][0]["stage"], "阶段 1")
        self.assertEqual(out["project_plan"][0]["covers_gaps"], ["会计准则", "财务报表分析"])

    def test_teaching_node_gets_teaching_candidates(self):
        captured = {}

        def fake(prompt, schema):
            captured["prompt"] = prompt
            return PA.ProjectPlan(projects=[
                PA.ProjectItem(
                    name="单元设计作品集",
                    goal="补齐教学设计与学情分析",
                    covers_gaps=["教学设计", "学情分析"],
                    tech_stack=["PPT", "课程标准"],
                    stage="阶段 1",
                    deliverable="一份单元教学设计",
                )
            ])

        state = {
            "target_job": "中学教师",
            "skill_gaps": TEACHING_GAPS,
            "learning_plan": [
                {"stage": 1, "topic": "教学基础", "goal": "会设计", "covers": ["教学设计"]},
            ],
            "user_skills": [
                {"skill": "学科专业知识", "level": "中级"},
                {"skill": "教育心理学", "level": "初级"},
            ],
        }
        with mock.patch.object(PA, "structured_invoke", fake):
            out = PA.project_node(state)

        self.assertIn("单元教学设计作品集", captured["prompt"])
        self.assertNotIn("月度账务处理与报表编制", captured["prompt"])
        self.assertEqual(out["project_plan"][0]["covers_gaps"], ["教学设计", "学情分析"])


if __name__ == "__main__":
    unittest.main()
