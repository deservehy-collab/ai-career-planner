"""内置资料摘要装载（agents/external_sources.py）的本地测试。

覆盖：
1. 只转格式：provenance / source_id / 原句逐字保留，不写 company_slug
2. 取舍规则：单技能上限、全局上限、按人工重要度排序、required 优先
3. 如实统计：kept + dropped == total，覆盖了多少技能
4. 脏数据：缺 source_id 或缺原句的 evidence 条目直接丢掉，画像缺失返回空表

全部读仓库内的 knowledge/jobs/*.yaml 与内存构造的画像，不调模型、不写文件。

运行：
    .venv/Scripts/python.exe -m unittest tests.test_external_sources -v
"""

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agents.external_sources import (  # noqa: E402
    MAX_EVIDENCE_PER_SKILL,
    MAX_OFFICIAL_EVIDENCE,
    OFFICIAL_PROVENANCE,
    build_official_evidence,
)
from agents.job_catalog import JobCatalog  # noqa: E402


def make_entry(source_id="source_x_001", note="公开标准整理：掌握这项能力的常见要求", **extra):
    return {"source_id": source_id, "note": note, **extra}


def make_profile(skills, *, job_id="test_job", job_name="测试岗位"):
    return {"job_id": job_id, "job_name": job_name, "skills": skills}


def make_skill(name, importance=3, evidence=None):
    return {
        "skill": name,
        "importance": importance,
        "level": "中级",
        "evidence": evidence if evidence is not None else [make_entry()],
    }


class ShapeTests(unittest.TestCase):
    """转出来的条目必须与用户原文同构，否则 job_agent 的编号回查会塌。"""

    def test_accountant_profile_yields_one_record_per_evidence_entry(self):
        catalog = JobCatalog()
        profile = catalog.resolve_profile("会计")
        records, stats = build_official_evidence(profile)

        self.assertTrue(records)
        self.assertEqual(len(records), stats["entries_kept"])
        self.assertEqual(stats["skills_with_evidence"], stats["skills_total"])
        for record in records:
            self.assertEqual(record["provenance"], OFFICIAL_PROVENANCE)
            self.assertEqual(record["job_id"], "accountant")
            self.assertTrue(str(record["source_id"]).startswith("source_"))
            self.assertTrue(str(record["note"]).strip())
            self.assertTrue(str(record["skill"]).strip())
            # 整理稿写不出具体公司：留空比猜一个诚实
            self.assertIsNone(record["company_slug"])

    def test_original_wording_is_kept_verbatim(self):
        catalog = JobCatalog()
        records, _ = build_official_evidence(catalog.resolve_profile("会计"))
        notes = {record["note"] for record in records}
        self.assertTrue(any("企业会计准则" in note for note in notes))

    def test_user_jd_record_keys_are_matched(self):
        """字段集合与 chunk_user_jd 一致，下游才不会因为少一个键而走偏。"""
        from agents.user_jd import chunk_user_jd

        jd_records, _ = chunk_user_jd("精通 Python；熟悉 Docker 容器化部署。")
        records, _ = build_official_evidence(
            make_profile([make_skill("Python", evidence=[make_entry(note="公开标准整理：掌握 Python 基础语法")])])
        )
        self.assertEqual(set(records[0]), set(jd_records[0]))


class SelectionTests(unittest.TestCase):
    """取舍规则：谁进提示词、谁被丢弃，都必须能解释。"""

    def test_prefers_required_over_bonus(self):
        profile = make_profile([
            make_skill("Python", evidence=[
                make_entry(source_id="source_a", requirement_type="bonus", note="加分项：有开源贡献记录者优先考虑"),
                make_entry(source_id="source_b", requirement_type="required", note="任职要求：熟练使用 Python 编写服务"),
            ])
        ])
        records, _ = build_official_evidence(profile)
        self.assertEqual([r["source_id"] for r in records], ["source_b", "source_a"])

    def test_caps_entries_per_skill(self):
        profile = make_profile([
            make_skill("Python", evidence=[
                make_entry(source_id=f"source_{i}", note=f"任职要求：第{i}项关于 Python 的具体要求说明")
                for i in range(5)
            ])
        ])
        records, stats = build_official_evidence(profile)
        self.assertEqual(len(records), MAX_EVIDENCE_PER_SKILL)
        self.assertEqual(stats["entries_dropped"], 5 - MAX_EVIDENCE_PER_SKILL)

    def test_global_cap_drops_the_least_important_skills(self):
        """超出全局上限时先砍重要度低的技能，而不是 YAML 末尾那个。"""
        skills = [
            make_skill(f"技能{i}", importance=5 if i == 0 else 1, evidence=[
                make_entry(source_id=f"src_{i}_{j}", note=f"任职要求：技能{i} 的第 {j} 条具体说明")
                for j in range(2)
            ])
            for i in range(20)
        ]
        records, stats = build_official_evidence(make_profile(skills))

        self.assertEqual(len(records), MAX_OFFICIAL_EVIDENCE)
        self.assertEqual(stats["entries_kept"] + stats["entries_dropped"], stats["entries_total"])
        # 唯一的重要度 5 技能必须完整保留
        self.assertEqual(len([r for r in records if r["skill"] == "技能0"]), 2)

    def test_skills_without_evidence_are_counted_but_not_invented(self):
        profile = make_profile([
            make_skill("有资料的技能", evidence=[make_entry(note="任职要求：这条有出处可以回查")]),
            make_skill("没资料的技能", evidence=[]),
        ])
        records, stats = build_official_evidence(profile)
        self.assertEqual(stats["skills_total"], 2)
        self.assertEqual(stats["skills_with_evidence"], 1)
        self.assertEqual([r["skill"] for r in records], ["有资料的技能"])


class DirtyDataTests(unittest.TestCase):
    def test_none_and_empty_profiles_yield_no_records(self):
        for profile in (None, {}, {"skills": []}, make_profile([{"skill": "Python", "importance": 5}])):
            records, stats = build_official_evidence(profile)
            self.assertEqual(records, [], profile)
            self.assertEqual(stats["entries_kept"], 0)

    def test_entries_missing_source_id_or_note_are_dropped(self):
        profile = make_profile([
            make_skill("Python", evidence=[
                {"note": "任职要求：没有出处编号的条目不能算有依据"},
                {"source_id": "source_a"},
                make_entry(source_id="source_ok", note="任职要求：唯一一条完整可用的资料条目"),
            ])
        ])
        records, stats = build_official_evidence(profile)
        self.assertEqual([r["source_id"] for r in records], ["source_ok"])
        self.assertEqual(stats["skills_with_evidence"], 1)
        self.assertEqual(stats["entries_total"], 1)

    def test_importance_is_clamped_and_level_passed_through(self):
        profile = make_profile([
            {"skill": "Python", "importance": 9, "level": "中级", "evidence": [
                make_entry(source_id="source_a", note="任职要求：熟练使用 Python 编写后端服务")]},
            {"skill": "RAG", "importance": "x", "level": "", "evidence": [
                make_entry(source_id="source_b", note="任职要求：理解 RAG 检索增强生成的链路")]},
        ])
        records, _ = build_official_evidence(profile)
        by_skill = {r["skill"]: r for r in records}
        self.assertEqual(by_skill["Python"]["importance"], 5)
        self.assertEqual(by_skill["Python"]["level"], "中级")
        self.assertIsNone(by_skill["RAG"]["importance"])
        self.assertIsNone(by_skill["RAG"]["level"])


if __name__ == "__main__":
    unittest.main()
