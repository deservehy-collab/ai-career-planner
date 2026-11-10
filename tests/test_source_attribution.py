"""来源归因的口径测试：回查不到证据的一律算模型推断。

运行：
    .venv/Scripts/python.exe -m pytest tests/test_source_attribution.py -q

这里全是纯函数，不打模型、不读盘，因此可以进每次提交的回归。
"""

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agents.external_sources import OFFICIAL_PROVENANCE  # noqa: E402
from agents.job_agent import evidence_ref  # noqa: E402
from agents.source_attribution import (  # noqa: E402
    SOURCE_KNOWLEDGE_BASE,
    SOURCE_MODEL,
    SOURCE_OFFICIAL_DATA,
    SOURCE_PRIORITY,
    SOURCE_USER_JD,
    build_source_report,
    catalog_profile_matched,
    count_by_source,
    distinct_official_sources,
    evidence_by_source,
    evidence_source,
    gap_source_hint,
    group_by_source,
    is_model_inferred,
    ref_of,
    requirement_citation,
    requirement_source,
    resolve_refs,
)

JD_EVIDENCE = {"provenance": "user_pasted", "source_id": "user_pasted",
               "note": "熟练掌握 Python，能独立开发后端接口"}
#: 只有 source_id 前缀、provenance 不是 official_digest 的历史条目：靠前缀兜底认档。
OFFICIAL_EVIDENCE = {"provenance": "public_summary", "source_id": "source_py_jd_001",
                     "note": "参与微服务拆分与网关治理"}
BLANK_EVIDENCE = {"provenance": "", "source_id": "", "note": "来路不明的一句"}
EVIDENCE = [JD_EVIDENCE, OFFICIAL_EVIDENCE, BLANK_EVIDENCE]


def req(skill="Python", refs=(), status="supported", notes=(),
        importance_basis="", level_origin=""):
    item = {
        "skill": skill,
        "importance": 5,
        "level": "中级",
        "evidence_status": status,
        "evidence_refs": list(refs),
        "source_notes": list(notes),
    }
    if importance_basis:
        item["importance_basis"] = importance_basis
    if level_origin:
        item["level_basis"] = {"origin": level_origin, "quote": None, "by_company": {}}
    return item


class RefNumberingTest(unittest.TestCase):
    def test_ref_format_must_match_job_agent(self):
        """两处各写一遍编号格式必然漂移；这条用例就是把它们钉在一起。"""
        for index in range(0, 12):
            self.assertEqual(ref_of(index), evidence_ref(index))

    def test_resolve_refs_skips_invalid_and_duplicates(self):
        pairs = resolve_refs(["E01", "e01", "E02", "E99", "无", None, "E0"], EVIDENCE)
        self.assertEqual([token for token, _ in pairs], ["E01", "E02"])
        self.assertIs(pairs[1][1], OFFICIAL_EVIDENCE)

    def test_resolve_refs_survives_missing_evidence_list(self):
        self.assertEqual(resolve_refs(["E01"], None), [])
        self.assertEqual(resolve_refs(None, EVIDENCE), [])
        self.assertEqual(resolve_refs(["E03"], [JD_EVIDENCE, "不是字典"]), [])


class EvidenceSourceTest(unittest.TestCase):
    def test_three_buckets(self):
        self.assertEqual(evidence_source(JD_EVIDENCE), SOURCE_USER_JD)
        self.assertEqual(evidence_source(OFFICIAL_EVIDENCE), SOURCE_OFFICIAL_DATA)
        self.assertIsNone(evidence_source(BLANK_EVIDENCE))
        self.assertIsNone(evidence_source("不是字典"))


class RequirementSourceTest(unittest.TestCase):
    def test_original_quote_wins_over_everything(self):
        both = req(refs=["E01", "E02"])
        self.assertEqual(requirement_source(both, EVIDENCE), SOURCE_USER_JD)

    def test_weak_evidence_is_still_user_jd(self):
        """partially_supported 是"原话里有这句但不是规范技能名"，来源不变，
        性质由 evidence_status 自己表达，不在这里造第二套档位。"""
        item = req(refs=["E01"], status="partially_supported")
        self.assertEqual(requirement_source(item, EVIDENCE), SOURCE_USER_JD)

    def test_public_summary_maps_to_official_data(self):
        item = req(refs=["E02"])
        self.assertEqual(requirement_source(item, EVIDENCE), SOURCE_OFFICIAL_DATA)

    def test_untraceable_ref_falls_back_to_model(self):
        """模型引用了一个不存在的编号时，绝不能因为"它自称有证据"就认下来。"""
        hallucinated = req(refs=["E88"])
        self.assertEqual(requirement_source(hallucinated, EVIDENCE), SOURCE_MODEL)
        self.assertTrue(is_model_inferred(hallucinated, EVIDENCE))

    def test_evidence_from_unknown_provenance_is_not_a_source(self):
        item = req(refs=["E03"])
        self.assertEqual(requirement_source(item, EVIDENCE), SOURCE_MODEL)

    def test_profile_grounding_without_any_quote(self):
        by_importance = req(importance_basis="profile_skill_importance")
        by_level = req(level_origin="profile_level")
        self.assertEqual(requirement_source(by_importance, EVIDENCE), SOURCE_KNOWLEDGE_BASE)
        self.assertEqual(requirement_source(by_level, EVIDENCE), SOURCE_KNOWLEDGE_BASE)

    def test_pure_model_supplement(self):
        item = req(status="model_supplement")
        self.assertEqual(requirement_source(item, EVIDENCE), SOURCE_MODEL)

    def test_garbage_input_is_model_not_crash(self):
        for junk in (None, {}, "字符串", [], {"evidence_refs": "E01"}):
            self.assertEqual(requirement_source(junk, EVIDENCE), SOURCE_MODEL)


class CitationViewTest(unittest.TestCase):
    def test_quotes_merge_without_duplicates(self):
        """evidence 的 note 与代码回填的 source_notes 是两条通道，同一句只留一次。"""
        item = req(refs=["E01"], notes=[JD_EVIDENCE["note"], "负责网关治理"])
        citation = requirement_citation(item, EVIDENCE)
        self.assertEqual(citation["quotes"], [JD_EVIDENCE["note"], "负责网关治理"])
        self.assertTrue(citation["traceable"])
        self.assertEqual(citation["source_type"], SOURCE_USER_JD)
        self.assertEqual(citation["source_label"], "用户粘贴的招聘原文")
        self.assertEqual(citation["evidence_refs"], ["E01"])

    def test_untraceable_citation_exposes_no_quote(self):
        citation = requirement_citation(req(refs=["E88"]), EVIDENCE)
        self.assertFalse(citation["traceable"])
        self.assertEqual(citation["quotes"], [])
        self.assertEqual(citation["source_type"], SOURCE_MODEL)
        self.assertEqual(citation["source_label"], "模型推断")

    def test_citation_carries_existing_status_verbatim(self):
        citation = requirement_citation(req(refs=["E01"], status="partially_supported",
                                            level_origin="jd_text"), EVIDENCE)
        self.assertEqual(citation["evidence_status"], "partially_supported")
        self.assertEqual(citation["level_basis_origin"], "jd_text")

    def test_citation_survives_garbage(self):
        citation = requirement_citation(None, EVIDENCE)
        self.assertEqual(citation["source_type"], SOURCE_MODEL)
        self.assertEqual(citation["skill"], "")


class AggregationTest(unittest.TestCase):
    def setUp(self):
        self.requirements = [
            req("Python", refs=["E01"]),
            req("微服务", refs=["E02"]),
            req("会计准则", importance_basis="profile_skill_importance"),
            req("沟通", status="model_supplement"),
            req("幻觉", refs=["E77"]),
        ]

    def test_counts_add_up(self):
        counts = count_by_source(self.requirements, EVIDENCE)
        self.assertEqual(counts["total"], len(self.requirements))
        self.assertEqual(counts[SOURCE_USER_JD], 1)
        self.assertEqual(counts[SOURCE_OFFICIAL_DATA], 1)
        self.assertEqual(counts[SOURCE_KNOWLEDGE_BASE], 1)
        self.assertEqual(counts[SOURCE_MODEL], 2)

    def test_grouping_keeps_every_key_present(self):
        """前端按固定顺序渲染四类，缺键会让它取到 undefined。"""
        grouped = group_by_source([], [])
        self.assertEqual(set(grouped), set(SOURCE_PRIORITY))
        self.assertEqual(grouped[SOURCE_MODEL], [])

    def test_distinct_official_sources_sorted(self):
        extra = req("网关", refs=["E02"])
        ids = distinct_official_sources(self.requirements + [extra], EVIDENCE)
        self.assertEqual(ids, ["source_py_jd_001"])

    def test_distinct_official_sources_ignores_user_jd(self):
        ids = distinct_official_sources([req("Python", refs=["E01"])], EVIDENCE)
        self.assertEqual(ids, [])


class SourceReportTest(unittest.TestCase):
    """投影层出口：前端拿到的 source_report 必须自洽、且与逐条真值表同源。"""

    def setUp(self):
        self.requirements = [
            req("Python", refs=["E01"]),
            req("微服务", refs=["E02"]),
            req("会计准则", importance_basis="profile_skill_importance"),
            req("沟通", status="model_supplement"),
        ]
        self.state = {
            "target_job": "后端开发",
            "job_requirements": self.requirements,
            "job_evidence": EVIDENCE,
            "job_profile_origin": "cache",
        }

    def test_report_shape(self):
        report = build_source_report(self.state, requirement_basis="user_pasted")
        self.assertEqual(report["target_job"], "后端开发")
        self.assertEqual(report["requirement_basis"], "user_pasted")
        self.assertEqual(report["evidence_sentence_count"], len(EVIDENCE))
        self.assertEqual(report["official_source_ids"], ["source_py_jd_001"])
        self.assertEqual(report["job_profile_origin"], "cache")
        self.assertEqual(report["model_only_skills"], ["沟通"])
        self.assertEqual(
            report["counts"],
            {"user_jd": 1, "official_data": 1, "knowledge_base": 1,
             "model_inference": 1, "total": 4},
        )

    def test_citations_align_with_requirements_by_index(self):
        """前端按下标配对，所以顺序与条数必须逐条对齐。"""
        citations = build_source_report(self.state)["citations"]
        self.assertEqual([c["skill"] for c in citations],
                         [r["skill"] for r in self.requirements])
        self.assertEqual([c["source_type"] for c in citations],
                         [requirement_source(r, EVIDENCE) for r in self.requirements])

    def test_empty_state_reports_zeroes_not_crash(self):
        report = build_source_report({}, requirement_basis="")
        self.assertEqual(report["counts"]["total"], 0)
        self.assertEqual(report["citations"], [])
        self.assertEqual(report["target_job"], "")
        self.assertEqual(report["job_profile_origin"], "")

    def test_catalog_matched_uses_builtin_directory(self):
        self.assertTrue(catalog_profile_matched("会计"))
        self.assertFalse(catalog_profile_matched("宠物美容师"))
        self.assertFalse(catalog_profile_matched(""))
        self.assertFalse(catalog_profile_matched(None))


class OfficialDigestEvidenceTest(unittest.TestCase):
    """内置资料摘要（agents/external_sources.py 产出）归到 official_data 这一档。

    这类条目的 source_id 不一定是 ``source_`` 前缀（AI 岗画像里是 ``jd_hundsun_001``
    这种真招聘广告编号），靠的是 ``provenance = official_digest`` 这个显式标记 ——
    它必须是"能回查、但不是这家公司的原话"，既不能算用户原文，也不能算没出处。
    """

    def setUp(self):
        self.digest = {"provenance": OFFICIAL_PROVENANCE, "source_id": "jd_hundsun_001",
                       "skill": "Python", "note": "岗位要求2：熟练使用Python、Java中一种语言"}
        self.evidence = [JD_EVIDENCE, self.digest]

    def test_provenance_beats_the_source_id_prefix(self):
        self.assertEqual(evidence_source(self.digest), SOURCE_OFFICIAL_DATA)

    def test_requirement_citing_a_digest_is_traceable_official_data(self):
        item = req("Python", refs=["E02"])
        self.assertEqual(requirement_source(item, self.evidence), SOURCE_OFFICIAL_DATA)
        citation = requirement_citation(item, self.evidence)
        self.assertTrue(citation["traceable"])
        self.assertIn("岗位要求2", citation["quotes"][0])

    def test_digest_counts_and_split_are_reported(self):
        state = {
            "target_job": "AI应用开发工程师",
            "job_requirements": [req("Python", refs=["E02"])],
            "job_evidence": self.evidence,
        }
        report = build_source_report(state, requirement_basis="knowledge_base")
        self.assertEqual(report["counts"], {"user_jd": 0, "official_data": 1,
                                           "knowledge_base": 0, "model_inference": 0,
                                           "total": 1})
        self.assertEqual(report["evidence_by_source"],
                         {"user_jd": 1, "official_data": 1, "model_inference": 0,
                          "knowledge_base": 0, "unattributed": 0})
        self.assertEqual(report["official_source_ids"], ["jd_hundsun_001"])

    def test_gap_hint_names_the_digest_as_such(self):
        self.assertIn("不是这家公司的原话", gap_source_hint({"requirement_source": SOURCE_OFFICIAL_DATA}))
        self.assertIn("模型推断", gap_source_hint({"requirement_source": SOURCE_MODEL}))
        self.assertEqual(gap_source_hint({"requirement_source": SOURCE_USER_JD}), "")
        self.assertEqual(gap_source_hint({}), "")

    def test_unattributed_evidence_is_not_quietly_counted(self):
        counts = evidence_by_source([JD_EVIDENCE, self.digest, BLANK_EVIDENCE])
        self.assertEqual(counts["unattributed"], 1)
        self.assertEqual(counts["user_jd"] + counts["official_data"], 2)


if __name__ == "__main__":
    unittest.main()
