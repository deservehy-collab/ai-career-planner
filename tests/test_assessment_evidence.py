"""证据门槛：等级达标不等于"已满足"。

这一组测试锁住三件事：

1. 只有**同一技能 + 项目/成长记录**才算「已满足」；
2. 跨技能语义支撑最多算「疑似具备」，自述最多算「未验证」；
3. 这两类降级必须同步影响优先级、jd_match 统计与成长历史中的证据标签，
   否则界面说"没验证"，学习路线却把它当已满足跳过。

运行：
    .venv/Scripts/python.exe -m pytest tests/test_assessment_evidence.py -q
"""

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agents import assessment_agent as AA  # noqa: E402
from agents import growth_rules  # noqa: E402
from agents import profile_agent as PA  # noqa: E402
from agents.growth_store import GrowthStore  # noqa: E402
from agents.jd_match import compute_jd_match  # noqa: E402
from agents.user_jd import USER_JD_PROVENANCE, USER_JD_SOURCE_ID  # noqa: E402


def skill(name, level="中级", kind="self_report", evidence="", ref=""):
    item = {"skill": name, "level": level, "evidence": evidence, "evidence_kind": kind}
    if ref:
        item["evidence_ref"] = ref
    return item


def requirement(name, level="中级", importance=5):
    return {"skill": name, "level": level, "importance": importance}


def answer(name, supported_by, gap="缺这项能力"):
    return SimpleNamespace(skill=name, supported_by=supported_by, gap=gap)


def judge(user_skills, requirements, answers):
    return AA.judge_gaps(
        user_skills, requirements, SimpleNamespace(gaps=answers)
    )


def make_jd_evidence(note="要求熟练使用 Python"):
    return [{
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
    }]


class AssessmentEvidenceTests(unittest.TestCase):
    def test_self_report_exact_match_is_unverified(self):
        gaps = judge(
            [skill("Python", "中级", "self_report", "自述会用 Python")],
            [requirement("Python")],
            [answer("Python", "Python")],
        )
        gap = gaps[0]
        self.assertEqual(gap["status"], "已满足")            # 等级确实够
        self.assertEqual(gap["display_status"], "未验证")   # 但证据不够
        self.assertEqual(gap["verification"], AA.VERIFICATION_UNVERIFIED)
        self.assertEqual(gap["priority"], "中")             # 不能按已满足给低优先级
        self.assertEqual(gap["evidence_kind"], "self_report")
        self.assertIn("自述", gap["gap"])

    def test_project_evidence_exact_match_is_direct(self):
        gaps = judge(
            [skill("Python", "中级", "project", "在数据清洗项目中用 Python 处理了 10 万行数据")],
            [requirement("Python")],
            [answer("Python", "Python")],
        )
        gap = gaps[0]
        self.assertEqual(gap["display_status"], "已满足")
        self.assertEqual(gap["verification"], AA.VERIFICATION_DIRECT)
        self.assertEqual(gap["priority"], "低")
        self.assertEqual(gap["evidence_kind"], "project")
        self.assertIn("数据清洗", gap["evidence_ref"])

    def test_growth_record_is_direct_and_keeps_title(self):
        gaps = judge(
            [skill("Python", "中级", "growth", "完成了检索服务",
                   ref="完成 RAG 知识库课程项目")],
            [requirement("Python")],
            [answer("Python", "Python")],
        )
        gap = gaps[0]
        self.assertEqual(gap["display_status"], "已满足")
        self.assertEqual(gap["verification"], AA.VERIFICATION_DIRECT)
        self.assertEqual(gap["evidence_ref"], "完成 RAG 知识库课程项目")

    def test_cross_skill_support_is_suspected_not_satisfied(self):
        """教师场景的假阳性：教育心理学不能证明会做教学设计。"""
        requirements = [
            requirement("教学设计"), requirement("命题与作业设计"),
            requirement("学情分析"), requirement("课堂管理"),
        ]
        gaps = judge(
            [skill("教育心理学", "高级", "self_report", "修过教育心理学课程")],
            requirements,
            [answer(item["skill"], "教育心理学") for item in requirements],
        )
        self.assertEqual(len(gaps), 4)
        for gap in gaps:
            self.assertEqual(gap["status"], "已满足")          # 等级比较仍是够的
            self.assertEqual(gap["display_status"], "疑似具备")  # 但不算已满足
            self.assertEqual(gap["verification"], AA.VERIFICATION_INFERRED)
            self.assertEqual(gap["priority"], "中")
            self.assertIn("不是同一技能", gap["gap"])

    def test_cross_skill_with_project_evidence_is_still_suspected(self):
        """对方技能证据再硬，也只能证明"可能会迁移"，不能证明本项会。"""
        gaps = judge(
            [skill("教育心理学", "高级", "project", "在课程设计项目中应用了学习动机理论")],
            [requirement("教学设计")],
            [answer("教学设计", "教育心理学")],
        )
        gap = gaps[0]
        self.assertEqual(gap["display_status"], "疑似具备")
        self.assertEqual(gap["verification"], AA.VERIFICATION_INFERRED)

    def test_absent_skill_stays_insufficient(self):
        gaps = judge(
            [skill("Python", "高级", "project", "在多个项目中用 Python")],
            [requirement("RAG")],
            [answer("RAG", "无")],
        )
        gap = gaps[0]
        self.assertEqual(gap["status"], "明显不足")
        self.assertEqual(gap["display_status"], "明显不足")
        self.assertEqual(gap["priority"], "高")
        self.assertEqual(gap["evidence_kind"], "none")

    def test_unverified_is_counted_as_shortfall_in_jd_match(self):
        gap = {
            "skill": "Python", "status": "已满足", "display_status": "疑似具备",
            "verification": "inferred", "supported_by": "Java", "current_level": "中级",
            "required_level": "中级", "importance": 5, "priority": "中", "gap": "",
        }
        match = compute_jd_match(
            [gap],
            [{"skill": "Python", "evidence_refs": ["E01"],
              "evidence_status": "supported", "importance": 5}],
            make_jd_evidence(),
        )
        self.assertEqual(match["requirement_satisfied"], 0)
        self.assertEqual(match["requirement_shortfall"], 1)
        self.assertEqual(match["match_rate"], 0)
        self.assertEqual(match["overall"], "partial")

    def test_direct_satisfied_still_counts_as_satisfied_in_jd_match(self):
        gap = {
            "skill": "Python", "status": "已满足", "display_status": "已满足",
            "verification": "direct", "supported_by": "Python", "current_level": "中级",
            "required_level": "中级", "importance": 5, "priority": "低", "gap": "",
        }
        match = compute_jd_match(
            [gap],
            [{"skill": "Python", "evidence_refs": ["E01"],
              "evidence_status": "supported", "importance": 5}],
            make_jd_evidence(),
        )
        self.assertEqual(match["requirement_satisfied"], 1)
        self.assertEqual(match["requirement_shortfall"], 0)
        self.assertEqual(match["match_rate"], 1)
        self.assertEqual(match["overall"], "satisfied")


class GrowthEvidencePropagationTests(unittest.TestCase):
    def test_apply_upgrades_marks_growth_and_keeps_original_untouched(self):
        before = [skill("Python", "初级", "project", "课程项目里用过")]
        after = growth_rules.apply_upgrades(before, [{
            "skill": "Python", "from": "初级", "to": "中级",
            "source": "完成 RAG 知识库课程项目", "evidence": "交付了可运行的系统",
        }])
        self.assertEqual(before[0]["evidence_kind"], "project")  # 入参未被改动
        self.assertEqual(after[0]["level"], "中级")
        self.assertEqual(after[0]["evidence_kind"], "growth")
        self.assertEqual(after[0]["evidence_ref"], "完成 RAG 知识库课程项目")

    def test_apply_upgrades_adds_missing_skill_as_growth(self):
        after = growth_rules.apply_upgrades([], [{
            "skill": "RAG", "to": "初级", "source": "完成 RAG 课程项目",
            "evidence": "实现了检索问答",
        }])
        self.assertEqual(after[0]["evidence_kind"], "growth")
        self.assertEqual(after[0]["evidence_ref"], "完成 RAG 课程项目")
        self.assertEqual(after[0]["evidence"], "实现了检索问答")

    def test_merge_with_profile_keeps_project_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = GrowthStore(directory=Path(tmp))
            data = {
                "history": [],
                "baseline": {"user_skills": [
                    skill("Python", "初级", "project", "课程项目里用过"),
                ]},
            }
            merged = store.merge_with_profile(
                [skill("Python", "初级", "self_report", "自述会用")], data=data
            )
            self.assertEqual(merged.user_skills[0]["evidence_kind"], "project")

    def test_latest_skills_backfills_old_history(self):
        """旧记录没有 evidence_kind：升级过的技能补 growth，其余补 self_report。"""
        with tempfile.TemporaryDirectory() as tmp:
            store = GrowthStore(directory=Path(tmp))
            data = {
                "baseline": {"user_skills": []},
                "history": [{
                    "user_skills_after": [
                        {"skill": "Python", "level": "中级", "evidence": ""},
                        {"skill": "Excel", "level": "初级", "evidence": ""},
                    ],
                    "level_changes": [{
                        "skill": "Python", "from": "初级", "to": "中级",
                        "source": "完成 RAG 课程项目",
                    }],
                }],
            }
            skills = {item["skill"]: item for item in store.latest_skills(data)}
            self.assertEqual(skills["Python"]["evidence_kind"], "growth")
            self.assertEqual(skills["Python"]["evidence_ref"], "完成 RAG 课程项目")
            self.assertEqual(skills["Excel"]["evidence_kind"], "self_report")


class ProfileEvidenceKindTests(unittest.TestCase):
    def _profile(self, projects, evidence_kind="project"):
        def fake(prompt, schema):
            return schema(
                skills=[{
                    "skill": "Python", "level": "中级",
                    "evidence": "在课程项目里用过", "evidence_kind": evidence_kind,
                }],
                strengths=["动手能力强"],
                weaknesses=["缺少大型项目经验"],
            )

        with mock.patch.object(PA, "structured_invoke", fake):
            return PA.profile_node({
                "user_profile": {
                    "major": "计算机科学与技术", "grade": "大三",
                    "skills": ["Python"], "projects": projects,
                }
            })

    def test_project_evidence_requires_actual_projects(self):
        out = self._profile([])
        self.assertEqual(out["user_skills"][0]["evidence_kind"], "self_report")

    def test_project_evidence_is_kept_when_projects_exist(self):
        out = self._profile(["用 Python 完成了课程数据分析项目"])
        self.assertEqual(out["user_skills"][0]["evidence_kind"], "project")


class RequirementSourceTagTests(unittest.TestCase):
    """缺口条目上的 requirement_source：只透传来源，不参与任何判定。"""

    def test_tag_added_when_evidence_passed(self):
        gaps = AA.judge_gaps(
            [skill("Python", "中级", "project", "课程项目")],
            [{"skill": "Python", "level": "中级", "importance": 5, "evidence_refs": ["E01"]}],
            SimpleNamespace(gaps=[answer("Python", "Python")]),
            make_jd_evidence(),
        )
        self.assertEqual(gaps[0]["requirement_source"], "user_jd")
        self.assertEqual(gaps[0]["status"], "已满足")

    def test_no_tag_when_evidence_list_absent(self):
        """成长前后对比那条路径拿不到证据表 —— 宁可不写这个键，也不写一个错的来源。"""
        gaps = AA.judge_gaps(
            [skill("Python", "中级", "project", "课程项目")],
            [{"skill": "Python", "level": "中级", "importance": 5, "evidence_refs": ["E01"]}],
            SimpleNamespace(gaps=[answer("Python", "Python")]),
        )
        self.assertNotIn("requirement_source", gaps[0])

    def test_empty_evidence_list_is_a_real_answer_not_absence(self):
        """空表=确实一句原文都没有，这时逐条判定为模型推断是正确结论。"""
        gaps = AA.judge_gaps(
            [skill("Python", "中级", "project", "课程项目")],
            [{"skill": "Python", "level": "中级", "importance": 5, "evidence_refs": ["E01"]}],
            SimpleNamespace(gaps=[answer("Python", "Python")]),
            [],
        )
        self.assertEqual(gaps[0]["requirement_source"], "model_inference")

    def test_zero_baseline_gaps_get_the_same_tag(self):
        gaps = AA._zero_baseline_gaps(
            [{"skill": "Python", "level": "中级", "importance": 5, "evidence_refs": ["E01"]}],
            make_jd_evidence(),
        )
        self.assertEqual(gaps[0]["requirement_source"], "user_jd")


if __name__ == "__main__":
    unittest.main(verbosity=2)
