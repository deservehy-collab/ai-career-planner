"""岗位知识库驱动岗位要求与能力评估的单元测试（标准库 unittest）。

覆盖三件事：
1. 知识库岗位（会计）的规范技能会进入提示词，并补全模型漏项；
2. 知识库中的人工画像优先于模型生成，避免小模型输出空任务；
3. 用户粘贴原文时，知识库不得越权改写或补全岗位要求；
4. 评估阶段能把「用友财务软件」与规范名「财务软件」唯一对上。
"""

import sys
import types
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agents import assessment_agent as AA  # noqa: E402
from agents import job_agent as JA  # noqa: E402
from agents import job_profile as JP  # noqa: E402
from agents.user_jd import USER_JD_PROVENANCE  # noqa: E402

ACCOUNTING_SKILLS = {
    "会计准则", "财务报表分析", "纳税申报", "财务软件", "Excel",
    "审计基础", "成本核算", "预算管理", "内部控制", "出纳实务",
}


def _requirement(skill, importance=3, level="中级"):
    return JA.JobRequirement(
        skill=skill, importance=importance, level=level, reason="岗位用途"
    )


def _run_job(target_job, skills, evidence=None):
    def fake(prompt, schema):
        fake.prompt = prompt
        return JA.JobAnalysis(requirements=[_requirement(s) for s in skills])

    state = {"target_job": target_job, "job_evidence": evidence or [],
             "retrieval_status": "no_source"}
    with mock.patch.object(JA, "structured_invoke", fake):
        out = JA.job_node(state)
    return out, fake.prompt


class TestKnowledgeGroundedJobNode(unittest.TestCase):
    def test_prompt_injects_canonical_skills(self):
        out, prompt = _run_job("会计", ["预算编制", "会计软件使用", "税务法规"])
        self.assertIn("岗位知识库·规范技能表", prompt)
        self.assertIn("会计准则", prompt)
        self.assertIn("财务软件", prompt)

    def test_model_wording_is_aligned_and_missing_skills_are_added(self):
        out, _ = _run_job("会计", ["预算编制", "会计软件使用", "税务法规", "飞书"])
        by_skill = {item["skill"]: item for item in out["job_requirements"]}

        # 近义词被改写成本岗位规范名
        self.assertIn("预算管理", by_skill)
        self.assertIn("财务软件", by_skill)
        self.assertIn("纳税申报", by_skill)
        self.assertNotIn("预算编制", by_skill)
        self.assertNotIn("会计软件使用", by_skill)
        self.assertNotIn("税务法规", by_skill)
        # 表外工具不进入会计岗位要求
        self.assertNotIn("飞书", by_skill)

        # 模型漏掉的规范技能由知识库补全
        self.assertEqual(set(by_skill), ACCOUNTING_SKILLS)

        excel = by_skill["Excel"]
        self.assertEqual(excel["importance_basis"], "profile_skill_importance")
        self.assertEqual(excel["level_basis"]["origin"], "profile_level")
        # 没有招聘原文时仍是模型补充，不冒充原话证据
        self.assertEqual(excel["evidence_status"], "model_supplement")
        self.assertEqual(excel["evidence_refs"], [])

    def test_builtin_profile_is_used_without_model_generation(self):
        generator = mock.Mock(side_effect=AssertionError("不应生成画像"))

        def fake(prompt, schema):
            return JA.JobAnalysis(requirements=[_requirement(s) for s in ("Excel", "会计准则", "财务软件")])

        with mock.patch.object(JA, "generate_job_profile", generator), \
             mock.patch.object(JA, "structured_invoke", fake):
            out = JA.job_node({"target_job": "会计", "job_evidence": [],
                               "retrieval_status": "no_source",
                               "enable_job_profile": True})

        generator.assert_not_called()
        self.assertEqual(out["job_profile_origin"], "knowledge_base")
        self.assertIsNotNone(out["job_profile"])
        self.assertGreaterEqual(len(out["job_profile"]["core_tasks"]), 4)
        self.assertIn("账务", out["job_profile"]["summary"])

    def test_user_jd_is_not_rewritten_or_supplemented(self):
        evidence = [{
            "skill": "纳税申报",
            "provenance": USER_JD_PROVENANCE,
            "note": "负责增值税、企业所得税等税种的申报",
            "company_slug": None,
        }]

        def fake(prompt, schema):
            return JA.JobAnalysis(requirements=[
                _requirement("税务法规", importance=5, level="中级")
            ])

        with mock.patch.object(JA, "structured_invoke", fake):
            out = JA.job_node({"target_job": "会计", "job_evidence": evidence,
                               "retrieval_status": "user_pasted"})

        items = out["job_requirements"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["skill"], "税务法规")
        # 名字是原文说法，按别名唯一归一到知识库技能后算部分支持；不是逐字命中
        self.assertEqual(items[0]["evidence_status"], "partially_supported")


class TestBuiltinProfile(unittest.TestCase):
    def test_catalog_profile_maps_entries_to_standard_source(self):
        source = {
            "job_name": "会计",
            "profile": {
                "summary": "负责账务处理",
                "core_tasks": [{"name": "编制报表", "detail": "出具三大报表"}],
                "tools": ["Excel"],
                "deliverables": ["月度报表"],
                "entry_requirements": ["会计学专业"],
                "career_path": ["总账会计"],
            },
        }
        profile = JP.builtin_profile(source, "会计")
        self.assertIsNotNone(profile)
        self.assertEqual(profile["core_tasks"][0]["source"], JP.SOURCE_STANDARD)
        self.assertEqual(profile["tools"][0]["name"], "Excel")

    def test_missing_core_tasks_returns_none(self):
        self.assertIsNone(JP.builtin_profile({"profile": {"summary": "x"}}, "会计"))
        self.assertIsNone(JP.builtin_profile({}, "会计"))


class TestAssessmentCanonicalMatch(unittest.TestCase):
    def test_user_alias_matches_canonical_requirement(self):
        gaps = AA.judge_gaps(
            [{"skill": "用友财务软件", "level": "中级", "evidence": "课程实训"}],
            [{"skill": "财务软件", "importance": 4, "level": "中级"}],
            types.SimpleNamespace(gaps=[]),
        )
        self.assertEqual(gaps[0]["supported_by"], "用友财务软件")
        self.assertEqual(gaps[0]["current_level"], "中级")
        self.assertEqual(gaps[0]["status"], "已满足")

    def test_model_answer_using_canonical_name_maps_back_to_user_skill(self):
        answer = types.SimpleNamespace(
            skill="财务软件", required_level="中级",
            supported_by="财务软件", gap="需要熟悉财务软件",
        )
        gaps = AA.judge_gaps(
            [{"skill": "用友财务软件", "level": "中级", "evidence": "课程实训"}],
            [{"skill": "财务软件", "importance": 4, "level": "高级"}],
            types.SimpleNamespace(gaps=[answer]),
        )
        self.assertEqual(gaps[0]["supported_by"], "用友财务软件")
        self.assertEqual(gaps[0]["current_level"], "中级")
        self.assertEqual(gaps[0]["status"], "部分满足")

    def test_ambiguous_canonical_names_are_not_guessed(self):
        gaps = AA.judge_gaps(
            [
                {"skill": "用友", "level": "中级", "evidence": "A"},
                {"skill": "金蝶", "level": "初级", "evidence": "B"},
            ],
            [{"skill": "财务软件", "importance": 4, "level": "中级"}],
            types.SimpleNamespace(gaps=[]),
        )
        self.assertEqual(gaps[0]["supported_by"], "无")
        self.assertEqual(gaps[0]["current_level"], "无")


if __name__ == "__main__":
    unittest.main(verbosity=2)
