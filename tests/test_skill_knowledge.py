"""agents/skill_knowledge.py 的单元测试（标准库 unittest）。

隔离策略：用临时目录写入受控的 YAML 构造 SkillKnowledgeBase，不触碰真实知识库。
"""

import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import yaml  # noqa: E402

from agents import skill_knowledge as SK  # noqa: E402


def _write(tmp: Path, filename: str, data) -> None:
    (tmp / filename).write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")


def _skill(name, prerequisites=None, guides=None):
    return {
        "skill": name,
        "category": "core",
        "level_guides": guides or {
            "入门": f"{name} 入门标准",
            "初级": f"{name} 初级标准",
            "中级": f"{name} 中级标准",
            "高级": f"{name} 高级标准",
        },
        "prerequisites": prerequisites or [],
        "typical_usage": f"{name} 的典型用途",
        "common_pitfalls": [],
        "learning_resources": [],
        "estimated_weeks": 2,
    }


def _make_base(tmp: Path, skills: list):
    _write(tmp, "core.yaml", skills)
    return SK.SkillKnowledgeBase(directory=tmp)


class TestSkillKnowledge(unittest.TestCase):
    def test_get_exact(self):
        with tempfile.TemporaryDirectory() as d:
            base = _make_base(Path(d), [_skill("Python"), _skill("RAG", prerequisites=["Python"])])
        item = base.get("Python")
        self.assertIsNotNone(item)
        self.assertEqual(item["skill"], "Python")
        self.assertIn("入门", item["level_guides"])

    def test_get_unknown_returns_none(self):
        with tempfile.TemporaryDirectory() as d:
            base = _make_base(Path(d), [_skill("Python")])
        self.assertIsNone(base.get("不存在的技能"))
        self.assertFalse(base.has("不存在的技能"))

    def test_get_many_found_and_missing(self):
        with tempfile.TemporaryDirectory() as d:
            base = _make_base(Path(d), [_skill("Python"), _skill("RAG", prerequisites=["Python"])])
        found, missing = base.get_many(["Python", "RAG", "SVG"])
        self.assertEqual([f["skill"] for f in found], ["Python", "RAG"])
        self.assertEqual(missing, ["SVG"])

    def test_duplicate_skill_name_raises(self):
        with tempfile.TemporaryDirectory() as d:
            _write(Path(d), "a.yaml", [_skill("Python")])
            _write(Path(d), "b.yaml", [_skill("Python")])
            with self.assertRaises(SK.SkillKnowledgeError):
                SK.SkillKnowledgeBase(directory=Path(d))

    def test_missing_level_raises(self):
        bad = _skill("Python")
        bad["level_guides"] = {"入门": "x", "初级": "y", "中级": "z", "缺失": "?"}
        with tempfile.TemporaryDirectory() as d:
            _write(Path(d), "core.yaml", [bad])
            with self.assertRaises(SK.SkillKnowledgeError):
                SK.SkillKnowledgeBase(directory=Path(d))

    def test_invalid_yaml_raises(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "bad.yaml").write_text("skill: [unclosed", encoding="utf-8")
            with self.assertRaises(SK.SkillKnowledgeError):
                SK.SkillKnowledgeBase(directory=Path(d))

    def test_missing_directory_raises(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(SK.SkillKnowledgeError):
                SK.SkillKnowledgeBase(directory=Path(d) / "nope")

    def test_format_for_prompt_includes_levels(self):
        with tempfile.TemporaryDirectory() as d:
            base = _make_base(Path(d), [_skill("Python")])
        text = base.format_for_prompt(["Python"])
        self.assertIn("Python", text)
        self.assertIn("入门", text)
        self.assertIn("高级", text)

    def test_format_for_prompt_ignores_unknown(self):
        with tempfile.TemporaryDirectory() as d:
            base = _make_base(Path(d), [_skill("Python")])
        self.assertEqual(base.format_for_prompt(["SVG"]), "")

    def test_format_for_prompt_includes_learning_resources(self):
        skill = _skill("Python")
        skill["learning_resources"] = [
            {"title": "Python 官方教程", "url": "https://docs.python.org/zh-cn/3/tutorial/"},
            {"title": "缺链接的条目"},
        ]
        with tempfile.TemporaryDirectory() as d:
            base = _make_base(Path(d), [skill])
        text = base.format_for_prompt(["Python"])
        self.assertIn("学习资源", text)
        self.assertIn("Python 官方教程", text)
        self.assertIn("https://docs.python.org/zh-cn/3/tutorial/", text)
        self.assertNotIn("缺链接的条目", text)


if __name__ == "__main__":
    unittest.main()