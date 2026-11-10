"""目标结局：只有「全部要求 + 直接证据 + 可靠依据」才能宣布达成。

运行：
    .venv/Scripts/python.exe -m pytest tests/test_goal.py -q
"""

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agents.goal import (  # noqa: E402
    GOAL_ACHIEVED,
    GOAL_BASIS_INSUFFICIENT,
    GOAL_IN_PROGRESS,
    evaluate_goal,
    is_goal_closed,
)
from agents.job_agent import resolve_requirements_basis  # noqa: E402
from graph.workflow import _route_after_assessment, build_final_plan  # noqa: E402


def gap(skill, shown="已满足", verification="direct", priority="低", importance=4):
    item = {
        "skill": skill,
        "status": "已满足" if shown in ("已满足", "疑似具备", "未验证") else shown,
        "display_status": shown,
        "verification": verification,
        "priority": priority,
        "importance": importance,
        "supported_by": skill,
        "current_level": "中级",
        "required_level": "中级",
        "gap": "",
        "evidence_kind": "project",
        "evidence_ref": "项目复盘",
        "evidence_note": "做过项目",
    }
    return item


def state(gaps, basis="user_pasted", grounded=True, requirements=None):
    if requirements is None:
        requirements = [
            {
                "skill": item["skill"],
                "importance": item["importance"],
                "level": "中级",
                "evidence_status": "supported",
            }
            for item in gaps
        ]
    return {
        "target_job": "AI 应用开发工程师",
        "skill_gaps": gaps,
        "job_requirements": requirements,
        "job_evidence": [],
        "learning_plan": [],
        "project_plan": [],
        "job_requirements_basis": basis,
        "job_requirements_grounded": grounded,
    }


class GoalEvaluationTests(unittest.TestCase):
    def test_all_direct_evidence_with_solid_basis_is_achieved(self):
        goal = evaluate_goal(state([gap("Python"), gap("LangGraph")]))
        self.assertEqual(goal["status"], GOAL_ACHIEVED)
        self.assertEqual(goal["satisfied_count"], 2)
        self.assertEqual(goal["remaining_count"], 0)
        self.assertTrue(is_goal_closed(goal))
        self.assertIn("直接证据", goal["message"])

    def test_knowledge_base_profile_is_solid_basis(self):
        goal = evaluate_goal(
            state([gap("Python")], basis="knowledge_base", grounded=True)
        )
        self.assertEqual(goal["status"], GOAL_ACHIEVED)

    def test_level_satisfied_but_evidence_weak_is_not_achieved(self):
        goal = evaluate_goal(
            state([
                gap("Python"),
                gap("LangGraph", shown="未验证", verification="unverified"),
            ])
        )
        self.assertEqual(goal["status"], GOAL_IN_PROGRESS)
        self.assertEqual(goal["remaining"], ["LangGraph"])
        self.assertEqual(goal["needs_verification"], ["LangGraph"])
        self.assertIn("目标尚未达成", goal["message"])

    def test_cross_skill_inferred_is_not_achieved(self):
        goal = evaluate_goal(
            state([gap("LangGraph", shown="疑似具备", verification="inferred")])
        )
        self.assertEqual(goal["status"], GOAL_IN_PROGRESS)
        self.assertEqual(goal["needs_verification"], ["LangGraph"])

    def test_model_only_requirements_cannot_declare_achievement(self):
        goal = evaluate_goal(
            state([gap("Python")], basis="model_only", grounded=False)
        )
        self.assertEqual(goal["status"], GOAL_BASIS_INSUFFICIENT)
        self.assertFalse(goal["grounded"])
        self.assertIn("模型推断", goal["message"])
        self.assertTrue(is_goal_closed(goal))

    def test_user_jd_with_model_supplement_blocks_achievement(self):
        reqs = [
            {"skill": "Python", "importance": 4, "level": "中级",
             "evidence_status": "supported"},
            {"skill": "LangGraph", "importance": 4, "level": "中级",
             "evidence_status": "model_supplement"},
        ]
        goal = evaluate_goal(
            state(
                [gap("Python"), gap("LangGraph")],
                basis="user_pasted",
                grounded=False,
                requirements=reqs,
            )
        )
        self.assertEqual(goal["status"], GOAL_BASIS_INSUFFICIENT)
        self.assertEqual(goal["ungrounded_requirements"], ["LangGraph"])
        self.assertIn("模型补充", goal["message"])

    def test_model_only_with_remaining_gaps_is_in_progress(self):
        goal = evaluate_goal(
            state(
                [gap("Python"), gap("SQL", shown="明显不足",
                                      verification="unverified", priority="高")],
                basis="model_only",
                grounded=False,
            )
        )
        self.assertEqual(goal["status"], GOAL_IN_PROGRESS)
        self.assertEqual(goal["remaining_high"], ["SQL"])
        self.assertIn("缺少可靠依据", goal["message"])

    def test_empty_gaps_never_claims_achievement(self):
        goal = evaluate_goal(state([]))
        self.assertEqual(goal["status"], GOAL_IN_PROGRESS)
        self.assertIn("没有可判定", goal["message"])


class WorkflowClosureTests(unittest.TestCase):
    def test_route_skips_planning_only_when_goal_is_closed(self):
        achieved = state([gap("Python")])
        insufficient = state([gap("Python")], basis="model_only", grounded=False)
        in_progress = state([gap("Python", shown="未验证", verification="unverified")])
        self.assertEqual(_route_after_assessment(achieved), "done")
        self.assertEqual(_route_after_assessment(insufficient), "done")
        self.assertEqual(_route_after_assessment(in_progress), "learning")

    def test_final_plan_exposes_goal_and_closure_action(self):
        plan = build_final_plan(state([gap("Python"), gap("LangGraph")]))
        self.assertEqual(plan["goal_status"], GOAL_ACHIEVED)
        self.assertEqual(plan["goal"]["status"], GOAL_ACHIEVED)
        self.assertEqual(plan["already_satisfied"], ["Python", "LangGraph"])
        self.assertIn("目标已达成", plan["next_action"])
        self.assertEqual(plan["learning_stage_count"], 0)

    def test_final_plan_basis_insufficient_points_to_jd(self):
        plan = build_final_plan(
            state([gap("Python")], basis="model_only", grounded=False)
        )
        self.assertEqual(plan["goal_status"], GOAL_BASIS_INSUFFICIENT)
        self.assertIn("招聘要求", plan["next_action"])


class RequirementsBasisTests(unittest.TestCase):
    def test_user_pasted_without_supplements_is_grounded(self):
        basis, grounded = resolve_requirements_basis(
            True, None, [{"skill": "Python", "evidence_status": "supported"}]
        )
        self.assertEqual(basis, "user_pasted")
        self.assertTrue(grounded)

    def test_user_pasted_with_supplement_is_not_grounded(self):
        basis, grounded = resolve_requirements_basis(
            True, object(), [{"skill": "Python", "evidence_status": "model_supplement"}]
        )
        self.assertEqual(basis, "user_pasted")
        self.assertFalse(grounded)

    def test_builtin_profile_is_grounded(self):
        basis, grounded = resolve_requirements_basis(
            False, object(), [{"skill": "Python", "evidence_status": "model_supplement"}]
        )
        self.assertEqual(basis, "knowledge_base")
        self.assertTrue(grounded)

    def test_unknown_job_without_jd_is_model_only(self):
        basis, grounded = resolve_requirements_basis(
            False, None, [{"skill": "Python", "evidence_status": "model_supplement"}]
        )
        self.assertEqual(basis, "model_only")
        self.assertFalse(grounded)


if __name__ == "__main__":
    unittest.main()
