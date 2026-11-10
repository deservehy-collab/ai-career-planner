"""岗位依据与匹配结论的本地测试（标准库 unittest）。

覆盖：
1. 依据整理节点：粘了原文逐句切开；没粘原文时按「内置资料摘要 → 空证据」退让
2. 有原话归属数 / 可判定数 / 证据不足数三者严格区分
3. 满足率分母只取可判定数；分母为 0 时不得产出满足率
4. 同技能要求去重
5. 硬性缺口判定（含 supported / partially_supported 的证据强度标注）
6. 来源提示只分「有原文」与「没原文」两档
7. build_final_plan 兼容性（不破坏阶段二/阶段三字段与流程）

隔离策略：`compute_jd_match` / `chunk_user_jd` 是纯函数，`retrieval_node` 是纯文本操作
加一次**仓库内 yaml 查表**（没粘原文时的资料摘要退让），三者都不连 Ollama、
不联网、不写任何文件；证据、要求、缺口全部为测试构造的内存数据。

运行：
    .venv/Scripts/python.exe -m unittest tests.test_jd_match -v
"""

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agents.external_sources import (  # noqa: E402
    MAX_OFFICIAL_EVIDENCE,
    OFFICIAL_PROVENANCE,
)
from agents.jd_match import compute_jd_match  # noqa: E402
from agents.retrieval_agent import (  # noqa: E402
    STATUS_NO_SOURCE,
    STATUS_OFFICIAL_DIGEST,
    STATUS_USER_PASTED,
    retrieval_node,
)
from agents.user_jd import USER_JD_PROVENANCE, USER_JD_SOURCE_ID  # noqa: E402
from graph.state import CareerState  # noqa: E402
from graph.workflow import build_final_plan  # noqa: E402


def make_evidence(*notes):
    """按 retrieval_node 的产出形状构造原话记录（编号 = 数组位置 + 1）。"""
    return [
        {
            "job_id": USER_JD_SOURCE_ID,
            "source_id": USER_JD_SOURCE_ID,
            "company_slug": None,
            "skill": None,
            "requirement_type": USER_JD_PROVENANCE,
            "context": "用户粘贴的招聘要求",
            "importance": None,
            "level": None,
            "provenance": USER_JD_PROVENANCE,
            "note": note,
        }
        for note in notes
    ]


def make_req(skill, refs, evidence_status="supported", importance=5):
    return {
        "skill": skill,
        "evidence_refs": refs,
        "evidence_status": evidence_status,
        "importance": importance,
    }


def make_gap(skill, status, current_level="无", required_level="中级", importance=5,
             priority="高"):
    return {
        "skill": skill,
        "status": status,
        "supported_by": "无",
        "current_level": current_level,
        "required_level": required_level,
        "importance": importance,
        "priority": priority,
        "gap": "缺这项能力",
    }


class RetrievalNodeTests(unittest.TestCase):
    """节点 3 只做了切句这一件事，产出形状就是 job_evidence 数组。"""

    def test_pasted_text_becomes_one_record_per_sentence(self):
        out = retrieval_node({
            "target_job": "AI应用开发工程师",
            "jd_text": "1. 精通 Python；熟悉 Docker。\n2. 了解 RAG 检索链路。\n3. 加分项：有 Agent 编排经验",
        })
        self.assertEqual(out["retrieval_status"], STATUS_USER_PASTED)
        self.assertEqual(len(out["job_evidence"]), 4)
        self.assertEqual([e["source_id"] for e in out["job_evidence"]], [USER_JD_SOURCE_ID] * 4)
        self.assertEqual(out["retrieval_diagnostics"]["scope_count"], 4)
        self.assertTrue(out["job_evidence"][0]["note"].startswith("精通 Python"))

    def test_missing_target_job_raises(self):
        """原文不知道对应哪个岗位就不能开跑 —— 宁可报错，不能猜。"""
        with self.assertRaises(ValueError):
            retrieval_node({"jd_text": "精通 Python；熟悉 Docker。"})

    def test_no_pasted_text_uses_builtin_digest_for_known_job(self):
        """目录里的岗位没粘原文时，改用画像登记过的资料摘要 —— 仍然一句一条可回查。"""
        out = retrieval_node({"target_job": "AI应用开发工程师"})
        self.assertEqual(out["retrieval_status"], STATUS_OFFICIAL_DIGEST)
        self.assertTrue(out["job_evidence"])
        self.assertTrue(
            all(e["provenance"] == OFFICIAL_PROVENANCE for e in out["job_evidence"])
        )
        self.assertTrue(all(str(e["source_id"]).startswith("jd_") for e in out["job_evidence"]))
        coverage = out["retrieval_diagnostics"]["coverage"]
        self.assertEqual(coverage["user_jd"], 0)
        self.assertEqual(coverage["official"], len(out["job_evidence"]))
        # 摘要条数有上限，超出部分由 dropped_entries 如实报数
        self.assertLessEqual(len(out["job_evidence"]), MAX_OFFICIAL_EVIDENCE)
        self.assertGreater(out["retrieval_diagnostics"]["dropped_entries"], 0)

    def test_unrecognized_job_without_pasted_text_still_has_no_basis(self):
        """认不出岗位时不许拿别的岗位的整理稿凑数：仍然是空证据 + 全模型推断。"""
        out = retrieval_node({"target_job": "宠物美容师"})
        self.assertEqual(out["retrieval_status"], STATUS_NO_SOURCE)
        self.assertEqual(out["job_evidence"], [])
        self.assertEqual(out["retrieval_diagnostics"]["reason"], "no_jd_text")
        self.assertEqual(out["retrieval_diagnostics"]["coverage"], {"user_jd": 0, "official": 0, "source_ids": []})

    def test_headings_only_text_degrades_with_its_own_reason(self):
        """全是小标题/序号残留时原因要写清楚，不能和"没粘"混成一句。"""
        out = retrieval_node({"target_job": "AI应用开发工程师", "jd_text": "任职要求：\n1.\n2."})
        self.assertEqual(out["retrieval_status"], STATUS_NO_SOURCE)
        self.assertEqual(out["job_evidence"], [])
        self.assertEqual(out["retrieval_diagnostics"]["reason"], "user_jd_empty")

    def test_overlong_text_reports_dropped_sentence_count(self):
        """超长原文被截断必须如实带出句数，否则用户以为后半段也读过了。"""
        jd_text = "；".join(f"第{i}项技能要求需要能够独立落地实施" for i in range(1, 46))
        out = retrieval_node({"target_job": "AI应用开发工程师", "jd_text": jd_text})
        self.assertEqual(len(out["job_evidence"]), 40)
        self.assertEqual(out["retrieval_diagnostics"]["dropped_sentences"], 5)


class MatchTests(unittest.TestCase):
    def test_satisfied_overall(self):
        evidence = make_evidence("精通 Python，能独立排查线上问题")
        reqs = [make_req("Python", ["E01"], "supported", importance=5)]
        gaps = [make_gap("Python", "已满足", current_level="中级")]
        match = compute_jd_match(gaps, reqs, evidence)
        self.assertEqual(match["requirement_total"], 1)
        self.assertEqual(match["requirement_satisfied"], 1)
        self.assertEqual(match["requirement_shortfall"], 0)
        self.assertEqual(match["overall"], "satisfied")
        self.assertEqual(match["hard_blocks"], [])

    def test_partial_overall_with_gap(self):
        evidence = make_evidence("精通 Python，能独立排查线上问题")
        reqs = [make_req("Python", ["E01"], "supported", importance=5)]
        gaps = [make_gap("Python", "明显不足")]
        match = compute_jd_match(gaps, reqs, evidence)
        self.assertEqual(match["requirement_shortfall"], 1)
        self.assertEqual(match["overall"], "partial")

    def test_insufficient_when_no_attached_requirement(self):
        """没有可判定的要求 → insufficient，不强行给满足率。"""
        match = compute_jd_match([make_gap("Python", "已满足")], [], [])
        self.assertEqual(match["requirement_total"], 0)
        self.assertEqual(match["overall"], "insufficient")

    def test_insufficient_when_no_resolvable_gap(self):
        """要求反查到原话，但 assessment 没有该项 → 证据不足，不进分子也不进分母。"""
        evidence = make_evidence("熟悉 Docker 容器编排与上线流程")
        reqs = [make_req("Docker", ["E01"], "supported", importance=5)]
        match = compute_jd_match([], reqs, evidence)
        self.assertEqual(match["requirement_total"], 1)
        self.assertEqual(match["requirement_satisfied"], 0)
        self.assertEqual(match["requirement_shortfall"], 0)
        self.assertEqual(match["overall"], "insufficient")

    def test_model_supplement_without_refs_is_not_counted(self):
        """模型补充（无原话支撑）的要求不进任何统计 —— 不能把推测算成差距。"""
        evidence = make_evidence("精通 Python，能独立排查线上问题")
        reqs = [
            make_req("Python", ["E01"], "supported", importance=5),
            make_req("Kubernetes", [], "model_supplement", importance=5),
            make_req("MCP", ["E99"], "model_supplement", importance=5),  # 编号回查不到
        ]
        gaps = [make_gap("Python", "已满足"), make_gap("Kubernetes", "明显不足"),
                make_gap("MCP", "明显不足")]
        match = compute_jd_match(gaps, reqs, evidence)
        self.assertEqual(match["requirement_total"], 1)
        self.assertEqual(match["requirement_judged"], 1)
        self.assertEqual({g["skill"] for g in match["key_gaps"]}, {"Python"})


class DedupTests(unittest.TestCase):
    def test_duplicate_skill_counted_once(self):
        evidence = make_evidence("精通 Python，能独立排查线上问题")
        reqs = [
            make_req("Python", ["E01"], "supported", importance=5),
            make_req("python", ["E01"], "supported", importance=5),  # 重复（大小写/空格）
        ]
        gaps = [make_gap("Python", "已满足")]
        match = compute_jd_match(gaps, reqs, evidence)
        self.assertEqual(match["requirement_total"], 1)


class HardBlockTests(unittest.TestCase):
    def test_hard_block_detected(self):
        evidence = make_evidence("扎实的计算机基础与数据结构与算法能力")
        reqs = [make_req("数据结构与算法", ["E01"], "supported", importance=5)]
        gaps = [make_gap("数据结构与算法", "明显不足")]  # current_level=无，默认
        match = compute_jd_match(gaps, reqs, evidence)
        self.assertEqual(len(match["hard_blocks"]), 1)
        self.assertEqual(match["hard_blocks"][0]["skill"], "数据结构与算法")

    def test_non_none_current_level_not_hard_block(self):
        evidence = make_evidence("精通 Python，能独立排查线上问题")
        reqs = [make_req("Python", ["E01"], "supported", importance=5)]
        gaps = [make_gap("Python", "明显不足", current_level="初级")]  # 有基础，非"无"
        match = compute_jd_match(gaps, reqs, evidence)
        self.assertEqual(match["hard_blocks"], [])


class CountingTests(unittest.TestCase):
    """计数口径：有原话归属数 ≠ 可判定数 ≠ 满足率分母。"""

    def _run(self, requirements, gaps, evidence):
        return compute_jd_match(gaps, requirements, evidence)

    def test_attributed_judged_and_orphan_are_distinct(self):
        # 5 条归属要求：1 条已满足、1 条有差距、3 条在 skill_gaps 里对不上号
        skills = ["Python", "Docker", "RAG", "Agent", "Git"]
        evidence = make_evidence(*[f"要求掌握 {s}，并能落地到日常交付中" for s in skills])
        reqs = [make_req(s, [f"E{i:02d}"], "supported", importance=5)
                for i, s in enumerate(skills, start=1)]
        gaps = [make_gap("Python", "已满足"), make_gap("Docker", "明显不足")]

        match = self._run(reqs, gaps, evidence)
        self.assertEqual(match["requirement_total"], 5)      # 有原话归属
        self.assertEqual(match["requirement_judged"], 2)     # 可判定 = 1 + 1
        self.assertEqual(match["requirement_orphan"], 3)     # 证据不足
        self.assertEqual(match["requirement_satisfied"], 1)
        self.assertEqual(match["requirement_shortfall"], 1)

    def test_match_rate_denominator_is_judged_not_attributed(self):
        """满足率必须是 satisfied / judged，不能拿 total 当分母。"""
        skills = ["Python", "Docker", "RAG"]
        evidence = make_evidence(*[f"要求掌握 {s}，并能落地到日常交付中" for s in skills])
        reqs = [make_req(s, [f"E{i:02d}"], "supported", importance=5)
                for i, s in enumerate(skills, start=1)]
        gaps = [make_gap("Python", "已满足"), make_gap("Docker", "明显不足")]

        match = self._run(reqs, gaps, evidence)
        self.assertEqual(match["requirement_total"], 3)
        self.assertEqual(match["requirement_judged"], 2)
        # 1/3 是错的（拿归属数当分母），1/2 才是对的
        self.assertAlmostEqual(match["match_rate"], 0.5)

    def test_no_match_rate_when_nothing_is_judged(self):
        evidence = make_evidence("熟悉 Docker 容器编排与上线流程")
        reqs = [make_req("Docker", ["E01"], "supported", importance=5)]
        match = self._run(reqs, [], evidence)  # assessment 给不出对应项
        self.assertEqual(match["requirement_total"], 1)
        self.assertEqual(match["requirement_judged"], 0)
        self.assertEqual(match["requirement_orphan"], 1)
        self.assertIsNone(match["match_rate"], "分母为 0 时不得产出满足率")
        self.assertEqual(match["overall"], "insufficient")

    def test_all_satisfied_orphans_still_insufficient(self):
        """要求有原话、能力也够，但一条都对不上号 —— 仍是 insufficient。"""
        evidence = make_evidence("精通 Python，能独立排查线上问题")
        reqs = [make_req("Python", ["E01"], "supported", importance=5)]
        match = self._run(reqs, [], evidence)
        self.assertEqual(match["overall"], "insufficient")


class SourceWarningTests(unittest.TestCase):
    """来源提示只剩两档：有没有你粘的原文。措辞不能越界。"""

    def test_pasted_source_limits_conclusion_to_that_text(self):
        warnings = compute_jd_match([], [], make_evidence("精通 Python，能独立排查线上问题"))["jd_source_warning"]
        self.assertTrue(any("你粘贴的招聘要求原文" in w for w in warnings), warnings)
        self.assertFalse(any("行业通识推断" in w for w in warnings), warnings)

    def test_no_source_says_the_whole_conclusion_is_model_guesswork(self):
        """没粘原文时必须直说"由模型推断"，且不能假装对照过某份 JD。"""
        warnings = compute_jd_match([], [], [])["jd_source_warning"]
        self.assertTrue(any("模型按行业通识推断" in w for w in warnings), warnings)
        self.assertFalse(any("你粘贴" in w for w in warnings), warnings)

    def test_warning_is_always_present_as_a_list_of_strings(self):
        for evidence in ([], make_evidence("精通 Python，能独立排查线上问题")):
            warnings = compute_jd_match([], [], evidence)["jd_source_warning"]
            self.assertIsInstance(warnings, list)
            self.assertEqual(len(warnings), 1)
            self.assertIsInstance(warnings[0], str)


class CompatibilityTests(unittest.TestCase):
    def test_build_final_plan_keeps_existing_keys_and_adds_jd_match(self):
        state = {
            "target_job": "AI应用开发工程师",
            "skill_gaps": [make_gap("Python", "已满足")],
            "learning_plan": [],
            "project_plan": [],
            "job_requirements": [],
            "job_evidence": [],
        }
        plan = build_final_plan(state)
        for key in ("target_job", "already_satisfied", "top_gaps",
                    "learning_stage_count", "recommended_projects", "next_action"):
            self.assertIn(key, plan)
        self.assertIn("jd_match", plan)
        self.assertEqual(plan["jd_match"]["overall"], "insufficient")

    def test_state_declares_pasted_text_and_not_the_removed_kb_inputs(self):
        """向量检索入参已从 state 移除；粘贴原文是唯一的岗位依据入口。"""
        names = set(CareerState.__annotations__)
        self.assertIn("jd_text", names)
        self.assertIn("final_plan", names)
        self.assertNotIn("jd_match", names)          # 没有任何节点写这个死字段
        self.assertNotIn("jd_source_id", names)
        self.assertNotIn("target_company", names)

    def test_final_plan_jd_match_exposes_the_five_counts(self):
        state = {
            "target_job": "AI应用开发工程师",
            "skill_gaps": [],
            "learning_plan": [],
            "project_plan": [],
            "job_requirements": [],
            "job_evidence": [],
        }
        match = build_final_plan(state)["jd_match"]
        for key in ("requirement_total", "requirement_judged", "requirement_orphan",
                    "requirement_satisfied", "requirement_shortfall", "match_rate"):
            self.assertIn(key, match)
        # 公司/JD 维度的字段随向量检索一起退场
        for key in ("jd_target", "matched_jd_count"):
            self.assertNotIn(key, match)


class HardBlockEvidenceStatusTests(unittest.TestCase):
    """硬性缺口保留原判定条件，但必须能区分证据强度。"""

    def _run(self, evidence_status):
        evidence = make_evidence("扎实的计算机基础与数据结构与算法能力")
        reqs = [make_req("数据结构与算法", ["E01"], evidence_status, importance=5)]
        gaps = [make_gap("数据结构与算法", "明显不足")]  # current_level 默认 "无"
        return compute_jd_match(gaps, reqs, evidence)

    def test_supported_is_labelled(self):
        blocks = self._run("supported")["hard_blocks"]
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]["evidence_status"], "supported")

    def test_partially_supported_is_labelled_and_distinguishable(self):
        blocks = self._run("partially_supported")["hard_blocks"]
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]["evidence_status"], "partially_supported")

    def test_model_supplement_is_not_a_hard_block(self):
        """没有任何原话支撑的要求不进硬性缺口。"""
        self.assertEqual(self._run("model_supplement")["hard_blocks"], [])

    def test_partially_supported_does_not_turn_current_level_into_none(self):
        """证据强度与被测者能力是两回事：有基础的人不得因弱证据被算成"无"。"""
        evidence = make_evidence("精通 Python，能独立排查线上问题")
        reqs = [make_req("Python", ["E01"], "partially_supported", importance=5)]
        gaps = [make_gap("Python", "部分满足", current_level="初级")]
        match = compute_jd_match(gaps, reqs, evidence)
        self.assertEqual(match["hard_blocks"], [])
        self.assertEqual(match["requirement_shortfall"], 1)


if __name__ == "__main__":
    unittest.main()
