"""阶段三验收报告 S1–S8 的回归测试（标准库 unittest）。

覆盖：
- S1 技能名归一（唯一命中才采用；没命中/有歧义都记录，不猜）
- S2 covers 规范化、已满足/低优先级剔除、高优先级缺口覆盖率告警
- S3 项目可执行性：高级项目与前置全缺项目不得作为当前项目
- S4 covers_gaps 能映射回原始 skill_gaps 名称
- S5 无知识库依据的缺口必须显式列出，不得伪造成知识库标准
- S6 岗位能力分类标签不得当作独立技能；标签与规范名重复要合并
- S7 难度真实参与评分（旧版那一项恒为 0）
- S8 等级一致性校验与总时长上限（只报告，不删缺口）

隔离策略：
- LLM：patch 消费方模块上的 `structured_invoke`（`agents.learning_agent` /
  `agents.project_agent` / `agents.job_agent`），全程不连 Ollama。
- 知识库：Skill / Project 知识库用**真实文件**做只读查询（快、不写、不建索引）；
  词表用真实 `SkillVocabulary`（只读岗位画像 YAML）。

运行：
    uv run python -m unittest tests.test_stage3_validation -v
"""

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agents import job_agent as JA  # noqa: E402
from agents import learning_agent as LA  # noqa: E402
from agents import project_agent as PA  # noqa: E402
from agents.project_knowledge import DIFFICULTY_RANK, ProjectKnowledgeBase  # noqa: E402
from agents.skill_dict import SkillVocabulary  # noqa: E402
from agents.skill_knowledge import SkillKnowledgeBase  # noqa: E402
from agents.utils import build_skill_mapping, resolve_canonical  # noqa: E402

VOCAB = SkillVocabulary()
SKILL_BASE = SkillKnowledgeBase()

# 验收报告里四个"查不到知识库"的技能名，逐字照抄
AGENT_LABEL = "Agent与工作流编排"
BACKEND_LABEL = "后端服务"
DEPLOY_LABEL = "工程化与部署"
LLM_LABEL = "大模型调用与提示词工程"
# 同时沾上 Prompt Engineering 与 Embedding 两个规范技能 —— 归一必须判成"认不准"
AMBIGUOUS_LABEL = "提示词与向量检索"


def gap(skill, priority="高", importance=5, required="中级", current="无", status="明显不足"):
    return {"skill": skill, "status": status, "supported_by": "无", "current_level": current,
            "required_level": required, "importance": importance, "priority": priority,
            "gap": "缺这项能力"}


def stage(number, covers, goal="补缺口", criteria="能做出一个可验收的东西",
          weeks=4, topic="阶段主题"):
    return {"stage": number, "topic": topic, "goal": goal, "covers": list(covers),
            "prerequisite": "Python（中级）", "completion_criteria": criteria,
            "estimated_weeks": weeks}


# ══════════════════════════════════════════════════════════════════════
# S1 技能名归一
# ══════════════════════════════════════════════════════════════════════
class TestSkillNameNormalization(unittest.TestCase):
    def test_report_labels_resolve_as_observed(self):
        """报告里四个未命中名：三个能唯一归一，一个归不了 —— 逐项钉死。"""
        self.assertEqual(resolve_canonical(AGENT_LABEL, VOCAB), "Agent")
        self.assertEqual(resolve_canonical(BACKEND_LABEL, VOCAB), "后端 API 开发")
        self.assertEqual(resolve_canonical(LLM_LABEL, VOCAB), "Prompt Engineering")
        self.assertIsNone(resolve_canonical(DEPLOY_LABEL, VOCAB))

    def test_ambiguous_name_is_not_guessed(self):
        """名字同时沾到两个规范技能 → 有歧义，必须返回 None，不许挑一个。"""
        ambiguous = "LangChain 和 RAG 都要会"
        self.assertGreaterEqual(len(VOCAB.match_detailed(ambiguous)), 2)
        self.assertIsNone(resolve_canonical(ambiguous, VOCAB))

    def test_unknown_name_returns_none(self):
        self.assertIsNone(resolve_canonical("完全不存在的技能", VOCAB))

    def test_none_vocabulary_degrades(self):
        self.assertIsNone(resolve_canonical("Python", None))

    def test_canonical_name_maps_to_itself(self):
        self.assertEqual(resolve_canonical("Python", VOCAB), "Python")

    def test_build_skill_mapping_splits_hit_and_miss(self):
        mapping, unmapped = build_skill_mapping(
            [AGENT_LABEL, BACKEND_LABEL, DEPLOY_LABEL, LLM_LABEL], VOCAB
        )
        self.assertEqual(mapping, {
            AGENT_LABEL: "Agent",
            BACKEND_LABEL: "后端 API 开发",
            LLM_LABEL: "Prompt Engineering",
        })
        self.assertEqual(unmapped, [DEPLOY_LABEL])

    def test_normalize_covers_keeps_original_to_canonical_mapping(self):
        canonical, mapping, unmapped = LA.normalize_covers(
            [BACKEND_LABEL, "Python", DEPLOY_LABEL], VOCAB
        )
        self.assertEqual(canonical, ["后端 API 开发", "Python"])
        self.assertEqual(mapping[BACKEND_LABEL], "后端 API 开发")
        self.assertEqual(mapping["Python"], "Python")
        self.assertEqual(unmapped, [DEPLOY_LABEL])

    def test_normalize_covers_dedupes_same_canonical(self):
        """两种写法归一到同一技能时，covers 里只留一个。"""
        canonical, mapping, unmapped = LA.normalize_covers(
            [LLM_LABEL, "Prompt Engineering"], VOCAB
        )
        self.assertEqual(canonical, ["Prompt Engineering"])
        self.assertEqual(len(mapping), 2)
        self.assertEqual(unmapped, [])

    def test_split_compound_gap_resolves_to_the_gap_name(self):
        """模型把一个复合缺口拆成几个词回报 → 仍要认回那个缺口，不能整条丢掉。

        实测触发过：缺口写「变频器、伺服驱动、传感器等工控元件」，covers 回报
        ["变频器","伺服驱动","传感器"]，逐字相等一个都对不上，那条学习任务 skills
        恒为空，打卡只留完成记录、不涨等级。
        """
        compound = "变频器、伺服驱动、传感器等工控元件"
        canonical, mapping, unmapped = LA.normalize_covers(
            ["变频器", "伺服驱动", "传感器"], VOCAB, [gap(compound)]
        )
        self.assertEqual(canonical, [compound])
        self.assertEqual(set(mapping), {"变频器", "伺服驱动", "传感器"})
        self.assertEqual(unmapped, [])

    def test_substring_hit_on_two_gaps_is_not_guessed(self):
        """同一段文字沾上两个缺口 → 认不准，照旧丢弃，不许挑一个。"""
        canonical, _, unmapped = LA.normalize_covers(
            ["变频器"], VOCAB, [gap("变频器调试"), gap("变频器选型")]
        )
        self.assertEqual(canonical, [])
        self.assertEqual(unmapped, ["变频器"])

    def test_exact_gap_name_wins_over_substring(self):
        """有逐字相等的缺口名时用它；包含它的长名字不得把这一项判成歧义。"""
        canonical, _, unmapped = LA.normalize_covers(
            ["变频器"], VOCAB, [gap("变频器与伺服调试"), gap("变频器")]
        )
        self.assertEqual(canonical, ["变频器"])
        self.assertEqual(unmapped, [])

    def test_out_of_range_number_is_not_resolved_by_substring(self):
        """编号越界时不许退化成"这个数字是某个缺口名里的字符"。"""
        canonical, _, unmapped = LA.normalize_covers(
            ["3"], VOCAB, [gap("S7-1200/1500 PLC 编程")]
        )
        self.assertEqual(canonical, [])
        self.assertEqual(unmapped, ["3"])

    def test_single_character_is_not_resolved_by_substring(self):
        """单个字太弱，不足以认定它指哪个缺口。"""
        canonical, _, unmapped = LA.normalize_covers(["电"], VOCAB, [gap("电气控制与自动化")])
        self.assertEqual(canonical, [])
        self.assertEqual(unmapped, ["电"])

    def test_learning_node_does_not_touch_skill_gaps(self):
        """归一过程不得改写调用方传进来的 skill_gaps。"""
        gaps = [gap(AGENT_LABEL), gap(BACKEND_LABEL), gap(DEPLOY_LABEL)]
        before = copy.deepcopy(gaps)

        def fake(prompt, schema):
            return LA.LearningPlan(stages=[
                LA.LearningStage(stage=1, topic="A", goal="g", covers=[AGENT_LABEL],
                                 prerequisite="Python", completion_criteria="c", estimated_weeks=1),
                LA.LearningStage(stage=2, topic="B", goal="g", covers=[BACKEND_LABEL],
                                 prerequisite="Python", completion_criteria="c", estimated_weeks=1),
            ])

        with mock.patch.object(LA, "structured_invoke", fake):
            LA.learning_node({"target_job": "AI应用开发工程师", "skill_gaps": gaps})
        self.assertEqual(gaps, before)


# ══════════════════════════════════════════════════════════════════════
# S2 / S5 covers 修正与覆盖率
# ══════════════════════════════════════════════════════════════════════
class TestCoversRepairAndCoverage(unittest.TestCase):
    def test_low_priority_and_satisfied_are_removed_from_covers(self):
        """Python 已满足（priority=低）→ 不得继续占据 covers。"""
        stages = [stage(1, ["Python", "测试与效果评测"])]
        issues = LA.repair_stages(stages, [gap("Python", priority="低", status="已满足"),
                                           gap("测试与效果评测")], VOCAB)
        self.assertEqual(stages[0]["covers"], ["测试与效果评测"])
        self.assertEqual(stages[0]["dropped_low_priority"], ["Python"])
        self.assertTrue(any("低优先级" in i for i in issues))

    def test_ambiguous_cover_is_recorded_not_guessed(self):
        """沾上两个规范技能的名字照旧不记账 —— 这条红线没因为下面的放宽而松动。"""
        stages = [stage(1, [AMBIGUOUS_LABEL, "测试与效果评测"])]
        issues = LA.repair_stages(stages, [gap(AMBIGUOUS_LABEL), gap("测试与效果评测")], VOCAB)
        self.assertEqual(stages[0]["covers"], ["测试与效果评测"])
        self.assertEqual(stages[0]["unmapped_covers"], [AMBIGUOUS_LABEL])
        self.assertTrue(any(AMBIGUOUS_LABEL in i for i in issues))

    def test_gap_name_outside_vocabulary_stays_in_covers(self):
        """词表没收录、但确实是本用户缺口的技能名，按原名留在 covers 里。

        这就是"电气/护理岗的学习任务永远进不了成长记录"那一刀的回归点：
        以前这种名字一律当噪声删掉，任务 skills 恒为空。
        """
        stages = [stage(1, [DEPLOY_LABEL])]
        issues = LA.repair_stages(stages, [gap(DEPLOY_LABEL)], VOCAB)
        self.assertEqual(stages[0]["covers"], [DEPLOY_LABEL])
        self.assertEqual(stages[0]["unmapped_covers"], [])
        self.assertFalse(any("covers 为空" in i for i in issues))

    def test_split_compound_gap_leaves_stage_covering_it(self):
        """复合缺口被拆开回报时，修正后 covers 仍要指回那个缺口（否则打卡不涨等级）。"""
        compound = "变频器、伺服驱动、传感器等工控元件"
        stages = [stage(1, ["变频器", "伺服驱动", "传感器"])]
        issues = LA.repair_stages(stages, [gap(compound)], VOCAB)
        self.assertEqual(stages[0]["covers"], [compound])
        self.assertEqual(stages[0]["unmapped_covers"], [])
        self.assertEqual(stages[0]["skill_mapping"], {
            "变频器": compound, "伺服驱动": compound, "传感器": compound,
        })
        self.assertFalse(any("covers 为空" in i for i in issues))

    def test_covers_written_as_gap_numbers_are_resolved_and_traced(self):
        """模型把 covers 写成缺口编号时按编号还原（那套编号本来就是我们自己打的）。"""
        stages = [stage(1, ["1", "2"])]
        LA.repair_stages(stages, [gap("电气控制与自动化"), gap("电路设计与分析")], VOCAB)
        self.assertEqual(stages[0]["covers"], ["电气控制与自动化", "电路设计与分析"])
        self.assertEqual(stages[0]["skill_mapping"],
                         {"1": "电气控制与自动化", "2": "电路设计与分析"})
        self.assertEqual(stages[0]["unmapped_covers"], [])

    def test_covers_number_out_of_range_is_not_guessed(self):
        stages = [stage(1, ["9"])]
        LA.repair_stages(stages, [gap("电气控制与自动化")], VOCAB)
        self.assertEqual(stages[0]["covers"], [])
        self.assertEqual(stages[0]["unmapped_covers"], ["9"])

    def test_number_resolved_to_gap_name_is_still_canonicalized(self):
        """编号还原出来的缺口名要再过一遍词表 —— 规范名不能从这条路上漏掉。"""
        stages = [stage(1, ["1"])]
        LA.repair_stages(stages, [gap(BACKEND_LABEL)], VOCAB)
        self.assertEqual(stages[0]["covers"], ["后端 API 开发"])
        self.assertEqual(stages[0]["skill_mapping"], {"1": "后端 API 开发"})

    def test_stage_left_empty_is_reported(self):
        stages = [stage(1, ["词表里没有、缺口里也没有的东西"])]
        issues = LA.repair_stages(stages, [gap(DEPLOY_LABEL)], VOCAB)
        self.assertEqual(stages[0]["covers"], [])
        self.assertTrue(any("covers 为空" in i for i in issues))

    def test_coverage_reports_uncovered_high_gap(self):
        """报告里最重要的那条：Agent与工作流编排 无人覆盖，必须报出来。"""
        stages = [stage(1, ["Prompt Engineering"])]
        issues = LA.check_coverage(stages, [gap(AGENT_LABEL), gap("Prompt Engineering")], VOCAB)
        self.assertTrue(any(AGENT_LABEL in i for i in issues))
        self.assertEqual(len(issues), 1)

    def test_coverage_silent_when_all_high_gaps_covered(self):
        stages = [stage(1, ["Agent"])]
        self.assertEqual(LA.check_coverage(stages, [gap(AGENT_LABEL)], VOCAB), [])

    def test_coverage_ignores_low_priority_gaps(self):
        stages = [stage(1, ["Agent"])]
        self.assertEqual(
            LA.check_coverage(stages, [gap("Python", priority="低")], VOCAB), []
        )

    def test_gap_outside_vocabulary_is_still_checked(self):
        """词表没收录的缺口现在也能核对覆盖情况 —— 原名就是 covers 里的合法技能名。"""
        issues = LA.check_coverage([stage(1, [])], [gap(DEPLOY_LABEL)], VOCAB)
        self.assertTrue(any("没有被任何学习阶段覆盖" in i for i in issues))
        self.assertEqual(
            LA.check_coverage([stage(1, [DEPLOY_LABEL])], [gap(DEPLOY_LABEL)], VOCAB), []
        )

    def test_gate_stops_a_route_that_links_no_skill_at_all(self):
        """一条技能都没关联上的路线必须失败，不能生成交不了差的任务清单。"""
        with self.assertRaises(ValueError) as ctx:
            LA.require_any_covered_skill([stage(1, []), stage(2, [])])
        self.assertIn("成长记录", str(ctx.exception))

    def test_gate_passes_when_any_stage_has_a_skill(self):
        LA.require_any_covered_skill([stage(1, []), stage(2, [DEPLOY_LABEL])])

    def test_node_surfaces_unmapped_gap_in_prompt(self):
        """S5：查不到知识库的缺口要显式写进 prompt，并说明不是知识库来源。"""
        captured = {}

        def fake(prompt, schema):
            captured["prompt"] = prompt
            return LA.LearningPlan(stages=[
                LA.LearningStage(stage=1, topic="A", goal="g", covers=["Agent"],
                                 prerequisite="Python", completion_criteria="c", estimated_weeks=1),
                LA.LearningStage(stage=2, topic="B", goal="g", covers=["Prompt Engineering"],
                                 prerequisite="Python", completion_criteria="c", estimated_weeks=1),
            ])

        with mock.patch.object(LA, "structured_invoke", fake):
            LA.learning_node({
                "target_job": "AI应用开发工程师",
                "skill_gaps": [gap(AGENT_LABEL), gap(DEPLOY_LABEL)],
            })
        self.assertIn(DEPLOY_LABEL, captured["prompt"])
        self.assertIn("能力标准不在知识库收录范围内", captured["prompt"])
        # 知识库确实按规范名查到了 Agent 的等级标准
        self.assertIn("【Agent】", captured["prompt"])
        # 归一后的规范名进了知识库查询，原始标签名不再出现在标准块里
        self.assertNotIn(f"【{AGENT_LABEL}】", captured["prompt"])

    def test_kb_block_falls_back_when_base_missing(self):
        self.assertIn("知识库暂时不可用", LA._skill_knowledge_block(["Agent"], None))
        self.assertIn("无差距技能可检索", LA._skill_knowledge_block([], SKILL_BASE))


# ══════════════════════════════════════════════════════════════════════
# S8 等级一致性与总时长
# ══════════════════════════════════════════════════════════════════════
class TestLevelAndDuration(unittest.TestCase):
    def test_total_weeks_reports_overage_without_dropping_gaps(self):
        stages = [stage(1, ["Agent"], weeks=30), stage(2, ["Prompt Engineering"], weeks=30)]
        issues = LA.check_total_weeks(stages, limit=52)
        self.assertEqual(len(issues), 1)
        self.assertIn("60 周", issues[0])
        self.assertIn("没有用删减缺口", issues[0])
        # 缺口与阶段一个都没被删
        self.assertEqual(len(stages), 2)
        self.assertEqual(stages[0]["covers"], ["Agent"])

    def test_total_weeks_silent_within_limit(self):
        self.assertEqual(LA.check_total_weeks([stage(1, ["Agent"], weeks=10)], limit=52), [])

    def test_total_weeks_ignores_dirty_values(self):
        self.assertEqual(LA.total_weeks([{"estimated_weeks": "8"}, {"estimated_weeks": None}]), 8)

    def test_goal_level_conflict_is_detected(self):
        """报告里的真实案例：goal 说"高级特性"，完成标准抄的是知识库中级标准。"""
        python_mid = SKILL_BASE.get("Python")["level_guides"]["中级"]
        s = stage(1, ["Python"], goal="掌握Python的高级特性", criteria=python_mid)
        issues = LA.check_level_consistency(s, [gap("Python", priority="低")], SKILL_BASE, VOCAB)
        self.assertTrue(any("不一致" in i for i in issues))

    def test_consistent_levels_pass(self):
        s = stage(1, ["Python"], goal="达到中级水平",
                  criteria="能独立完成模块化开发，会写类、用类型注解")
        self.assertEqual(
            LA.check_level_consistency(s, [gap("Python", required="中级")], SKILL_BASE, VOCAB), []
        )

    def test_level_conflicts_with_required_level(self):
        """阶段自己说的等级没错，但和岗位要求等级对不上，同样要报。"""
        s = stage(1, ["Python"], goal="达到高级水平", criteria="达到高级水平")
        issues = LA.check_level_consistency(s, [gap("Python", required="中级")], SKILL_BASE, VOCAB)
        self.assertTrue(any("岗位要求等级" in i for i in issues))

    def test_validate_plan_runs_every_check(self):
        stages = [stage(1, ["Python", DEPLOY_LABEL], goal="掌握Python的高级特性",
                        criteria=SKILL_BASE.get("Python")["level_guides"]["中级"], weeks=57)]
        gaps = [gap("Python", priority="低", status="已满足"), gap(AGENT_LABEL), gap(DEPLOY_LABEL)]
        issues = LA.validate_learning_plan(stages, gaps, VOCAB, SKILL_BASE)
        blob = "\n".join(issues)
        self.assertIn("低优先级", blob)          # S2
        self.assertIn(AGENT_LABEL, blob)          # S2 覆盖率
        self.assertIn(DEPLOY_LABEL, blob)         # S5
        self.assertIn("超过上限", blob)            # S8 时长
        self.assertIn("不一致", blob)              # S8 等级


# ══════════════════════════════════════════════════════════════════════
# S3 / S7 项目可执行性与评分
# ══════════════════════════════════════════════════════════════════════
class TestProjectSelection(unittest.TestCase):
    def setUp(self):
        self.base = ProjectKnowledgeBase()

    def names(self, items):
        return [p["name"] for p in items]

    def test_advanced_project_blocked_for_insufficient_prereqs(self):
        """综合项目（advanced）不得作为当前执行项目推给前置不足的学生。"""
        student = {"Python": "中级", "RAG": "中级"}
        executable, blocked = self.base.recommend_for_user(["Python", "RAG"], student)
        self.assertNotIn("AI 求职规划助手", self.names(executable))
        blocked_names = self.names(blocked)
        self.assertIn("AI 求职规划助手", blocked_names)
        entry = next(p for p in blocked if p["name"] == "AI 求职规划助手")
        self.assertIn("advanced", entry["blocked_reason"])
        self.assertIn("Agent", entry["missing_prerequisites"])

    def test_executable_is_sorted_easy_to_hard(self):
        student = {"Python": "中级"}
        executable, _ = self.base.recommend_for_user(["Python", "Agent", "RAG"], student)
        ranks = [DIFFICULTY_RANK[p["difficulty"]] for p in executable]
        self.assertEqual(ranks, sorted(ranks))
        self.assertGreaterEqual(len(executable), 2)

    def test_project_losing_every_prerequisite_is_blocked(self):
        """前置一个都不具备 → 不当作能立即做的项目。"""
        executable, blocked = self.base.recommend_for_user(["Agent"], {"Docker": "入门"})
        self.assertNotIn("RAG 知识库问答系统", self.names(executable))
        entry = next(p for p in blocked if p["name"] == "RAG 知识库问答系统")
        self.assertEqual(entry["blocked_reason"], "前置技能一个都不具备")
        self.assertEqual(sorted(entry["missing_prerequisites"]),
                         ["Embedding", "Prompt Engineering", "Python"])

    def test_prerequisites_not_enforced_when_student_unknown(self):
        """拿不到学生数据时不过滤前置，避免凭空把人挡在门外。"""
        executable, _ = self.base.recommend_for_user(["Python", "RAG"], None)
        self.assertIn("RAG 知识库问答系统", self.names(executable))

    def test_difficulty_actually_affects_the_score(self):
        """S7：难度必须真实参与评分。旧版那一项恒为 0，等于没算。"""
        projects = [
            {"name": "E", "difficulty": "beginner", "covers_skills": ["Python"],
             "prerequisites": [], "tech_stack": [], "stage_hint": "", "deliverable": "",
             "acceptance_criteria": [], "estimated_weeks": 1},
            {"name": "M", "difficulty": "intermediate", "covers_skills": ["Python"],
             "prerequisites": [], "tech_stack": [], "stage_hint": "", "deliverable": "",
             "acceptance_criteria": [], "estimated_weeks": 1},
        ]
        import tempfile

        import yaml
        with tempfile.TemporaryDirectory() as d:
            for i, p in enumerate(projects):
                (Path(d) / f"p{i}.yaml").write_text(
                    yaml.safe_dump(p, allow_unicode=True), encoding="utf-8"
                )
            base = ProjectKnowledgeBase(directory=Path(d))

        executable, _ = base.recommend_for_user(["Python"], None)
        by_name = {p["name"]: p for p in executable}
        self.assertEqual(by_name["E"]["coverage"], by_name["M"]["coverage"])
        # 同等覆盖下，越简单分越高
        self.assertGreater(by_name["E"]["score"], by_name["M"]["score"])

    def test_recommend_keeps_coverage_first_for_backward_compat(self):
        """`recommend()` 仍是覆盖优先（既有调用方的语义不变）。"""
        recs = self.base.recommend(["Python", "RAG", "Agent", "后端 API 开发"])
        self.assertEqual(recs[0]["name"], "AI 求职规划助手")

    def test_agent_uses_executable_by_default(self):
        """注入 prompt 的候选只含 beginner/intermediate，高级项目进"暂不推荐"。"""
        block = PA._load_project_templates(
            ["Agent", "RAG"], [], {"Python": "中级", "RAG": "中级"}
        )
        self.assertIn("难度从易到难", block)
        self.assertIn("暂不适合作当前执行的项目", block)
        self.assertIn("AI 求职规划助手", block.split("暂不适合作当前执行的项目")[1])
        candidates = block.split("【以下项目")[0]
        self.assertNotIn("AI 求职规划助手", candidates)


# ══════════════════════════════════════════════════════════════════════
# S4 covers_gaps 回映射
# ══════════════════════════════════════════════════════════════════════
class TestCoversGapsAlignment(unittest.TestCase):
    def test_canonical_names_map_back_to_gap_names(self):
        projects = [{"name": "P", "covers_gaps": ["Agent", "后端 API 开发", "RAG"]}]
        gaps = [gap(AGENT_LABEL), gap(BACKEND_LABEL), gap("RAG")]
        issues = PA.align_covers_gaps(projects, gaps, VOCAB)
        self.assertEqual(projects[0]["covers_gaps"],
                         [AGENT_LABEL, BACKEND_LABEL, "RAG"])
        self.assertEqual(issues, [])

    def test_gap_names_are_left_alone(self):
        projects = [{"name": "P", "covers_gaps": [AGENT_LABEL]}]
        PA.align_covers_gaps(projects, [gap(AGENT_LABEL)], VOCAB)
        self.assertEqual(projects[0]["covers_gaps"], [AGENT_LABEL])

    def test_unmappable_skill_is_kept_and_reported(self):
        projects = [{"name": "P", "covers_gaps": ["Kubernetes"]}]
        issues = PA.align_covers_gaps(projects, [gap(AGENT_LABEL)], VOCAB)
        self.assertEqual(projects[0]["covers_gaps"], ["Kubernetes"])
        self.assertTrue(any("无法关联" in i for i in issues))

    def test_duplicates_are_collapsed(self):
        projects = [{"name": "P", "covers_gaps": [AGENT_LABEL, "Agent"]}]
        PA.align_covers_gaps(projects, [gap(AGENT_LABEL)], VOCAB)
        self.assertEqual(projects[0]["covers_gaps"], [AGENT_LABEL])


class TestBlockedProjectGuard(unittest.TestCase):
    def test_project_matching_blocked_template_is_flagged(self):
        """模型无视提示、换个名字重推综合项目时，仍要告警。"""
        blocked = [{"name": "AI 求职规划助手", "blocked_reason": "难度 advanced 超限",
                    "covers_skills": ["Python", "RAG", "Agent", "后端 API 开发",
                                      "测试与效果评测", "Docker"]}]
        projects = [{"name": "AI 智能求职助手",
                     "covers_gaps": ["Python", "RAG", "Agent", "后端 API 开发", "Docker"]}]
        issues = PA.check_blocked_projects(projects, blocked)
        self.assertEqual(len(issues), 1)
        self.assertIn("AI 求职规划助手", issues[0])

    def test_unrelated_project_is_not_flagged(self):
        blocked = [{"name": "X", "blocked_reason": "r", "covers_skills": ["Docker", "Git"]}]
        projects = [{"name": "Y", "covers_gaps": ["Python"]}]
        self.assertEqual(PA.check_blocked_projects(projects, blocked), [])

    def test_difficulty_order_check_flags_regression(self):
        """能对上知识库模板的项目顺序倒退时要报警；对不上的项目跳过。"""
        base = ProjectKnowledgeBase()
        projects = [{"name": "AI Agent 服务"}, {"name": "AI 智能问答助手"}]
        issues = PA.check_difficulty_order(projects, base)
        self.assertEqual(len(issues), 1)
        self.assertIn("难度递进", issues[0])

    def test_difficulty_order_skips_unknown_names(self):
        base = ProjectKnowledgeBase()
        projects = [{"name": "我自己编的项目 A"}, {"name": "我自己编的项目 B"}]
        self.assertEqual(PA.check_difficulty_order(projects, base), [])


# ══════════════════════════════════════════════════════════════════════
# S6 岗位能力分类标签
# ══════════════════════════════════════════════════════════════════════
class TestCategoryLabels(unittest.TestCase):
    def test_label_detection(self):
        self.assertTrue(JA.is_category_label(AGENT_LABEL))
        self.assertTrue(JA.is_category_label(BACKEND_LABEL))
        self.assertTrue(JA.is_category_label(DEPLOY_LABEL))
        self.assertFalse(JA.is_category_label("Python"))
        self.assertFalse(JA.is_category_label("Agent"))

    def test_unmappable_label_is_dropped(self):
        """工程化与部署在规范技能表里没有对应项 → 不能当作一个独立技能。"""
        dropped, merged = [], []
        kept = JA.dedupe_by_canonical(
            JA.drop_category_labels([{"skill": "Python"}, {"skill": DEPLOY_LABEL}], dropped),
            merged,
        )
        self.assertEqual([r["skill"] for r in kept], ["Python"])
        self.assertEqual(dropped, [DEPLOY_LABEL])

    def test_mappable_label_is_kept_for_dedupe(self):
        dropped, merged = [], []
        kept = JA.dedupe_by_canonical(
            JA.drop_category_labels([{"skill": BACKEND_LABEL}], dropped), merged
        )
        self.assertEqual([r["skill"] for r in kept], [BACKEND_LABEL])
        self.assertEqual(dropped, [])

    def test_label_and_its_skill_merge_into_canonical_name(self):
        """S6 的关键：这是报告里"技能重复 + 状态矛盾"的根源。"""
        reqs = [{"skill": LLM_LABEL}, {"skill": "Prompt Engineering"}]
        dropped, merged = [], []
        kept = JA.dedupe_by_canonical(JA.drop_category_labels(reqs, dropped), merged)
        self.assertEqual([r["skill"] for r in kept], ["Prompt Engineering"])
        self.assertEqual(merged, [LLM_LABEL])

    def test_same_skill_different_wording_of_plain_names(self):
        """不含标签的重复写法同样合并（LangChain / LangGraph）。"""
        kept = JA.dedupe_by_canonical(
            [{"skill": "LangGraph"}, {"skill": "LangChain"}], []
        )
        self.assertEqual(len(kept), 1)

    def test_unrelated_skills_are_not_merged(self):
        kept = JA.dedupe_by_canonical([{"skill": "Python"}, {"skill": "RAG"}], [])
        self.assertEqual([r["skill"] for r in kept], ["Python", "RAG"])


class TestCategoryLabelsInJobNode(unittest.TestCase):
    """整条 job_node 的端到端行为（LLM 打桩）。"""

    def run_job(self, skills, evidence=None, retrieval_status="matched"):
        def fake(prompt, schema):
            return JA.JobAnalysis(requirements=[
                JA.JobRequirement(skill=s, importance=3, level="中级", reason="用于岗位职责")
                for s in skills
            ])

        state = {"target_job": "AI应用开发工程师", "job_evidence": evidence or [],
                 "retrieval_status": retrieval_status}
        with mock.patch.object(JA, "structured_invoke", fake):
            return JA.job_node(state)

    def test_real_report_requirements_are_cleaned(self):
        """复现验收报告里那一组输出：标签被清掉、重复被合并。"""
        out = self.run_job(["Python", AGENT_LABEL, BACKEND_LABEL, DEPLOY_LABEL,
                            LLM_LABEL, "测试与效果评测", "Prompt Engineering"])
        skills = [r["skill"] for r in out["job_requirements"]]
        self.assertNotIn(DEPLOY_LABEL, skills)         # 归不了 → 丢弃
        self.assertNotIn(LLM_LABEL, skills)            # 与 Prompt Engineering 重复 → 合并
        self.assertIn("Prompt Engineering", skills)
        self.assertEqual(len(skills), len(set(skills)))
        self.assertEqual(len(skills), 5)

    def test_no_contradiction_between_merged_pair(self):
        """合并后不再出现"同一职能既是已满足又是明显不足"的两条。"""
        out = self.run_job(["Python", AGENT_LABEL, LLM_LABEL, "Prompt Engineering"])
        skills = [r["skill"] for r in out["job_requirements"]]
        self.assertEqual(skills.count("Prompt Engineering"), 1)
        self.assertFalse([s for s in skills if s == LLM_LABEL])

    def test_cleanup_falls_back_when_below_minimum(self):
        """清理后不足 3 条 → 整条回退，保住节点不崩、也不丢分类标签。"""
        out = self.run_job(["Python", "测试与效果评测", DEPLOY_LABEL])
        skills = [r["skill"] for r in out["job_requirements"]]
        self.assertIn(DEPLOY_LABEL, skills)
        self.assertEqual(len(skills), 3)

    def test_job_requirements_shape_unchanged(self):
        out = self.run_job(["Python", AGENT_LABEL, BACKEND_LABEL, DEPLOY_LABEL,
                            LLM_LABEL, "测试与效果评测", "Prompt Engineering"])
        for item in out["job_requirements"]:
            for key in ("skill", "importance", "level", "reason",
                        "evidence_status", "evidence_refs", "source_companies",
                        "importance_basis", "level_basis"):
                self.assertIn(key, item)

    def test_sorted_by_importance_after_cleanup(self):
        def fake(prompt, schema):
            return JA.JobAnalysis(requirements=[
                JA.JobRequirement(skill="Python", importance=5, level="中级", reason="r"),
                JA.JobRequirement(skill="Agent", importance=2, level="中级", reason="r"),
                JA.JobRequirement(skill="RAG", importance=4, level="中级", reason="r"),
            ])

        with mock.patch.object(JA, "structured_invoke", fake):
            out = JA.job_node({"target_job": "AI应用开发工程师", "job_evidence": [],
                               "retrieval_status": "matched"})
        values = [r["importance"] for r in out["job_requirements"]]
        self.assertEqual(values, sorted(values, reverse=True))


# ══════════════════════════════════════════════════════════════════════
# B4：项目匹配前把缺口名归一为规范技能名
# ══════════════════════════════════════════════════════════════════════
class TestGapSkillNormalizationForProjects(unittest.TestCase):
    def setUp(self):
        self.base = ProjectKnowledgeBase()

    # ── 唯一归一 ─────────────────────────────────────────────────────
    def test_agent_label_normalizes_to_agent(self):
        canonical, unmapped = PA.normalize_gap_skills([AGENT_LABEL], VOCAB)
        self.assertEqual(canonical, ["Agent"])
        self.assertEqual(unmapped, [])

    def test_backend_label_normalizes_to_backend_api(self):
        canonical, unmapped = PA.normalize_gap_skills([BACKEND_LABEL], VOCAB)
        self.assertEqual(canonical, ["后端 API 开发"])
        self.assertEqual(unmapped, [])

    def test_already_canonical_names_pass_through(self):
        canonical, unmapped = PA.normalize_gap_skills(["Python", "RAG"], VOCAB)
        self.assertEqual(canonical, ["Python", "RAG"])
        self.assertEqual(unmapped, [])

    def test_same_canonical_is_deduped(self):
        """LangChain 与 LangGraph 是同一个规范技能，只保留一个。"""
        canonical, unmapped = PA.normalize_gap_skills(["LangChain", "LangGraph"], VOCAB)
        self.assertEqual(canonical, ["LangChain / LangGraph"])
        self.assertEqual(unmapped, [])

    # ── 未匹配 / 有歧义：不猜 ────────────────────────────────────────
    def test_unmappable_label_is_excluded_not_guessed(self):
        canonical, unmapped = PA.normalize_gap_skills([DEPLOY_LABEL], VOCAB)
        self.assertEqual(canonical, [])
        self.assertEqual(unmapped, [DEPLOY_LABEL])

    def test_ambiguous_name_is_excluded_not_guessed(self):
        ambiguous = "LangChain 和 RAG 都要会"
        self.assertGreaterEqual(len(VOCAB.match_detailed(ambiguous)), 2)
        canonical, unmapped = PA.normalize_gap_skills([ambiguous], VOCAB)
        self.assertEqual(canonical, [])
        self.assertEqual(unmapped, [ambiguous])

    def test_vocabulary_unavailable_degrades_to_empty(self):
        canonical, unmapped = PA.normalize_gap_skills([AGENT_LABEL], None)
        self.assertEqual(canonical, [])
        self.assertEqual(unmapped, [AGENT_LABEL])

    def test_unmapped_never_inflates_coverage(self):
        """归一失败的名字不许被当成知识库里的某个具体技能算进覆盖度。"""
        raw = [DEPLOY_LABEL, "LangChain 和 RAG 都要会"]
        canonical, _ = PA.normalize_gap_skills(raw, VOCAB)
        executable, _ = self.base.recommend_for_user(canonical, {"Python": "中级"})
        self.assertTrue(all(p["coverage"] == 0 for p in executable))

    # ── 覆盖度 / matched_skills 修正 ─────────────────────────────────
    def test_coverage_is_wrong_before_fix_and_right_after(self):
        """B4 的核心证据：传原始名覆盖度恒为 0，归一后才是真实值。"""
        raw = [AGENT_LABEL, BACKEND_LABEL, DEPLOY_LABEL, "RAG"]
        student = {"Python": "中级"}

        before = {p["name"]: p for p in self.base.recommend_for_user(raw, student)[0]}
        self.assertEqual(before["AI Agent 服务"]["coverage"], 0)
        self.assertEqual(before["AI Agent 服务"]["matched_skills"], [])

        canonical, unmapped = PA.normalize_gap_skills(raw, VOCAB)
        self.assertEqual(canonical, ["Agent", "后端 API 开发", "RAG"])
        self.assertEqual(unmapped, [DEPLOY_LABEL])
        after = {p["name"]: p for p in self.base.recommend_for_user(canonical, student)[0]}
        self.assertEqual(after["AI Agent 服务"]["coverage"], 2)
        self.assertEqual(sorted(after["AI Agent 服务"]["matched_skills"]),
                         ["Agent", "后端 API 开发"])
        self.assertEqual(after["RAG 知识库问答系统"]["coverage"], 1)
        self.assertEqual(after["RAG 知识库问答系统"]["matched_skills"], ["RAG"])

    def test_same_difficulty_order_no_longer_broken_by_naming(self):
        """同难度两个候选：只有归一后，覆盖度才会把该排前的项目排到前面。"""
        def project(name, covers):
            return {"name": name, "difficulty": "beginner", "covers_skills": covers,
                    "prerequisites": [], "tech_stack": ["Python"], "stage_hint": "",
                    "deliverable": "d", "acceptance_criteria": ["a"], "estimated_weeks": 1}

        import yaml
        with tempfile.TemporaryDirectory() as d:
            for i, p in enumerate([project("A", ["Python"]), project("B", ["Agent"])]):
                (Path(d) / f"p{i}.yaml").write_text(
                    yaml.safe_dump(p, allow_unicode=True), encoding="utf-8"
                )
            base = ProjectKnowledgeBase(directory=Path(d))

        raw = [AGENT_LABEL]
        before = [p["name"] for p in base.recommend_for_user(raw, None)[0]]
        self.assertEqual(set(before), {"A", "B"})  # 两者覆盖度都是 0，只能靠名字排

        canonical, _ = PA.normalize_gap_skills(raw, VOCAB)
        after = base.recommend_for_user(canonical, None)[0]
        self.assertEqual(after[0]["name"], "B")     # 归一后 B 覆盖 Agent，排第一
        self.assertEqual(after[0]["coverage"], 1)

    # ── 原始数据不被改 ──────────────────────────────────────────────
    def test_normalize_does_not_touch_input_list(self):
        raw = [AGENT_LABEL, BACKEND_LABEL, DEPLOY_LABEL]
        snapshot = copy.deepcopy(raw)
        PA.normalize_gap_skills(raw, VOCAB)
        self.assertEqual(raw, snapshot)

    def test_project_node_does_not_mutate_gaps_or_learning_plan(self):
        gaps = [gap(AGENT_LABEL), gap(BACKEND_LABEL), gap(DEPLOY_LABEL)]
        plan = [stage(1, ["Agent"], weeks=4), stage(2, ["后端 API 开发"], weeks=4)]
        gaps_before, plan_before = copy.deepcopy(gaps), copy.deepcopy(plan)

        def fake(prompt, schema):
            return PA.ProjectPlan(projects=[
                PA.ProjectItem(name="P", goal="补缺口", covers_gaps=["Agent"],
                               tech_stack=["Python"], stage="阶段1", deliverable="d")])

        with mock.patch.object(PA, "structured_invoke", fake):
            PA.project_node({"target_job": "AI应用开发工程师", "skill_gaps": gaps,
                             "learning_plan": plan,
                             "user_skills": [{"skill": "Python", "level": "中级"}]})
        self.assertEqual(gaps, gaps_before)
        self.assertEqual(plan, plan_before)

    # ── covers_gaps 输出映射仍是独立的一环 ───────────────────────────
    def test_covers_gaps_still_maps_back_to_original_gap_names(self):
        """B4 的输入归一变规范名，S4 的输出映射变回原名 —— 两件事各管一头。"""
        gaps = [gap(AGENT_LABEL), gap(BACKEND_LABEL)]

        def fake(prompt, schema):
            return PA.ProjectPlan(projects=[
                PA.ProjectItem(name="P", goal="补缺口",
                               covers_gaps=["Agent", "后端 API 开发"],
                               tech_stack=["Python"], stage="阶段1", deliverable="d")])

        with mock.patch.object(PA, "structured_invoke", fake):
            out = PA.project_node({"target_job": "AI应用开发工程师", "skill_gaps": gaps,
                                   "learning_plan": [],
                                   "user_skills": [{"skill": "Python", "level": "中级"}]})
        self.assertEqual(out["project_plan"][0]["covers_gaps"], [AGENT_LABEL, BACKEND_LABEL])


# ══════════════════════════════════════════════════════════════════════
# B：终端隐藏内部追踪字段
# ══════════════════════════════════════════════════════════════════════
TRACKING_FIELDS = ("skill_mapping", "unmapped_covers", "dropped_low_priority")


class TestTerminalDisplayFields(unittest.TestCase):
    def setUp(self):
        import main  # 有 __main__ 保护，导入不会触发工作流
        self.main = main

    def _internal_stage(self):
        return {
            "stage": 1, "topic": "Agent 编排", "goal": "补 Agent 缺口",
            "covers": ["Agent"], "prerequisite": "Python（中级）",
            "completion_criteria": "能做出一个多工具 Agent 服务", "estimated_weeks": 4,
            "skill_mapping": {AGENT_LABEL: "Agent"},
            "unmapped_covers": [DEPLOY_LABEL],
            "dropped_low_priority": ["Python"],
        }

    def test_learning_node_keeps_tracking_fields_internally(self):
        """内部 state 必须继续保留追踪字段（本任务明确要求不删）。"""

        def fake(prompt, schema):
            return LA.LearningPlan(stages=[
                LA.LearningStage(stage=1, topic="A", goal="g", covers=[AGENT_LABEL],
                                 prerequisite="Python", completion_criteria="c",
                                 estimated_weeks=1),
                LA.LearningStage(stage=2, topic="B", goal="g", covers=["Prompt Engineering"],
                                 prerequisite="Python", completion_criteria="c",
                                 estimated_weeks=1),
            ])

        with mock.patch.object(LA, "structured_invoke", fake):
            out = LA.learning_node({"target_job": "AI应用开发工程师",
                                    "skill_gaps": [gap(AGENT_LABEL), gap("Prompt Engineering")]})
        for stage_item in out["learning_plan"]:
            self.assertIn("skill_mapping", stage_item)
            self.assertIn("unmapped_covers", stage_item)
        self.assertEqual(out["learning_plan"][0]["skill_mapping"], {AGENT_LABEL: "Agent"})

    def test_projection_hides_every_tracking_field(self):
        shown = self.main.public_learning_plan([self._internal_stage()])
        blob = json.dumps(shown, ensure_ascii=False)
        for field in TRACKING_FIELDS:
            self.assertNotIn(field, blob)
            self.assertNotIn(field, shown[0])
        # 追踪字段的值也不应该漏出来
        self.assertNotIn(DEPLOY_LABEL, blob)
        self.assertNotIn(AGENT_LABEL, blob)

    def test_seven_business_fields_preserved_with_values_and_types(self):
        internal = self._internal_stage()
        shown = self.main.public_learning_plan([internal])[0]
        self.assertEqual(tuple(shown), self.main.LEARNING_STAGE_FIELDS)
        self.assertEqual(set(shown), {"stage", "topic", "goal", "covers", "prerequisite",
                                      "completion_criteria", "estimated_weeks"})
        for key in self.main.LEARNING_STAGE_FIELDS:
            self.assertEqual(shown[key], internal[key])
            self.assertIs(type(shown[key]), type(internal[key]))

    def test_projection_does_not_mutate_internal_stage(self):
        internal = self._internal_stage()
        snapshot = copy.deepcopy(internal)
        self.main.public_learning_plan([internal])
        self.assertEqual(internal, snapshot)
        self.assertIn("skill_mapping", internal)

    def test_stage_without_dropped_low_priority_renders(self):
        internal = self._internal_stage()
        internal.pop("dropped_low_priority")     # 该键本来就是"非空才写"
        shown = self.main.public_learning_plan([internal])
        self.assertEqual(len(shown), 1)
        self.assertEqual(shown[0]["stage"], 1)
        self.assertNotIn("dropped_low_priority", shown[0])

    def test_empty_and_none_are_safe(self):
        self.assertEqual(self.main.public_learning_plan([]), [])
        self.assertEqual(self.main.public_learning_plan(None), [])

    def test_non_dict_entries_are_skipped(self):
        shown = self.main.public_learning_plan([self._internal_stage(), "垃圾数据", None])
        self.assertEqual(len(shown), 1)


# ══════════════════════════════════════════════════════════════════════
# 问题 1：学习路线漏掉高优先级缺口 —— 代码自动补齐
# ══════════════════════════════════════════════════════════════════════
def _stage_spec(topic, covers, weeks=4, goal="补这个缺口", criteria="能做出一个可验收的东西"):
    return LA.LearningStage(stage=1, topic=topic, goal=goal, covers=list(covers),
                            prerequisite="Python（中级）", completion_criteria=criteria,
                            estimated_weeks=weeks)


def _run_learning(gaps, specs, user_skills=None):
    """跑一次 learning_node（LLM 打桩），返回 (输出, stdout)。"""
    import contextlib
    import io

    def fake(prompt, schema):
        return LA.LearningPlan(stages=specs)

    buf = io.StringIO()
    state = {"target_job": "AI应用开发工程师", "skill_gaps": gaps,
             "user_skills": user_skills or [{"skill": "Python", "level": "中级"}]}
    with mock.patch.object(LA, "structured_invoke", fake), contextlib.redirect_stdout(buf):
        out = LA.learning_node(state)
    return out["learning_plan"], buf.getvalue()


def _final_issues(stages, gaps):
    return LA.validate_learning_plan(stages, gaps, VOCAB, SKILL_BASE)


FIVE_HIGH = ["Agent", "LLM API", "后端 API 开发", "Docker", "Git"]


class TestCoverageBackfill(unittest.TestCase):
    def test_acceptance_five_high_gaps_all_covered_after_node(self):
        """验收用例：5 个高优先级缺口，模型只覆盖 3 个，代码必须补齐其余 2 个。

        补齐后过一遍 validate_learning_plan，不应再出现"优先级 高 没有被任何
        学习阶段覆盖"的报警。
        """
        gaps = [gap(name) for name in FIVE_HIGH]
        specs = [_stage_spec("Agent", ["Agent"]), _stage_spec("LLM API", ["LLM API"]),
                 _stage_spec("后端 API 开发", ["后端 API 开发"])]
        stages, log = _run_learning(gaps, specs)

        covered = LA.covered_skills(stages)
        self.assertTrue(set(FIVE_HIGH) <= covered, f"仍有未被覆盖的高优先级缺口：{set(FIVE_HIGH) - covered}")
        self.assertEqual(len(stages), 5)
        self.assertIn("已用代码自动补齐 2 个未被覆盖的高优先级缺口", log)

        issues = _final_issues(stages, gaps)
        self.assertEqual([i for i in issues if "没有被任何学习阶段覆盖" in i], [])

    def test_backfilled_stage_reuses_knowledge_base_level_standard(self):
        """补出来的阶段复用知识库等级标准原文，不编内容。"""
        gaps = [gap("Docker", required="中级")]
        specs = [_stage_spec("别的", ["Agent"]), _stage_spec("再一个", ["LLM API"])]
        stages, _ = _run_learning(gaps, specs, user_skills=[{"skill": "Python", "level": "中级"}])

        new = [s for s in stages if s.get("backfilled")]
        self.assertEqual(len(new), 1)
        entry = SKILL_BASE.get("Docker")
        self.assertEqual(new[0]["completion_criteria"], entry["level_guides"]["中级"])
        self.assertEqual(new[0]["covers"], ["Docker"])
        self.assertEqual(new[0]["estimated_weeks"], entry["estimated_weeks"])
        self.assertEqual(new[0]["prerequisite"], "、".join(entry["prerequisites"]))

    def test_backfill_stage_is_marked_and_numbered_last(self):
        gaps = [gap("Git")]
        specs = [_stage_spec("A", ["Agent"]), _stage_spec("B", ["LLM API"])]
        stages, _ = _run_learning(gaps, specs)
        self.assertTrue(stages[-1]["backfilled"])
        self.assertEqual([s["stage"] for s in stages], [1, 2, 3])

    def test_no_backfill_when_everything_is_covered(self):
        gaps = [gap("Agent"), gap("LLM API")]
        specs = [_stage_spec("A", ["Agent"]), _stage_spec("B", ["LLM API"])]
        stages, log = _run_learning(gaps, specs)
        self.assertEqual(len(stages), 2)
        self.assertNotIn("自动补齐", log)
        self.assertFalse(any(s.get("backfilled") for s in stages))

    def test_low_priority_gaps_are_never_backfilled(self):
        gaps = [gap("Agent"), gap("Docker", priority="低", status="已满足")]
        specs = [_stage_spec("A", ["Agent"]), _stage_spec("B", ["LLM API"])]
        stages, _ = _run_learning(gaps, specs)
        self.assertNotIn("Docker", LA.covered_skills(stages))

    def test_unresolvable_high_gap_is_reported_not_fabricated(self):
        """归一不了的缺口不能凭空补一个阶段出来 —— 那样只是编内容。"""
        gaps = [gap(AGENT_LABEL), gap(DEPLOY_LABEL)]
        specs = [_stage_spec("A", [AGENT_LABEL]), _stage_spec("B", ["LLM API"])]
        stages, log = _run_learning(gaps, specs)
        self.assertNotIn("自动补齐", log)
        self.assertFalse(any(s.get("backfilled") for s in stages))
        self.assertTrue(any(DEPLOY_LABEL in i for i in _final_issues(stages, gaps)))

    def test_backfill_helper_round_limit(self):
        gaps = [gap("Docker")]
        stages = []
        notes = LA.backfill_uncovered_gaps(stages, gaps, VOCAB, SKILL_BASE, max_rounds=0)
        self.assertEqual(notes, [])
        self.assertEqual(stages, [])

    def test_backfill_helper_is_idempotent(self):
        gaps = [gap("Docker")]
        stages = []
        LA.backfill_uncovered_gaps(stages, gaps, VOCAB, SKILL_BASE)
        first = copy.deepcopy(stages)
        second_notes = LA.backfill_uncovered_gaps(stages, gaps, VOCAB, SKILL_BASE)
        self.assertEqual(second_notes, [])          # 已覆盖，不再补
        self.assertEqual(stages, first)             # 也不重复追加

    def test_backfill_without_knowledge_base_falls_back_safely(self):
        """知识库不可用时补的阶段仍要合法，不能带 None 字段。"""
        gaps = [gap("Docker", required="中级")]
        stages = []
        LA.backfill_uncovered_gaps(stages, gaps, VOCAB, None)
        self.assertEqual(len(stages), 1)
        for key in ("stage", "topic", "goal", "covers", "prerequisite",
                    "completion_criteria", "estimated_weeks"):
            self.assertIsNotNone(stages[0][key])
        self.assertIn("Docker", stages[0]["completion_criteria"])

    def test_uncovered_high_gaps_helper(self):
        gaps = [gap("Agent"), gap("Docker", importance=5), gap("Git", importance=3),
                gap("LLM API", priority="低")]
        stages = [{"covers": ["Agent"]}]
        missing = LA.uncovered_high_gaps(stages, gaps, VOCAB)
        self.assertEqual([m["canonical"] for m in missing], ["Docker", "Git"])  # 重要度降序

    def test_gap_list_declares_high_priority_count(self):
        """提示词里显式声明"高优先级有几条"，并把缺口编号。"""
        text = LA._format_gaps([gap("Agent"), gap("Docker", priority="低", status="已满足")])
        self.assertIn("共 2 项缺口，其中优先级「高」1 项", text)
        self.assertIn("1. [高] Agent", text)
        self.assertIn("2. [低] Docker", text)


# ══════════════════════════════════════════════════════════════════════
# 问题 3：等级措辞向缺口要求等级对齐
# ══════════════════════════════════════════════════════════════════════
class TestLevelWordingAlignment(unittest.TestCase):
    def test_realign_replaces_only_level_words(self):
        text = "掌握Python的高级特性，如类、类型注解"
        self.assertEqual(LA.realign_level_words(text, "中级"),
                         "掌握Python的中级特性，如类、类型注解")

    def test_realign_keeps_text_without_level_words(self):
        text = "能独立完成模块化开发"
        self.assertEqual(LA.realign_level_words(text, "中级"), text)

    def test_realign_replaces_every_conflicting_level_word(self):
        self.assertEqual(LA.realign_level_words("从入门到高级", "初级"), "从初级到初级")

    def test_realign_target_level_untouched(self):
        self.assertEqual(LA.realign_level_words("达到中级", "中级"), "达到中级")

    def test_stage_target_level_takes_strictest(self):
        lookup = LA._gap_lookup([gap("Agent", required="中级"), gap("RAG", required="高级")], VOCAB)
        self.assertEqual(LA._stage_target_level({"covers": ["Agent", "RAG"]}, lookup), "高级")

    def test_align_stage_levels_fixes_goal(self):
        stages = [dict(stage(1, ["Python"], goal="掌握Python的高级特性",
                             criteria="能独立完成模块化开发"))]
        notes = LA.align_stage_levels(stages, [gap("Python", required="中级")], VOCAB, SKILL_BASE)
        self.assertIn("中级", stages[0]["goal"])
        self.assertNotIn("高级", stages[0]["goal"])
        self.assertTrue(notes)

    def test_alignment_skipped_without_target(self):
        stages = [dict(stage(1, [], goal="掌握高级技巧"))]
        LA.align_stage_levels(stages, [gap("Python")], VOCAB, SKILL_BASE)
        self.assertEqual(stages[0]["goal"], "掌握高级技巧")

    def test_level_warning_gone_after_alignment(self):
        """问题 3 的核心：对齐之后等级一致性校验不该再报警。"""
        raw = dict(stage(1, ["Python"], goal="掌握Python的高级特性",
                         criteria="能独立完成模块化开发，会写类、用类型注解"))
        gaps = [gap("Python", required="中级")]
        self.assertTrue(LA.check_level_consistency(raw, gaps, SKILL_BASE, VOCAB))
        LA.align_stage_levels([raw], gaps, VOCAB, SKILL_BASE)
        self.assertEqual(LA.check_level_consistency(raw, gaps, SKILL_BASE, VOCAB), [])

    def test_node_no_longer_reports_level_mismatch(self):
        gaps = [gap("Agent", required="中级"), gap("LLM API", required="中级")]
        specs = [_stage_spec("Agent", ["Agent"], goal="掌握 Agent 的高级用法",
                             criteria="能设计多工具工作流，处理工具调用与上下文管理"),
                 _stage_spec("LLM API", ["LLM API"], goal="掌握 LLM API 的高级用法")]
        stages, log = _run_learning(gaps, specs)
        # 会有"已对齐为「中级」"的说明，但不能再有等级不一致的**警告**
        self.assertIn("等级措辞已对齐为「中级」", log)
        self.assertNotIn("不一致", log)
        self.assertNotIn("高级", stages[0]["goal"])
        self.assertEqual([i for i in _final_issues(stages, gaps) if "等级" in i], [])


# ══════════════════════════════════════════════════════════════════════
# 问题 2：covers_gaps 里不属于本用户缺口的技能名 → 剔除
# ══════════════════════════════════════════════════════════════════════
class TestDropUnlinkedCoversGaps(unittest.TestCase):
    def test_unlinked_skill_is_dropped(self):
        projects = [{"name": "RAG 知识库问答系统",
                     "covers_gaps": ["Prompt Engineering", "Embedding"]}]
        notes = PA.drop_unlinked_covers_gaps(projects, [gap("Prompt Engineering")], VOCAB)
        self.assertEqual(projects[0]["covers_gaps"], ["Prompt Engineering"])
        self.assertEqual(len(notes), 1)
        self.assertIn("Embedding", notes[0])

    def test_mappable_canonical_name_is_kept_for_alignment(self):
        """能回映射到缺口的规范名不能被误删，要留给 align 去映射。"""
        projects = [{"name": "P", "covers_gaps": ["Agent"]}]
        notes = PA.drop_unlinked_covers_gaps(projects, [gap(AGENT_LABEL)], VOCAB)
        self.assertEqual(projects[0]["covers_gaps"], ["Agent"])
        self.assertEqual(notes, [])
        PA.align_covers_gaps(projects, [gap(AGENT_LABEL)], VOCAB)
        self.assertEqual(projects[0]["covers_gaps"], [AGENT_LABEL])

    def test_gap_names_are_left_alone(self):
        projects = [{"name": "P", "covers_gaps": [AGENT_LABEL, "RAG"]}]
        notes = PA.drop_unlinked_covers_gaps(projects, [gap(AGENT_LABEL), gap("RAG")], VOCAB)
        self.assertEqual(projects[0]["covers_gaps"], [AGENT_LABEL, "RAG"])
        self.assertEqual(notes, [])

    def test_notes_say_when_nothing_is_left(self):
        projects = [{"name": "P", "covers_gaps": ["Kubernetes"]}]
        notes = PA.drop_unlinked_covers_gaps(projects, [gap("Agent")], VOCAB)
        self.assertEqual(projects[0]["covers_gaps"], [])
        self.assertIn("已无任何关联本用户缺口的技能", notes[0])

    def test_duplicates_collapse(self):
        projects = [{"name": "P", "covers_gaps": ["Agent", "Agent"]}]
        PA.drop_unlinked_covers_gaps(projects, [gap("Agent")], VOCAB)
        self.assertEqual(projects[0]["covers_gaps"], ["Agent"])

    def test_does_not_touch_other_project_fields(self):
        projects = [{"name": "P", "goal": "g", "tech_stack": ["Python"],
                     "covers_gaps": ["Embedding"]}]
        PA.drop_unlinked_covers_gaps(projects, [gap("Agent")], VOCAB)
        self.assertEqual(projects[0]["goal"], "g")
        self.assertEqual(projects[0]["tech_stack"], ["Python"])

    def test_node_no_longer_warns_about_unlinkable_skill(self):
        """问题 2 的核心：整条 project_node 不再出现"无法关联"报警。"""
        import contextlib
        import io

        gaps = [gap(AGENT_LABEL), gap("LLM API")]

        def fake(prompt, schema):
            return PA.ProjectPlan(projects=[
                PA.ProjectItem(name="RAG 知识库问答系统", goal="补缺口",
                               covers_gaps=["Agent", "Embedding"],
                               tech_stack=["Python"], stage="阶段1", deliverable="d")])

        buf = io.StringIO()
        with mock.patch.object(PA, "structured_invoke", fake), contextlib.redirect_stdout(buf):
            out = PA.project_node({"target_job": "AI应用开发工程师", "skill_gaps": gaps,
                                   "learning_plan": [],
                                   "user_skills": [{"skill": "Python", "level": "中级"}]})
        log = buf.getvalue()
        self.assertNotIn("无法关联", log)
        self.assertIn("已从该项剔除", log)
        self.assertEqual(out["project_plan"][0]["covers_gaps"], [AGENT_LABEL])


# ══════════════════════════════════════════════════════════════════════
# 收尾 1：topic 与 goal 的等级矛盾
# ══════════════════════════════════════════════════════════════════════
class TestTopicLevelAlignment(unittest.TestCase):
    def _stages(self, topic, goal, criteria, covers=("Agent",)):
        return [dict(stage(1, list(covers), topic=topic, goal=goal, criteria=criteria))]

    def test_topic_is_aligned_too(self):
        """问题现场：topic 说「高级技能学习」、goal 说「中级」。"""
        stages = self._stages("高级技能学习", "掌握高级技能以支持 AI 应用开发",
                              "能独立完成任务")
        LA.align_stage_levels(stages, [gap("Agent", required="中级")], VOCAB, SKILL_BASE)
        self.assertEqual(stages[0]["topic"], "中级技能学习")
        self.assertNotIn("高级", stages[0]["topic"])
        self.assertNotIn("高级", stages[0]["goal"])

    def test_no_stage_internal_level_conflict_after_alignment(self):
        stages = self._stages("高级技能学习", "掌握高级技能以支持 AI 应用开发",
                              "达到高级水平")
        LA.align_stage_levels(stages, [gap("Agent", required="中级")], VOCAB, SKILL_BASE)
        mentioned = {f: LA._levels_mentioned(stages[0][f])
                     for f in ("topic", "goal", "completion_criteria")}
        self.assertEqual(mentioned, {"topic": {"中级"}, "goal": {"中级"},
                                     "completion_criteria": {"中级"}})
        self.assertEqual(LA.check_level_consistency(stages[0], [gap("Agent", required="中级")],
                                                    SKILL_BASE, VOCAB), [])

    def test_topic_without_level_word_is_untouched(self):
        stages = self._stages("Agent 编排与工具调用", "掌握高级用法", "能独立完成任务")
        before = stages[0]["topic"]
        LA.align_stage_levels(stages, [gap("Agent", required="中级")], VOCAB, SKILL_BASE)
        self.assertEqual(stages[0]["topic"], before)      # 不含等级词 → 原样不动
        self.assertEqual(stages[0]["goal"], "掌握中级用法")

    def test_note_mentions_topic_when_changed(self):
        stages = self._stages("高级技能学习", "掌握高级技能", "能独立完成任务")
        notes = LA.align_stage_levels(stages, [gap("Agent", required="中级")], VOCAB, SKILL_BASE)
        self.assertTrue(any("topic" in n for n in notes))

    def test_no_target_level_leaves_everything_alone(self):
        stages = self._stages("高级技能学习", "掌握高级技能", "能独立完成任务", covers=())
        snapshot = copy.deepcopy(stages)
        self.assertEqual(LA.align_stage_levels(stages, [gap("Agent")], VOCAB, SKILL_BASE), [])
        self.assertEqual(stages, snapshot)

    def test_node_output_has_no_topic_goal_conflict(self):
        gaps = [gap("Agent", required="中级"), gap("LLM API", required="中级")]
        specs = [_stage_spec("高级技能学习", ["Agent"], goal="掌握 Agent 的高级用法"),
                 _stage_spec("LLM API", ["LLM API"], goal="掌握 LLM API 的高级用法")]
        stages, _ = _run_learning(gaps, specs)
        for s in stages:
            self.assertNotIn("高级", s["topic"])
            self.assertNotIn("高级", s["goal"])
        self.assertEqual(stages[0]["topic"], "中级技能学习")   # 原本写"高级技能学习"
        self.assertEqual(stages[1]["topic"], "LLM API")        # 不含等级词，原样不动


# ══════════════════════════════════════════════════════════════════════
# 收尾 2：删除没有独立价值的空阶段
# ══════════════════════════════════════════════════════════════════════
class TestEmptyStageRemoval(unittest.TestCase):
    def _three_stages_one_empty(self):
        """第 3 个阶段只覆盖已满足的 RAG → 过滤后 covers 为空。"""
        return [dict(stage(1, ["Agent"], topic="Agent", weeks=12)),
                dict(stage(2, ["LLM API"], topic="LLM API", weeks=8)),
                dict(stage(3, ["RAG"], topic="专项技能提升",
                           goal="掌握专项技能", criteria="能独立完成 RAG 知识库建设", weeks=8))]

    def _gaps(self):
        return [gap("Agent"), gap("LLM API"), gap("RAG", priority="低", status="已满足")]

    def test_droppable_reason_for_low_priority_only_stage(self):
        s = {"covers": [], "dropped_low_priority": ["RAG"], "unmapped_covers": []}
        self.assertIn("低优先级", LA.droppable_empty_stage(s))

    def test_stage_with_unmapped_covers_is_not_droppable(self):
        """覆盖项归一不了 → 信息不足，不删。"""
        s = {"covers": [], "dropped_low_priority": [], "unmapped_covers": ["工程化与部署"]}
        self.assertIsNone(LA.droppable_empty_stage(s))

    def test_stage_with_covers_is_not_droppable(self):
        self.assertIsNone(LA.droppable_empty_stage({"covers": ["Agent"]}))

    def test_empty_stage_is_removed_and_weeks_recomputed(self):
        stages, gaps = self._three_stages_one_empty(), self._gaps()
        LA.repair_stages(stages, gaps, VOCAB)
        self.assertEqual(LA.total_weeks(stages), 28)

        notes, warnings = LA.drop_empty_stages(stages, gaps, VOCAB)
        self.assertEqual(warnings, [])
        self.assertEqual(len(stages), 2)
        self.assertEqual(LA.total_weeks(stages), 20)     # 8 周随阶段一起消失
        self.assertTrue(any("已删除空阶段" in n for n in notes))
        self.assertTrue(any("20 周" in n for n in notes))

    def test_remaining_stages_are_renumbered(self):
        stages, gaps = self._three_stages_one_empty(), self._gaps()
        LA.repair_stages(stages, gaps, VOCAB)
        LA.drop_empty_stages(stages, gaps, VOCAB)
        self.assertEqual([s["stage"] for s in stages], [1, 2])

    def test_every_remaining_stage_has_valid_covers(self):
        stages, gaps = self._three_stages_one_empty(), self._gaps()
        LA.repair_stages(stages, gaps, VOCAB)
        LA.drop_empty_stages(stages, gaps, VOCAB)
        for s in stages:
            self.assertTrue(s["covers"])

    def test_high_priority_gaps_still_covered_after_removal(self):
        stages, gaps = self._three_stages_one_empty(), self._gaps()
        LA.repair_stages(stages, gaps, VOCAB)
        LA.drop_empty_stages(stages, gaps, VOCAB)
        self.assertEqual(LA.check_coverage(stages, gaps, VOCAB), [])
        self.assertTrue({"Agent", "LLM API"} <= LA.covered_skills(stages))

    def test_removal_never_breaks_min_stage_count(self):
        """只剩 2 个阶段、其中一个是空阶段 → 不删，改为告警。"""
        stages = [dict(stage(1, ["Agent"], topic="Agent", weeks=12)),
                  dict(stage(2, ["RAG"], topic="专项技能提升", weeks=8))]
        gaps = [gap("Agent"), gap("RAG", priority="低", status="已满足")]
        LA.repair_stages(stages, gaps, VOCAB)

        notes, warnings = LA.drop_empty_stages(stages, gaps, VOCAB)
        self.assertEqual(notes, [])
        self.assertEqual(len(stages), 2)                  # 一个都没删
        self.assertTrue(any("低于下限" in w for w in warnings))

    def test_satisfied_skills_are_not_reintroduced(self):
        """删阶段不等于把已满足的技能塞回别的阶段。"""
        stages, gaps = self._three_stages_one_empty(), self._gaps()
        LA.repair_stages(stages, gaps, VOCAB)
        LA.drop_empty_stages(stages, gaps, VOCAB)
        self.assertNotIn("RAG", LA.covered_skills(stages))
        self.assertNotIn("Python", LA.covered_skills(stages))

    def test_route_is_never_empty(self):
        stages = [dict(stage(1, ["RAG"], topic="A", weeks=4)),
                  dict(stage(2, ["RAG"], topic="B", weeks=4))]
        gaps = [gap("RAG", priority="低", status="已满足")]
        LA.repair_stages(stages, gaps, VOCAB)
        LA.drop_empty_stages(stages, gaps, VOCAB)
        self.assertTrue(stages)                            # 绝不删空

    def test_node_removes_empty_stage_from_final_plan(self):
        gaps = [gap("Agent"), gap("LLM API"), gap("RAG", priority="低", status="已满足")]
        specs = [_stage_spec("Agent", ["Agent"], weeks=12),
                 _stage_spec("LLM API", ["LLM API"], weeks=8),
                 _stage_spec("专项技能提升", ["RAG"], weeks=8, goal="掌握专项技能",
                             criteria="能独立完成 RAG 知识库建设")]
        stages, log = _run_learning(gaps, specs)
        self.assertIn("已删除空阶段", log)
        self.assertEqual(len(stages), 2)
        self.assertEqual([s["stage"] for s in stages], [1, 2])
        self.assertEqual(LA.total_weeks(stages), 20)
        for s in stages:
            self.assertTrue(s["covers"])

    def test_node_keeps_unmappable_empty_stage_and_warns(self):
        """covers 归一不了而变空的阶段不能删，validate 仍会告警。"""
        gaps = [gap(AGENT_LABEL), gap(DEPLOY_LABEL)]
        specs = [_stage_spec("A", [AGENT_LABEL]), _stage_spec("B", [DEPLOY_LABEL])]
        stages, log = _run_learning(gaps, specs)
        self.assertEqual(len(stages), 2)
        self.assertIn("无法归一", log)


# ══════════════════════════════════════════════════════════════════════
# 收尾 3：covers 有、但 goal / completion_criteria 都没提到
# ══════════════════════════════════════════════════════════════════════
REAL_GOAL = "掌握基础技能以支持后续中级技能的学习"
REAL_CRITERIA = "能独立开发一个提供 3 个接口的 AI 服务并写清 README"


class TestSkillMentionWarning(unittest.TestCase):
    def test_llm_api_only_in_covers_warns(self):
        """验收用例：真实案例 —— LLM API 只在 covers 里，两段文字都不涉及模型调用。"""
        s = dict(stage(1, ["LLM API"], goal=REAL_GOAL, criteria=REAL_CRITERIA))
        issues = LA.check_skill_mentions([s], VOCAB)
        self.assertEqual(len(issues), 1)
        self.assertIn("LLM API", issues[0])

    def test_backend_api_same_text_does_not_warn(self):
        """同一段文字对「后端 API 开发」是有效覆盖，不能误报。"""
        s = dict(stage(1, ["后端 API 开发"], goal=REAL_GOAL, criteria=REAL_CRITERIA))
        self.assertEqual(LA.check_skill_mentions([s], VOCAB), [])

    def test_no_warning_when_skill_named_in_goal(self):
        s = dict(stage(1, ["Agent"], goal="掌握 Agent 工作流编排", criteria="能完成任务"))
        self.assertEqual(LA.check_skill_mentions([s], VOCAB), [])

    def test_no_warning_when_skill_named_in_criteria_only(self):
        s = dict(stage(1, ["Agent"], goal="掌握核心技能", criteria="能实现一个 Agent 服务"))
        self.assertEqual(LA.check_skill_mentions([s], VOCAB), [])

    def test_alias_counts_as_mentioned(self):
        """Prompt Engineering 与知识库别名「提示词」同义。"""
        s = dict(stage(1, ["Prompt Engineering"], goal="掌握提示词设计",
                       criteria="能写出结构化提示词"))
        self.assertEqual(LA.check_skill_mentions([s], VOCAB), [])

    def test_skill_name_part_counts_as_mentioned(self):
        s = dict(stage(1, ["LangChain / LangGraph"], goal="掌握 LangGraph 编排",
                       criteria="能搭一个图工作流"))
        self.assertEqual(LA.check_skill_mentions([s], VOCAB), [])

    def test_empty_covers_produce_no_warning(self):
        s = dict(stage(1, [], goal=REAL_GOAL, criteria=REAL_CRITERIA))
        self.assertEqual(LA.check_skill_mentions([s], VOCAB), [])

    def test_blank_text_is_skipped(self):
        s = dict(stage(1, ["LLM API"], goal="", criteria=""))
        self.assertEqual(LA.check_skill_mentions([s], VOCAB), [])

    def test_warning_does_not_modify_stages(self):
        s = dict(stage(1, ["LLM API"], goal=REAL_GOAL, criteria=REAL_CRITERIA))
        snapshot = copy.deepcopy(s)
        LA.check_skill_mentions([s], VOCAB)
        self.assertEqual(s, snapshot)

    def test_warning_surfaces_through_validate_learning_plan(self):
        gaps = [gap("LLM API")]
        stages = [dict(stage(1, ["LLM API"], goal=REAL_GOAL, criteria=REAL_CRITERIA, weeks=4)),
                  dict(stage(2, ["Agent"], goal="掌握 Agent", criteria="能实现 Agent", weeks=4))]
        issues = LA.validate_learning_plan(stages, gaps, VOCAB, SKILL_BASE)
        self.assertTrue(any("LLM API" in i and "没有提到" in i for i in issues))

    def test_mention_terms_come_from_existing_structures(self):
        terms = LA.skill_mention_terms("LLM API", VOCAB)
        self.assertIn("llm api", terms)
        self.assertIn("api", terms)
        self.assertIn("大模型的调用", terms)         # 来自 SkillVocabulary 的别名表


# ══════════════════════════════════════════════════════════════════════
# 修复 1：跨阶段重复技能检查（只告警）
# ══════════════════════════════════════════════════════════════════════
class TestDuplicateCoversCheck(unittest.TestCase):
    def test_duplicate_across_stages_warns(self):
        stages = [stage(1, ["LLM API", "Prompt Engineering"]),
                  stage(2, ["Agent"]),
                  stage(3, ["LLM API", "Prompt Engineering"])]
        issues = LA.check_duplicate_covers(stages, VOCAB)
        self.assertEqual(len(issues), 2)
        self.assertTrue(any("LLM API" in i and "1, 3" in i for i in issues))
        self.assertTrue(any("Prompt Engineering" in i and "1, 3" in i for i in issues))

    def test_no_duplicate_no_warning(self):
        stages = [stage(1, ["LLM API"]), stage(2, ["Agent"])]
        self.assertEqual(LA.check_duplicate_covers(stages, VOCAB), [])

    def test_alias_wording_is_recognised_as_duplicate(self):
        """别名/不同写法归一到同一技能后仍要判为重复，不能漏检。"""
        stages = [stage(1, ["后端服务"]), stage(2, ["后端 API 开发"])]
        issues = LA.check_duplicate_covers(stages, VOCAB)
        self.assertEqual(len(issues), 1)
        self.assertIn("后端 API 开发", issues[0])

    def test_langchain_variants_are_recognised(self):
        stages = [stage(1, ["LangChain"]), stage(2, ["LangGraph"])]
        issues = LA.check_duplicate_covers(stages, VOCAB)
        self.assertEqual(len(issues), 1)
        self.assertIn("LangChain / LangGraph", issues[0])

    def test_three_stages_all_listed(self):
        stages = [stage(1, ["Agent"]), stage(2, ["Agent"]), stage(3, ["Agent"])]
        issues = LA.check_duplicate_covers(stages, VOCAB)
        self.assertEqual(len(issues), 1)
        self.assertIn("1, 2, 3", issues[0])

    def test_same_skill_twice_in_one_stage_is_not_cross_stage_duplicate(self):
        stages = [stage(1, ["Agent", "Agent"])]
        self.assertEqual(LA.check_duplicate_covers(stages, VOCAB), [])

    def test_empty_covers_are_ignored(self):
        stages = [stage(1, []), stage(2, ["Agent"])]
        self.assertEqual(LA.check_duplicate_covers(stages, VOCAB), [])

    def test_check_does_not_modify_stages(self):
        stages = [stage(1, ["LLM API"]), stage(3, ["LLM API"])]
        snapshot = copy.deepcopy(stages)
        LA.check_duplicate_covers(stages, VOCAB)
        self.assertEqual(stages, snapshot)

    def test_surfaces_through_validate_learning_plan(self):
        gaps = [gap("LLM API"), gap("Agent")]
        stages = [dict(stage(1, ["LLM API"], goal="掌握 LLM API", criteria="能调用模型", weeks=4)),
                  dict(stage(2, ["LLM API"], goal="深入 LLM API", criteria="能封装调用层", weeks=4)),
                  dict(stage(3, ["Agent"], goal="掌握 Agent", criteria="能实现 Agent", weeks=4))]
        route = ("stage", "topic", "goal", "covers", "prerequisite",
                 "completion_criteria", "estimated_weeks")
        snapshot = [{k: s.get(k) for k in route} for s in stages]
        issues = LA.validate_learning_plan(stages, gaps, VOCAB, SKILL_BASE)
        self.assertTrue(any("重复覆盖" in i and "LLM API" in i for i in issues))
        # 只告警：阶段数、技能覆盖、目标与完成标准都不变（新增的只是追踪字段）
        self.assertEqual([{k: s.get(k) for k in route} for s in stages], snapshot)


# ══════════════════════════════════════════════════════════════════════
# 修复 2：项目排序（难度升序 → 高优先级缺口覆盖数 → 原顺序）
# ══════════════════════════════════════════════════════════════════════
def proj(name, covers_gaps, goal="g", tech=None):
    return {"name": name, "goal": goal, "covers_gaps": list(covers_gaps),
            "tech_stack": tech or ["Python"], "stage": "阶段1", "deliverable": "d"}


class TestProjectOrdering(unittest.TestCase):
    def setUp(self):
        self.base = ProjectKnowledgeBase()

    def names(self, items):
        return [p["name"] for p in items]

    def test_sorted_by_difficulty(self):
        projects = [proj("AI Agent 服务", ["Agent"]),          # intermediate
                    proj("AI 智能问答助手", ["LLM API"])]      # beginner
        ordered, notes = PA.order_projects_by_difficulty(projects, [], self.base)
        self.assertEqual(self.names(ordered), ["AI 智能问答助手", "AI Agent 服务"])
        self.assertTrue(notes)

    def test_same_difficulty_prefers_more_high_priority_coverage(self):
        """两个 beginner 项目：覆盖 2 个高优先级缺口的排在覆盖 1 个的前面。"""
        gaps = [gap("LLM API"), gap("Prompt Engineering"), gap("Agent")]
        low = proj("AI 智能问答助手", ["LLM API"])                       # 覆盖 1 个高
        high = proj("RAG 知识库问答系统", ["LLM API", "Prompt Engineering"])  # 覆盖 2 个高
        ordered, _ = PA.order_projects_by_difficulty([low, high], gaps, self.base)
        self.assertEqual(self.names(ordered), ["RAG 知识库问答系统", "AI 智能问答助手"])

    def test_low_priority_coverage_does_not_count(self):
        """只覆盖低优先级缺口的项目不因此排前。"""
        gaps = [gap("LLM API", priority="高"), gap("Python", priority="低")]
        a = proj("AI 智能问答助手", ["Python"])            # 只覆盖低优先级
        b = proj("RAG 知识库问答系统", ["LLM API"])        # 覆盖 1 个高优先级
        ordered, _ = PA.order_projects_by_difficulty([a, b], gaps, self.base)
        self.assertEqual(self.names(ordered), ["RAG 知识库问答系统", "AI 智能问答助手"])

    def test_full_tie_keeps_original_relative_order(self):
        gaps = [gap("LLM API")]
        a = proj("RAG 知识库问答系统", ["LLM API"])
        b = proj("AI 智能问答助手", ["LLM API"])
        ordered, notes = PA.order_projects_by_difficulty([a, b], gaps, self.base)
        self.assertEqual(self.names(ordered), ["RAG 知识库问答系统", "AI 智能问答助手"])
        self.assertEqual(notes, [])          # 顺序没变 → 不该报"重排过"

    def test_unknown_project_gets_no_difficulty_and_goes_last(self):
        projects = [proj("我自己编的项目", ["Agent"]),          # 匹配不到模板
                    proj("AI Agent 服务", ["Agent"])]         # intermediate
        ordered, notes = PA.order_projects_by_difficulty(projects, [gap("Agent")], self.base)
        self.assertEqual(self.names(ordered), ["AI Agent 服务", "我自己编的项目"])
        self.assertIsNone(PA._template_difficulty_rank("我自己编的项目", self.base))
        self.assertTrue(any("未赋予难度" in n for n in notes))

    def test_unknown_projects_keep_their_relative_order(self):
        projects = [proj("自创 B", []), proj("自创 A", []), proj("AI 智能问答助手", [])]
        ordered, _ = PA.order_projects_by_difficulty(projects, [], self.base)
        self.assertEqual(self.names(ordered), ["AI 智能问答助手", "自创 B", "自创 A"])

    def test_no_reorder_when_already_ordered(self):
        projects = [proj("AI 智能问答助手", []), proj("AI Agent 服务", [])]
        ordered, notes = PA.order_projects_by_difficulty(projects, [], self.base)
        self.assertEqual(notes, [])
        self.assertEqual(self.names(ordered), self.names(projects))

    def test_no_base_leaves_order_untouched(self):
        projects = [proj("AI Agent 服务", []), proj("AI 智能问答助手", [])]
        ordered, notes = PA.order_projects_by_difficulty(projects, [], None)
        self.assertEqual(self.names(ordered), self.names(projects))
        self.assertEqual(notes, [])

    def test_project_fields_are_untouched(self):
        projects = [proj("AI Agent 服务", ["Agent"], goal="补 Agent", tech=["FastAPI"]),
                    proj("AI 智能问答助手", ["LLM API"], goal="补 LLM API", tech=["Python"])]
        snapshot = copy.deepcopy(projects)
        ordered, _ = PA.order_projects_by_difficulty(projects, [gap("Agent")], self.base)
        self.assertEqual(sorted(snapshot, key=lambda p: p["name"]),
                         sorted(ordered, key=lambda p: p["name"]))
        for before in snapshot:
            after = next(p for p in ordered if p["name"] == before["name"])
            self.assertEqual(after, before)          # 内容一字未改

    def test_empty_input(self):
        self.assertEqual(PA.order_projects_by_difficulty([], [], self.base), ([], []))

    def test_difficulty_order_check_passes_after_ordering(self):
        """重排之后，原有的难度递进校验不应再报警。"""
        projects = [proj("AI Agent 服务", ["Agent"]), proj("AI 智能问答助手", ["LLM API"])]
        ordered, _ = PA.order_projects_by_difficulty(projects, [gap("Agent")], self.base)
        self.assertEqual(PA.check_difficulty_order(ordered, self.base), [])


# ══════════════════════════════════════════════════════════════════════
# 修复 3：训练技能（covers_skills）与技能缺口（covers_gaps）分离
# ══════════════════════════════════════════════════════════════════════
class TestTrainingSkillsVsGaps(unittest.TestCase):
    def setUp(self):
        self.base = ProjectKnowledgeBase()

    def test_unmet_gaps_excludes_satisfied_only(self):
        gaps = [gap("Agent", status="明显不足"), gap("RAG", status="已满足"),
                gap("后端 API 开发", status="部分满足")]
        self.assertEqual([g["skill"] for g in PA.unmet_gaps(gaps)],
                         ["Agent", "后端 API 开发"])

    def test_unmet_gaps_tolerates_missing_status(self):
        self.assertEqual([g["skill"] for g in PA.unmet_gaps([{"skill": "Agent"}])], ["Agent"])

    def test_satisfied_gap_is_not_counted_in_coverage(self):
        """已满足的技能不得充进项目覆盖度。"""
        lv = {"Python": "中级"}
        satisfied = PA.normalize_gap_skills(["LLM API", "RAG", "Python"], VOCAB)[0]
        unsolved = PA.normalize_gap_skills(
            [g["skill"] for g in PA.unmet_gaps(
                [gap("LLM API"), gap("RAG", status="已满足"), gap("Python", status="已满足")])],
            VOCAB)[0]
        before = {p["name"]: p["coverage"]
                  for p in self.base.recommend_for_user(satisfied, lv)[0]}
        after = {p["name"]: p["coverage"]
                 for p in self.base.recommend_for_user(unsolved, lv)[0]}
        self.assertGreater(before["RAG 知识库问答系统"], after["RAG 知识库问答系统"])
        rag_sys = next(p for p in self.base.recommend_for_user(unsolved, lv)[0]
                       if p["name"] == "RAG 知识库问答系统")
        self.assertNotIn("RAG", rag_sys["matched_skills"])
        self.assertNotIn("Python", rag_sys["matched_skills"])

    def test_rag_project_still_trains_rag(self):
        """RAG 项目照样训练 RAG —— 训练技能不受用户缺口影响。"""
        tpl = self.base.get("RAG 知识库问答系统")
        self.assertIn("RAG", tpl["covers_skills"])
        self.assertIn("Embedding", tpl["covers_skills"])
        # 用户已满足 RAG，但模板的训练技能一个都没被改
        self.assertEqual(tpl["covers_skills"], ["Python", "LLM API", "Embedding", "RAG"])

    def test_covers_skills_are_never_used_as_gap_names(self):
        """covers_skills 与 covers_gaps 是两套东西：前者是模板数据，后者是用户缺口。"""
        tpl = self.base.get("AI Agent 服务")
        proj_item = proj("AI Agent 服务", ["Agent"])          # 用户缺口只有 Agent
        self.assertEqual(proj_item["covers_gaps"], ["Agent"])
        self.assertNotEqual(sorted(tpl["covers_skills"]), sorted(proj_item["covers_gaps"]))

    def test_satisfied_gap_name_is_dropped_from_covers_gaps(self):
        """模型把已满足的技能写进 covers_gaps 时，按"不是缺口"剔除。"""
        gaps = [gap("LLM API"), gap("RAG", status="已满足", priority="低")]
        projects = [proj("RAG 知识库问答系统", ["LLM API", "RAG"])]
        notes = PA.drop_unlinked_covers_gaps(projects, PA.unmet_gaps(gaps), VOCAB)
        self.assertEqual(projects[0]["covers_gaps"], ["LLM API"])
        self.assertTrue(any("RAG" in n for n in notes))

    def test_node_excludes_satisfied_gap_end_to_end(self):
        import contextlib
        import io

        gaps = [gap("LLM API"), gap("Agent"), gap("RAG", status="已满足", priority="低")]

        def fake(prompt, schema):
            return PA.ProjectPlan(projects=[
                PA.ProjectItem(name="RAG 知识库问答系统", goal="补缺口",
                               covers_gaps=["LLM API", "RAG"],
                               tech_stack=["Python", "Embedding"], stage="阶段1",
                               deliverable="d")])

        buf = io.StringIO()
        with mock.patch.object(PA, "structured_invoke", fake), contextlib.redirect_stdout(buf):
            out = PA.project_node({"target_job": "AI应用开发工程师", "skill_gaps": gaps,
                                   "learning_plan": [],
                                   "user_skills": [{"skill": "Python", "level": "中级"}]})
        item = out["project_plan"][0]
        self.assertEqual(item["covers_gaps"], ["LLM API"])          # 已满足的 RAG 被排除
        self.assertIn("Embedding", item["tech_stack"])              # 训练技能不受影响

    def test_node_prompt_tells_model_which_gaps_are_satisfied(self):
        import contextlib
        import io

        gaps = [gap("LLM API"), gap("RAG", status="已满足", priority="低")]
        captured = {}

        def fake(prompt, schema):
            captured["prompt"] = prompt
            return PA.ProjectPlan(projects=[
                PA.ProjectItem(name="P", goal="g", covers_gaps=["LLM API"],
                               tech_stack=["Python"], stage="s", deliverable="d")])

        with mock.patch.object(PA, "structured_invoke", fake), contextlib.redirect_stdout(io.StringIO()):
            PA.project_node({"target_job": "AI应用开发工程师", "skill_gaps": gaps,
                             "learning_plan": [],
                             "user_skills": [{"skill": "Python", "level": "中级"}]})
        self.assertIn("状态 已满足", captured["prompt"])             # 模型能分辨已满足
        self.assertIn("已满足", captured["prompt"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
