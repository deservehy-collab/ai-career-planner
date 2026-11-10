"""黄金评测集：用例结构 + 计分规则的回归测试。

这里**不调模型**：
1. 用例文件必须自洽（技能名能落到知识库、数量在 20~30、四类岗位都覆盖）；
2. 计分规则必须真的能抓到问题（合成一份"全过"结果，再逐项破坏它）。

真正的模型评测由 ``scripts/eval_golden.py`` 手工触发，不属于 pytest。
"""

import re
import unittest
from copy import deepcopy

from agents.job_catalog import VALID_DOMAINS
from golden.scoring import (
    EvalContext,
    evaluate_case,
    load_cases,
    regression_diff,
    summarize,
)

CASES = load_cases()
CTX = EvalContext.build()
DOMAIN_SKILLS = {
    domain: {
        CTX.canonical(item["skill"])
        for profile in CTX.catalog.profiles
        if profile.get("domain") == domain
        for item in profile.get("skills") or []
    }
    for domain in VALID_DOMAINS
}


def case_by_id(case_id):
    return next(case for case in CASES if case["id"] == case_id)


def canonical_set(values):
    return {CTX.canonical(value) for value in values or []}


def result_for(case, **overrides):
    """一份"满足该用例全部检查"的合成结果，供计分规则单测破坏。"""
    result = {
        "job_requirements": [
            {"skill": "会计准则", "importance": 5, "level": "中级"},
        ],
        "skill_gaps": [
            {
                "skill": "会计准则",
                "status": "明显不足",
                "display_status": "明显不足",
                "verification": "unverified",
                "supported_by": "无",
                "current_level": "无",
                "required_level": "中级",
                "importance": 5,
                "priority": "高",
                "gap": "缺少准则依据",
                "evidence_kind": "none",
                "evidence_ref": "",
                "evidence_note": "",
            }
        ],
        "learning_plan": [
            {
                "stage": 1,
                "topic": "会计准则基础",
                "goal": "补会计准则",
                "covers": ["会计准则"],
                "prerequisite": "无",
                "completion_criteria": "能独立完成一套账务处理",
                "estimated_weeks": 2,
            }
        ],
        "project_plan": [
            {
                "name": "月度账务处理与报表编制",
                "goal": "用真实账套练准则",
                "covers_gaps": ["会计准则"],
                "tech_stack": ["Excel"],
                "stage": "阶段 1",
                "deliverable": "一套月度账套",
            }
        ],
        "job_profile_origin": "knowledge_base",
        "requirement_basis": "model_only",
    }
    for key, value in overrides.items():
        result[key] = value
    return result


UNIT_CASE = {
    "id": "unit-accounting",
    "target_job": "会计",
    "expect": {
        "domain": "accounting",
        "requirement_basis": "model_only",
        "requirements_include": ["会计准则"],
        "requirements_exclude": ["Python"],
        "assessment": {"not_satisfied": ["会计准则"]},
        "learning": {"cover": ["会计准则"], "forbid_keywords": ["Python"]},
        "projects": {"allowed_domains": ["accounting"], "min_from_kb": 1},
    },
}


def failed_names(report):
    return {check.name for check in report.failures}


class GoldenCaseShapeTests(unittest.TestCase):
    def test_case_count_and_unique_ids(self):
        self.assertGreaterEqual(len(CASES), 20)
        self.assertLessEqual(len(CASES), 30)
        ids = [str(case.get("id")) for case in CASES]
        self.assertEqual(len(ids), len(set(ids)))
        for case_id in ids:
            self.assertRegex(case_id, re.compile(r"^[a-z0-9-]+$"))

    def test_every_case_has_annotation_and_inputs(self):
        for case in CASES:
            with self.subTest(case=case["id"]):
                self.assertGreater(len(str(case.get("note") or "").strip()), 10)
                profile = case.get("profile") or {}
                self.assertTrue(str(profile.get("major") or "").strip())
                self.assertTrue(str(profile.get("grade") or "").strip())
                self.assertTrue(profile.get("skills"), "至少要有一条自述技能")
                self.assertIsInstance(profile.get("projects"), list)
                self.assertTrue(str(case.get("target_job") or "").strip())

    def test_domain_matches_catalog_and_coverage(self):
        # 黄金集目前只覆盖首批四个领域；数据分析与软件测试还没有可复现的
        # 公开样本，先不硬凑用例，等有样本后再补。
        benchmarked = ("ai-application", "accounting", "operations", "teaching")
        counts = {domain: 0 for domain in benchmarked}
        for case in CASES:
            with self.subTest(case=case["id"]):
                expect = case.get("expect") or {}
                self.assertIn("domain", expect)
                expected = expect["domain"]
                self.assertIn(expected, (*VALID_DOMAINS, None))
                self.assertEqual(
                    CTX.catalog.resolve_domain(case.get("target_job")), expected,
                    "用例标注的领域与岗位目录解析结果不一致",
                )
                if expected:
                    counts[expected] = counts.get(expected, 0) + 1
        for domain, count in counts.items():
            self.assertGreaterEqual(count, 3, f"{domain} 的用例太少：{count}")

    def test_known_domain_skills_stay_in_domain(self):
        for case in CASES:
            expect = case.get("expect") or {}
            domain = expect.get("domain")
            if domain is None:
                continue
            with self.subTest(case=case["id"]):
                allowed = DOMAIN_SKILLS[domain]
                included = canonical_set(expect.get("requirements_include"))
                self.assertTrue(included <= allowed, f"期望技能不在 {domain}：{included - allowed}")
                excluded = canonical_set(expect.get("requirements_exclude"))
                self.assertFalse(excluded & allowed, f"排除项里混入了同领域技能：{excluded & allowed}")
                assessed = canonical_set(
                    (expect.get("assessment") or {}).get("not_satisfied")
                )
                self.assertTrue(assessed <= allowed, f"评估期望不在 {domain}：{assessed - allowed}")
                covered = canonical_set((expect.get("learning") or {}).get("cover"))
                self.assertTrue(covered <= allowed, f"学习期望不在 {domain}：{covered - allowed}")

                projects = expect.get("projects") or {}
                if projects.get("min_from_kb"):
                    self.assertIn(domain, projects.get("allowed_domains") or [])

    def test_requirements_basis_matches_jd(self):
        """主依据档位只能由「有没有原文 / 岗位在不在目录」两件事决定。

        没粘原文但岗位在内置目录里 ⇒ ``knowledge_base``：这一轮的每条依据都能回查到
        内置资料原句（阶段 4 接通了 ``knowledge/jobs`` 的 evidence）。认不出岗位 ⇒ 只剩
        模型常识，必须老实写 ``model_only``。
        """
        for case in CASES:
            with self.subTest(case=case["id"]):
                job = str(case.get("target_job") or "")
                has_jd = bool(str(case.get("jd_text") or "").strip())
                in_catalog = CTX.catalog.resolve_profile(job) is not None
                expected = (
                    "user_pasted" if has_jd
                    else "knowledge_base" if in_catalog
                    else "model_only"
                )
                actual = (case.get("expect") or {}).get("requirement_basis")
                self.assertEqual(
                    actual, expected,
                    f"{job}：粘原文={has_jd} 目录内={in_catalog}，期望 {expected}",
                )

    def test_assessment_expectations_are_guaranteed_by_requirements(self):
        for case in CASES:
            expect = case.get("expect") or {}
            domain = expect.get("domain")
            assessment = expect.get("assessment") or {}
            with self.subTest(case=case["id"]):
                if domain is None:
                    # 未知岗位的要求由模型现场生成，不能写死"某项必须出现"
                    self.assertFalse(assessment.get("not_satisfied"))
                    self.assertFalse(assessment.get("satisfied"))
                    self.assertFalse((expect.get("learning") or {}).get("cover"))
                    self.assertEqual((expect.get("projects") or {}).get("min_from_kb", 0), 0)
                    continue

                if case.get("jd_text"):
                    # JD 路径下，只有写进 include 的技能才保证会被提取到
                    included = canonical_set(expect.get("requirements_include"))
                    assessed = canonical_set(assessment.get("not_satisfied"))
                    self.assertTrue(assessed <= included, f"未满足项未被 include 保证：{assessed - included}")
                else:
                    profile = CTX.catalog.resolve_profile(case.get("target_job"))
                    grounded = bool(profile and profile.get("ground_requirements"))
                    if not grounded:
                        # AI 岗位不强制 ground：无 JD 时不写死具体要求
                        self.assertFalse(assessment.get("not_satisfied"))
                        self.assertFalse((expect.get("learning") or {}).get("cover"))

                covered = canonical_set((expect.get("learning") or {}).get("cover"))
                if covered:
                    included = canonical_set(expect.get("requirements_include"))
                    profile = CTX.catalog.resolve_profile(case.get("target_job"))
                    grounded = bool(profile and profile.get("ground_requirements"))
                    self.assertTrue(
                        grounded or covered <= included,
                        "学习覆盖的缺口必须有确定来源（知识库 ground 或 JD include）",
                    )

    def test_required_regression_scenarios_present(self):
        # 自述无项目 → 必须有一条用例断言"未满足"
        self.assertTrue(any(
            not (case.get("profile") or {}).get("projects")
            and ((case.get("expect") or {}).get("assessment") or {}).get("not_satisfied")
            for case in CASES
        ))
        # 教师跨技能误判
        trap = case_by_id("teach-cross-evidence-trap")
        self.assertIn("教育心理学", trap["profile"]["skills"])
        self.assertIn("教学设计", trap["expect"]["assessment"]["not_satisfied"])
        # 未知岗位 & JD / 无 JD 两条路径
        self.assertTrue(any((case.get("expect") or {}).get("domain") is None for case in CASES))
        self.assertTrue(any(str(case.get("jd_text") or "").strip() for case in CASES))
        self.assertTrue(any(not str(case.get("jd_text") or "").strip() for case in CASES))

    def test_ai_product_manager_is_not_ai_engineer(self):
        self.assertIsNone(CTX.catalog.resolve_domain("AI 产品经理"))


class GoldenScoringTests(unittest.TestCase):
    def test_baseline_synthetic_result_passes(self):
        report = evaluate_case(UNIT_CASE, result_for(UNIT_CASE), CTX)
        self.assertTrue(report.passed, [check.to_dict() for check in report.failures])

    def test_false_satisfied_without_direct_evidence_is_caught(self):
        result = result_for(UNIT_CASE)
        result["skill_gaps"][0].update({
            "status": "已满足",
            "display_status": "已满足",
            "verification": "unverified",
            "supported_by": "会计准则",
            "evidence_kind": "self_report",
        })
        report = evaluate_case(UNIT_CASE, result, CTX)
        self.assertIn("assessment.honesty_invariant", failed_names(report))
        self.assertIn("assessment.not_satisfied", failed_names(report))

    def test_missing_expected_requirement_is_caught(self):
        result = result_for(UNIT_CASE, job_requirements=[])
        report = evaluate_case(UNIT_CASE, result, CTX)
        self.assertIn("requirements.include", failed_names(report))
        self.assertIn("workflow.requirements_present", failed_names(report))

    def test_cross_domain_requirement_is_caught(self):
        result = result_for(UNIT_CASE)
        result["job_requirements"].append(
            {"skill": "Python", "importance": 5, "level": "中级"}
        )
        report = evaluate_case(UNIT_CASE, result, CTX)
        self.assertIn("requirements.exclude", failed_names(report))

    def test_cross_domain_project_is_caught(self):
        result = result_for(UNIT_CASE)
        result["project_plan"][0]["name"] = "RAG 知识库问答系统"
        report = evaluate_case(UNIT_CASE, result, CTX)
        self.assertIn("projects.domain", failed_names(report))

    def test_satisfied_skill_cannot_be_used_as_project_gap(self):
        result = result_for(UNIT_CASE)
        result["skill_gaps"].append({
            "skill": "纳税申报",
            "status": "已满足",
            "display_status": "已满足",
            "verification": "direct",
            "supported_by": "纳税申报",
            "current_level": "中级",
            "required_level": "中级",
            "importance": 4,
            "priority": "低",
            "gap": "",
            "evidence_kind": "project",
            "evidence_ref": "税务实习",
            "evidence_note": "税务实习",
        })
        result["project_plan"][0]["covers_gaps"] = ["纳税申报"]
        report = evaluate_case(UNIT_CASE, result, CTX)
        self.assertIn("projects.covers_only_unmet", failed_names(report))

    def test_high_priority_gap_must_appear_in_learning(self):
        result = result_for(UNIT_CASE)
        result["learning_plan"][0]["covers"] = []
        report = evaluate_case(UNIT_CASE, result, CTX)
        self.assertIn("learning.cover", failed_names(report))
        self.assertIn("learning.high_priority_covered", failed_names(report))

    def test_unknown_job_must_not_claim_builtin_profile(self):
        case = {
            "id": "unit-unknown",
            "target_job": "宠物美容师",
            "expect": {"domain": None, "requirement_basis": "model_only"},
        }
        result = result_for(case, job_profile_origin="knowledge_base")
        report = evaluate_case(case, result, CTX)
        self.assertIn("job.profile_origin", failed_names(report))

    def test_summary_and_regression_diff(self):
        case = deepcopy(UNIT_CASE)
        passing = evaluate_case(case, result_for(case), CTX)
        broken = result_for(case)
        broken["skill_gaps"][0]["display_status"] = "已满足"
        failing = evaluate_case(case, broken, CTX)

        summary = summarize([passing, failing])
        self.assertEqual(summary["total"], 2)
        self.assertEqual(summary["passed"], 1)

        diff = regression_diff([failing], {"cases": [{"id": "unit-accounting", "passed": True}]})
        self.assertEqual(diff["regressions"], ["unit-accounting"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
