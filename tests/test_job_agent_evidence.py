"""agents/job_agent.py 证据驱动闭环的单元测试（标准库 unittest）。

隔离策略
--------
- **LLM**：`mock.patch.object(agents.job_agent, "structured_invoke", fake)`。
  必须打**消费方模块**的属性 —— `agents/job_agent.py` 用的是
  `from agents.llm import structured_invoke`，改 `agents.llm` 里的名字无效。
- **检索后端**：本节点不调用检索器，证据由 state 直接注入，无需 fake 后端。
  全程不连接 Ollama。

注意：`job_node` 沿用既有的 `check_non_empty(minimum=3)` 约束，因此每个用例
至少要凑够 3 项技能（`run_job` 会自动补足填充项）。

运行：
    uv run python -m unittest tests.test_job_agent_evidence -v
"""

import sys
import unittest
from pathlib import Path
from typing import get_args
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agents import assessment_agent as AA  # noqa: E402
from agents import job_agent as JA  # noqa: E402


def ev(skill, company, note, rt="required", ctx="qualification", importance=5, level="中级"):
    """构造一条与真实 job_evidence 同形状的证据记录。"""
    return {
        "job_id": "ai_application_engineer",
        "source_id": f"jd_{company}_001",
        "company_slug": company,
        "skill": skill,
        "requirement_type": rt,
        "context": ctx,
        "importance": importance,
        "level": level,
        "note": note,
    }


EVIDENCE = [
    ev("Python", "hundsun", "岗位要求2「熟练掌握Python或Java任一语言」", importance=5, level="中级"),
    ev("Embedding", "zhongzhi", "任职要求4「熟悉 Prompt、RAG、Embedding 等概念」", importance=3, level="初级"),
    ev("Agent", "zhongzhi", "岗位职责2「开发 AI Agent，实践工作流编排」", ctx="responsibility", importance=5),
    ev("后端 API 开发", "hundsun", "岗位要求5「了解Python（Flask/FastAPI）后端框架者优先」",
       rt="preferred", importance=4, level="初级"),
]

# 用来凑够 3 项的下限，不参与断言
FILLERS = ["Docker", "Git", "Linux"]


def req(skill, refs=None, importance=3, level="中级", reason="岗位用途"):
    item = {"skill": skill, "importance": importance, "level": level, "reason": reason}
    if refs is not None:
        item["evidence_refs"] = refs
    return item


def patch_llm(requirements):
    """把 Job Agent 的模型调用换成固定输出。"""

    def fake(prompt, schema):
        return JA.JobAnalysis(requirements=[JA.JobRequirement(**item) for item in requirements])

    return mock.patch.object(JA, "structured_invoke", fake)


def run_job(requirements, evidence=EVIDENCE, retrieval_status="matched"):
    """跑一次 job_node，返回 {skill: requirement}。自动补足到 3 项。"""
    items = list(requirements)
    for filler in FILLERS:
        if len(items) >= 3:
            break
        if all(str(i["skill"]).strip().lower() != filler.lower() for i in items):
            items.append(req(filler))
    state = {"target_job": "AI应用开发工程师", "job_evidence": evidence,
             "retrieval_status": retrieval_status}
    with patch_llm(items):
        out = JA.job_node(state)
    return {item["skill"]: item for item in out["job_requirements"]}


class TestEvidenceDrivenJobAgent(unittest.TestCase):
    # ── 1. 证据确实进入生成过程 ────────────────────────────────────────
    def test_evidence_is_injected_into_prompt(self):
        seen = {}

        def fake(prompt, schema):
            seen["prompt"] = prompt
            return JA.JobAnalysis(
                requirements=[JA.JobRequirement(**req(s)) for s in ("Python", "Docker", "Git")]
            )

        state = {"target_job": "AI应用开发工程师", "job_evidence": EVIDENCE,
                 "retrieval_status": "matched"}
        with mock.patch.object(JA, "structured_invoke", fake):
            JA.job_node(state)

        self.assertIn("E01", seen["prompt"])
        self.assertIn("熟练掌握Python或Java任一语言", seen["prompt"])
        self.assertIn("hundsun", seen["prompt"])

    def test_no_evidence_prompt_says_so(self):
        block = JA.format_evidence_block([], "no_source")
        self.assertIn("no_source", block)
        self.assertIn("没有可用证据", block)

    # ── 2. 直接匹配 → supported ────────────────────────────────────────
    def test_direct_name_match_is_supported(self):
        out = run_job([req("Python", importance=2, level="入门")])
        item = out["Python"]
        self.assertEqual(item["evidence_status"], "supported")
        self.assertEqual(item["evidence_refs"], ["E01"])
        self.assertEqual(item["source_companies"], ["hundsun"])
        # importance 用画像的技能级数据，不用模型给的 2
        self.assertEqual(item["importance"], 5)
        self.assertEqual(item["importance_basis"], "profile_skill_importance")
        # 等级来自 JD 原文措辞，不是模型给的"入门"
        self.assertEqual(item["level"], "高级")
        self.assertEqual(item["level_basis"]["origin"], "jd_text")
        self.assertEqual(item["level_basis"]["quote"], "熟练")

    # ── 3. 别名 / 名称不同 → partially_supported ───────────────────────
    def test_alias_match_is_partially_supported(self):
        item = run_job([req("向量数据库")])["向量数据库"]
        self.assertEqual(item["evidence_status"], "partially_supported")
        self.assertEqual(item["evidence_refs"], ["E02"])  # Embedding 那条
        self.assertEqual(item["source_companies"], ["zhongzhi"])

    def test_model_citation_partially_supports(self):
        """规则层解不了的名称（FastAPI），靠模型引用 → 仍只能算部分支持。"""
        item = run_job([req("FastAPI", refs=["E04"])])["FastAPI"]
        self.assertEqual(item["evidence_status"], "partially_supported")
        self.assertEqual(item["evidence_refs"], ["E04"])
        self.assertEqual(item["source_companies"], ["hundsun"])

    def test_substring_name_is_not_direct_evidence(self):
        """名字里"包含"Agent 不等于直接证据 —— 必须降为部分支持。"""
        item = run_job([req("Agent 与工作流编排")])["Agent 与工作流编排"]
        self.assertEqual(item["evidence_status"], "partially_supported")
        self.assertEqual(item["evidence_refs"], ["E03"])

    # ── 4. 无证据 / no_source → model_supplement ──────────────────────
    def test_no_evidence_is_model_supplement(self):
        out = run_job([req("Kubernetes", importance=4, level="初级")], evidence=[],
                      retrieval_status="no_source")
        item = out["Kubernetes"]
        self.assertEqual(item["evidence_status"], "model_supplement")
        self.assertEqual(item["evidence_refs"], [])
        self.assertEqual(item["source_companies"], [])
        self.assertEqual(item["importance_basis"], "model_generated")
        self.assertEqual(item["importance"], 4)
        self.assertEqual(item["level_basis"],
                         {"origin": "model_inferred", "quote": None, "by_company": {}})

    def test_all_items_are_model_supplement_without_evidence(self):
        out = run_job([req("Python")], evidence=[], retrieval_status="index_missing")
        self.assertEqual({i["evidence_status"] for i in out.values()}, {"model_supplement"})

    # ── 5. 虚构 / 无效引用 ────────────────────────────────────────────
    def test_fabricated_refs_are_rejected(self):
        item = run_job([req("Kubernetes", refs=["E99", "XYZ", "E00"])], evidence=[])["Kubernetes"]
        self.assertEqual(item["evidence_status"], "model_supplement")
        self.assertEqual(item["evidence_refs"], [])

    def test_out_of_range_ref_does_not_crash(self):
        out = run_job([req("Kubernetes", refs=["E99"])])
        self.assertEqual(out["Kubernetes"]["evidence_status"], "model_supplement")

    def test_model_text_cannot_forge_company(self):
        """公司只从证据记录里取 —— 模型在 reason 里写别家公司也没用。"""
        item = run_job([req("Python", refs=["E01"], reason="众智诚坚要求精通 Python")])["Python"]
        self.assertEqual(item["source_companies"], ["hundsun"])
        self.assertNotIn("zhongzhi", item["source_companies"])

    # ── 6. 等级来源与多公司冲突 ───────────────────────────────────────
    def test_profile_level_when_jd_is_silent(self):
        """JD 原文没写等级时不能用画像推荐值冒充 JD 明示。"""
        evidence = [ev("Docker", "zhongzhi", "加分项「有 Docker、云服务经验」",
                       rt="bonus", importance=2, level="初级")]
        out = run_job([req("Docker")], evidence=evidence)
        basis = out["Docker"]["level_basis"]
        self.assertEqual(basis["origin"], "profile_level")
        self.assertIsNone(basis["quote"])
        self.assertEqual(out["Docker"]["level"], "初级")

    def test_multi_company_level_conflict_takes_strictest(self):
        evidence = [
            ev("RAG", "hundsun", "岗位要求8「了解Prompt工程技巧、RAG核心流程」", level="初级"),
            ev("RAG", "zhongzhi", "任职要求4「熟悉 Prompt、RAG、Embedding 等概念」", level="初级"),
        ]
        item = run_job([req("RAG")], evidence=evidence)["RAG"]
        self.assertEqual(item["level"], "中级")  # 了解=入门 / 熟悉=中级 → 取最严
        self.assertEqual(item["level_basis"]["origin"], "jd_text")
        self.assertEqual(item["level_basis"]["by_company"],
                         {"hundsun": {"level": "入门", "quote": "了解"},
                          "zhongzhi": {"level": "中级", "quote": "熟悉"}})
        self.assertEqual(item["source_companies"], ["hundsun", "zhongzhi"])

    def test_level_quote_comes_from_real_note(self):
        basis = run_job([req("Python")])["Python"]["level_basis"]
        self.assertIn(basis["quote"], EVIDENCE[0]["note"])

    # ── 7. 兼容性 ─────────────────────────────────────────────────────
    def test_original_fields_sorting_and_dedupe_preserved(self):
        requirements = [req("Python", importance=2), req("Python", importance=5),
                        req("Docker"), req("Git")]
        out = run_job(requirements, evidence=[])
        self.assertEqual(len(out), 3)  # 同名去重
        for item in out.values():
            for key in ("skill", "importance", "level", "reason"):
                self.assertIn(key, item)
        values = [item["importance"] for item in out.values()]
        self.assertEqual(values, sorted(values, reverse=True))

    def test_skill_gaps_structure_keeps_original_fields(self):
        """新证据字段是**追加**的：原有八字段一个不少，下游旧消费方不受影响。"""
        items = list(run_job([req("Python"), req("向量数据库"), req("Kubernetes")]).values())

        def fake_assess(prompt, schema):
            item_model = get_args(schema.model_fields["gaps"].annotation)[0]
            gaps = [
                item_model(skill=item["skill"], required_level=item["level"],
                           supported_by="无", gap="还没掌握")
                for item in items
            ]
            return schema(gaps=gaps)

        with mock.patch.object(AA, "structured_invoke", fake_assess):
            out = AA.assessment_node({
                "user_skills": [{"skill": "Python", "level": "中级", "evidence": "项目里用过"}],
                "job_requirements": items,
            })

        gaps = out["skill_gaps"]
        self.assertEqual(len(gaps), len(items))
        self.assertEqual(
            set(gaps[0]),
            {"skill", "status", "supported_by", "current_level", "required_level",
             "importance", "priority", "gap",
             "display_status", "verification", "evidence_kind",
             "evidence_ref", "evidence_note"},
        )
        self.assertEqual(gaps[0]["current_level"], "中级")  # 学生水平仍来自 user_skills


if __name__ == "__main__":
    unittest.main(verbosity=2)
