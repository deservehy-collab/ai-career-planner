"""岗位目录与多领域技能词表的单元测试。

隔离策略：领域解析、技能并集、别名归一都只读真实知识库，不调模型、不写文件。
"""

import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import yaml  # noqa: E402

from agents.job_catalog import JobCatalog, JobCatalogError  # noqa: E402
from agents.skill_dict import SkillVocabulary  # noqa: E402
from agents.utils import resolve_canonical  # noqa: E402


def _profile(job_id="demo", job_name="示例岗位", domain="accounting", skills=None):
    return {
        "job_id": job_id,
        "job_name": job_name,
        "domain": domain,
        "aliases": [job_name],
        "skills": [{"skill": name} for name in (skills or ["示例技能"])],
    }


class TestJobCatalog(unittest.TestCase):
    def setUp(self):
        self.catalog = JobCatalog()

    def test_loads_all_six_domains(self):
        self.assertEqual(
            set(self.catalog.domains),
            {
                "ai-application", "accounting", "operations", "teaching",
                "data-analysis", "software-testing",
                "human-resources", "design", "healthcare", "electronics",
                "mechanical-civil", "media-language", "legal-public",
                "agriculture-environment", "sports", "bio-chemical",
            },
        )

    def test_new_jobs_resolve_by_alias(self):
        self.assertEqual(self.catalog.resolve_domain("电商运营"), "operations")
        self.assertEqual(self.catalog.resolve_domain("数据分析师"), "data-analysis")
        self.assertEqual(self.catalog.resolve_domain("软件测试工程师"), "software-testing")
        profile = self.catalog.resolve_profile("电商运营")
        self.assertEqual(profile["job_id"], "ecommerce_operator")
        self.assertGreaterEqual(len(self.catalog.profiles), 12)

    def test_skill_union_covers_ai_and_non_ai(self):
        names = set(self.catalog.skill_names)
        for name in ("Python", "RAG", "会计准则", "纳税申报", "内容策划", "教学设计"):
            self.assertIn(name, names)
        self.assertGreaterEqual(len(names), 40)

    def test_resolve_domain_by_alias_and_substring(self):
        self.assertEqual(self.catalog.resolve_domain("会计助理"), "accounting")
        self.assertEqual(self.catalog.resolve_domain("成本会计"), "accounting")
        self.assertEqual(self.catalog.resolve_domain("新媒体运营"), "operations")
        self.assertEqual(self.catalog.resolve_domain("电商运营"), "operations")
        self.assertEqual(self.catalog.resolve_domain("初中教师"), "teaching")
        self.assertEqual(self.catalog.resolve_domain("班主任"), "teaching")
        self.assertEqual(
            self.catalog.resolve_domain("AI 应用开发工程师"), "ai-application"
        )

    def test_resolve_profile_returns_job_and_builtin_profile(self):
        profile = self.catalog.resolve_profile("会计助理")
        self.assertIsNotNone(profile)
        self.assertEqual(profile["job_id"], "accountant")
        self.assertTrue(profile["ground_requirements"])
        self.assertGreaterEqual(len(profile["profile"]["core_tasks"]), 4)
        self.assertIn("会计", profile["profile"]["summary"])
        self.assertEqual(
            self.catalog.resolve_profile("AI 应用开发工程师")["job_id"],
            "ai_application_engineer",
        )

    def test_unknown_job_returns_none(self):
        self.assertIsNone(self.catalog.resolve_domain("护理"))
        self.assertIsNone(self.catalog.resolve_domain(""))
        self.assertIsNone(self.catalog.resolve_profile("护理"))

    def test_domain_for_skills_only_when_unique(self):
        # Excel 现在被会计/运营/教学/数据分析共用，不能再靠它判断领域
        self.assertEqual(
            self.catalog.domain_for_skills(["会计准则", "纳税申报"]), "accounting"
        )
        self.assertEqual(self.catalog.domain_for_skills(["用户增长", "活动策划"]), "operations")
        # 内容策划/文案写作 同时被运营与传媒语言岗位使用，不能再靠它们判断领域
        self.assertIsNone(self.catalog.domain_for_skills(["内容策划", "文案写作"]))
        self.assertEqual(self.catalog.domain_for_skills(["RAG", "Agent"]), "ai-application")
        self.assertEqual(self.catalog.domain_for_skills(["教学设计"]), "teaching")
        self.assertEqual(
            self.catalog.domain_for_skills(["统计学基础", "指标体系"]), "data-analysis"
        )
        self.assertEqual(
            self.catalog.domain_for_skills(["功能测试", "缺陷管理"]), "software-testing"
        )
        # 跨领域技能混在一起时不猜
        self.assertIsNone(self.catalog.domain_for_skills(["Python", "会计准则"]))

    def test_invalid_domain_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "bad.yaml"
            path.write_text(
                yaml.safe_dump(_profile(domain="godlike"), allow_unicode=True),
                encoding="utf-8",
            )
            with self.assertRaises(JobCatalogError):
                JobCatalog(directory=Path(d))

    def test_missing_skills_rejected(self):
        bad = _profile()
        bad.pop("skills")
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "bad.yaml").write_text(
                yaml.safe_dump(bad, allow_unicode=True), encoding="utf-8"
            )
            with self.assertRaises(JobCatalogError):
                JobCatalog(directory=Path(d))


class TestVocabularyAcrossDomains(unittest.TestCase):
    def setUp(self):
        self.vocab = SkillVocabulary()

    def test_non_ai_skill_names_are_known(self):
        names = set(self.vocab.names)
        self.assertIn("会计准则", names)
        self.assertIn("内容策划", names)
        self.assertIn("教学设计", names)

    def test_aliases_resolve_to_canonical(self):
        cases = {
            "企业会计准则": "会计准则",
            "报表分析": "财务报表分析",
            "家长沟通": "家校沟通",
            "剪辑": "短视频剪辑",
            "运营数据": "运营数据分析",
            "教案设计": "教学设计",
            "学情诊断": "学情分析",
            "投放": "投放优化",
            "会计软件使用": "财务软件",
            "税务法规": "纳税申报",
            "成本管理": "成本核算",
            # 新增规范技能"成本控制"后，字面名优先于旧别名"成本控制→成本核算"
            "成本控制": "成本控制",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(resolve_canonical(raw, self.vocab), expected)

    def test_ai_behavior_is_unchanged(self):
        self.assertEqual(resolve_canonical("后端服务", self.vocab), "后端 API 开发")
        self.assertEqual(resolve_canonical("Python", self.vocab), "Python")
        # 同时沾到两个规范名的含混说法仍然不猜
        self.assertIsNone(resolve_canonical("提示词与向量检索", self.vocab))

    def test_literal_name_wins_over_alias_substring(self):
        """新增技能名不能被旧别名("审计"、"教研")拖成歧义。"""
        self.assertEqual(resolve_canonical("审计程序", self.vocab), "审计程序")
        self.assertEqual(resolve_canonical("审计证据与底稿", self.vocab), "审计证据与底稿")
        self.assertEqual(resolve_canonical("审计沟通", self.vocab), "审计沟通")
        self.assertEqual(resolve_canonical("校本研修设计", self.vocab), "校本研修设计")
        # 只有别名命中且唯一时仍可归一
        self.assertEqual(resolve_canonical("教研", self.vocab), "教学反思与研究")

    def test_multiple_literal_names_are_still_ambiguous(self):
        self.assertIsNone(
            resolve_canonical("审计程序与审计沟通都要会", self.vocab)
        )


if __name__ == "__main__":
    unittest.main()
