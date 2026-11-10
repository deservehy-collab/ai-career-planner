"""Learning/Project Agent 知识注入集成测试（标准库 unittest）。

验证：
1. Learning Agent 确实把 Skill 知识传给了模型。
2. Project Agent 确实把 Project 模板传给了模型。
3. 知识库加载失败时回退到纯模型生成，不中断。
4. 注入知识后输出结构保持不变。

隔离策略：mock 消费方模块上的 `structured_invoke` 与知识库回退（`_load_*`），
不连接 Ollama；用临时目录指向真实知识库做只读检索。
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agents import learning_agent as LA  # noqa: E402
from agents import project_agent as PA  # noqa: E402

SKILL_KB_MARKER = "【岗位知识库·技能能力标准"
PROJECT_KB_MARKER = "【Project 知识库·候选项目模板"


def make_gaps():
    return [
        {"skill": "RAG", "status": "明显不足", "supported_by": "无",
         "current_level": "无", "required_level": "中级", "importance": 5,
         "priority": "高", "gap": "缺 RAG 检索-增强-生成实践"},
        {"skill": "后端 API 开发", "status": "部分满足", "supported_by": "Python",
         "current_level": "初级", "required_level": "中级", "importance": 4,
         "priority": "高", "gap": "缺后端服务开发"},
    ]


def make_learning_out():
    return LA.LearningPlan(stages=[
        LA.LearningStage(stage=1, topic="RAG 检索实践", goal="补 RAG 缺口",
                         covers=["RAG"], prerequisite="Python",
                         completion_criteria="能建一个 RAG 问答", estimated_weeks=3),
        LA.LearningStage(stage=2, topic="后端服务开发", goal="补后端缺口",
                         covers=["后端 API 开发"], prerequisite="Python",
                         completion_criteria="能写 3 个接口", estimated_weeks=2),
    ])


def make_project_out():
    return PA.ProjectPlan(projects=[
        PA.ProjectItem(name="RAG 问答系统", goal="补 RAG", covers_gaps=["RAG"],
                       tech_stack=["LangChain"], stage="阶段2", deliverable="可运行服务"),
    ])


class TestLearningKnowledgeInjection(unittest.TestCase):
    def test_skill_knowledge_injected_into_prompt(self):
        captured = {}

        def fake(prompt, schema):
            captured["prompt"] = prompt
            return make_learning_out()

        state = {"target_job": "AI应用开发工程师",
                 "skill_gaps": make_gaps(),
                 "user_skills": [{"skill": "Python", "level": "初级"}]}
        with mock.patch.object(LA, "structured_invoke", fake):
            out = LA.learning_node(state)
        self.assertIn(SKILL_KB_MARKER, captured["prompt"])
        # 系统真实知识库能查到 RAG
        self.assertIn("RAG", captured["prompt"])
        self.assertEqual([s["covers"] for s in out["learning_plan"]], [["RAG"], ["后端 API 开发"]])

    def test_output_structure_unchanged(self):
        def fake(prompt, schema):
            return make_learning_out()

        state = {"target_job": "AI应用开发工程师", "skill_gaps": make_gaps(),
                 "user_skills": [{"skill": "Python", "level": "初级"}]}
        with mock.patch.object(LA, "structured_invoke", fake):
            out = LA.learning_node(state)
        stage = out["learning_plan"][0]
        # 阶段号已重排为从 1 连续
        self.assertEqual([s["stage"] for s in out["learning_plan"]], [1, 2])
        self.assertIn("covers", stage)
        self.assertIsInstance(stage["estimated_weeks"], int)

    def test_kb_failure_falls_back(self):
        # 让知识库加载抛错，节点应回退不中断、结构仍合法
        captured = {}

        def fake(prompt, schema):
            captured["prompt"] = prompt
            return make_learning_out()

        state = {"target_job": "AI应用开发工程师", "skill_gaps": make_gaps(),
                 "user_skills": [{"skill": "Python", "level": "初级"}]}
        with mock.patch.object(LA, "structured_invoke", fake), \
             mock.patch("agents.learning_agent.SkillKnowledgeBase",
                        side_effect=Exception("模拟知识库故障")):
            out = LA.learning_node(state)
        # 注入了"不可用"占位文本，且没有真实找到"RAG 标准"块（回退成功）
        self.assertIn("知识库暂时不可用", captured["prompt"])
        self.assertEqual(len(out["learning_plan"]), 2)

    def test_missing_gaps_raises(self):
        with self.assertRaises(ValueError):
            LA.learning_node({"target_job": "x"})


class TestProjectKnowledgeInjection(unittest.TestCase):
    def test_project_templates_injected_into_prompt(self):
        captured = {}

        def fake(prompt, schema):
            captured["prompt"] = prompt
            return make_project_out()

        state = {"target_job": "AI应用开发工程师", "skill_gaps": make_gaps(),
                 "learning_plan": None}
        with mock.patch.object(PA, "structured_invoke", fake):
            out = PA.project_node(state)
        self.assertIn(PROJECT_KB_MARKER, captured["prompt"])
        # 差距技能 RAG 能命中知识库模板
        self.assertIn("RAG 知识库问答系统", captured["prompt"])
        self.assertEqual([p["name"] for p in out["project_plan"]], ["RAG 问答系统"])

    def test_kb_failure_falls_back(self):
        captured = {}

        def fake(prompt, schema):
            captured["prompt"] = prompt
            return make_project_out()

        state = {"target_job": "AI应用开发工程师", "skill_gaps": make_gaps(),
                 "learning_plan": None}
        with mock.patch.object(PA, "structured_invoke", fake), \
             mock.patch("agents.project_agent.ProjectKnowledgeBase",
                        side_effect=Exception("模拟知识库故障")):
            out = PA.project_node(state)
        self.assertIn("知识库暂时不可用", captured["prompt"])
        self.assertEqual(len(out["project_plan"]), 1)


if __name__ == "__main__":
    unittest.main()