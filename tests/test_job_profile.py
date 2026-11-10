"""岗位画像：结构、来源回查、缓存与只读边界的单元测试。

隔离策略
--------
- 全部使用 ``tempfile``，不碰项目 ``runs/``。
- 不调用模型：job_node 的模型调用用 mock 替换；画像生成失败路径也走 mock。
- ``run_workflow`` 的 workflow.invoke 用 mock 拦截，只检查注入/投影与是否写盘。
"""

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
from agents import job_profile as JP  # noqa: E402
from agents.growth_store import GrowthStore  # noqa: E402
from agents.loop_store import LoopStore  # noqa: E402
from agents.user_jd import USER_JD_PROVENANCE  # noqa: E402
from backend import analyze as analyze_mod  # noqa: E402
from backend import schemas, services  # noqa: E402
from backend.services import Stores  # noqa: E402


def _evidence():
    return [
        {
            "note": "负责按医嘱执行给药并观察患者反应",
            "provenance": USER_JD_PROVENANCE,
            "company_slug": "用户原文",
        },
        {
            "note": "熟悉电子病历系统与护理记录单",
            "provenance": "knowledge_base",
            "company_slug": "kb",
        },
    ]


def _raw_profile(**overrides):
    raw = {
        "summary": "负责住院患者的日常护理与记录，向医生和家属反馈病情。",
        "core_tasks": [
            {"name": "按医嘱执行给药并观察患者反应", "detail": "核对医嘱后执行", "evidence_refs": ["E01"]},
            {"name": "填写护理记录", "detail": "记录生命体征与护理过程", "evidence_refs": ["E99"]},
        ],
        "tools": [
            {"name": "电子病历系统", "detail": "录入和查询护理记录", "evidence_refs": ["E02"]},
        ],
        "deliverables": [
            {"name": "护理记录单", "detail": "每班次交接", "evidence_refs": ["E01"]},
        ],
        "entry_requirements": [
            {"name": "护士执业资格", "detail": "持证上岗", "evidence_refs": ["无"]},
        ],
        "career_path": [
            {"name": "独立值班", "detail": "能独立完成本班次护理", "evidence_refs": ["无"]},
        ],
    }
    raw.update(overrides)
    return raw


def _build(**overrides):
    profile, warnings = JP.build_profile(
        _raw_profile(**overrides), target_job="护士", evidence=_evidence()
    )
    return profile, warnings


def _analysis():
    return JA.JobAnalysis(requirements=[
        JA.JobRequirement(skill="临床护理", importance=5, level="中级", reason="执行护理任务"),
        JA.JobRequirement(skill="护理记录", importance=4, level="中级", reason="记录病情"),
        JA.JobRequirement(skill="沟通协作", importance=3, level="中级", reason="与家属沟通"),
    ])


class BuildProfileTest(unittest.TestCase):
    def test_invalid_ref_is_dropped_and_sources_are_derived_by_code(self):
        profile, warnings = _build()
        tasks = {item["name"]: item for item in profile["core_tasks"]}
        tools = {item["name"]: item for item in profile["tools"]}

        self.assertEqual(tasks["按医嘱执行给药并观察患者反应"]["source"], JP.SOURCE_JD)
        self.assertEqual(tasks["按医嘱执行给药并观察患者反应"]["evidence_refs"], ["E01"])
        self.assertTrue(tasks["按医嘱执行给药并观察患者反应"]["source_notes"])

        # 模型引用了本轮不存在的编号：只降级，不中断
        self.assertEqual(tasks["填写护理记录"]["source"], JP.SOURCE_MODEL)
        self.assertEqual(tasks["填写护理记录"]["evidence_refs"], [])
        self.assertTrue(any("E99" in item for item in warnings))

        # 非用户原文的证据归到知识库参考档
        self.assertEqual(tools["电子病历系统"]["source"], JP.SOURCE_STANDARD)
        self.assertEqual(tools["电子病历系统"]["evidence_refs"], ["E02"])

    def test_literal_user_jd_fallback_when_model_cites_nothing(self):
        raw = _raw_profile(core_tasks=[
            {"name": "按医嘱执行给药", "detail": "", "evidence_refs": ["无"]},
        ])
        profile, _ = JP.build_profile(raw, target_job="护士", evidence=_evidence())
        self.assertEqual(profile["core_tasks"][0]["source"], JP.SOURCE_JD)
        self.assertEqual(profile["core_tasks"][0]["evidence_refs"], ["E01"])

    def test_duplicate_names_are_merged(self):
        raw = _raw_profile(core_tasks=[
            {"name": "填写护理记录", "detail": "a", "evidence_refs": ["无"]},
            {"name": "填写护理记录 ", "detail": "b", "evidence_refs": ["无"]},
        ])
        profile, warnings = JP.build_profile(raw, target_job="护士", evidence=_evidence())
        self.assertEqual(len(profile["core_tasks"]), 1)
        self.assertTrue(any("重复" in item for item in warnings))

    def test_missing_core_tasks_raises(self):
        raw = _raw_profile(core_tasks=[])
        with self.assertRaises(JP.JobProfileError):
            JP.build_profile(raw, target_job="护士", evidence=_evidence())

    def test_plain_string_lists_are_accepted(self):
        """小模型常见的"列表里全是字符串"要能接住，而不是判成空画像。"""
        draft = JP.JobProfileDraft.model_validate({
            "summary": "护士的日常护理工作。",
            "core_tasks": ["监测并记录患者生命体征", "按医嘱执行给药"],
            "tools": ["电子病历系统"],
            "deliverables": ["护理记录单"],
            "entry_requirements": ["护士执业资格"],
            "career_path": ["独立值班"],
        })
        profile, _ = JP.build_profile(draft, target_job="护士", evidence=_evidence())
        self.assertEqual(len(profile["core_tasks"]), 2)
        self.assertEqual(profile["core_tasks"][0]["name"], "监测并记录患者生命体征")

    def test_no_evidence_marks_everything_as_model(self):
        profile, _ = JP.build_profile(_raw_profile(), target_job="护士", evidence=[])
        for section in ("core_tasks", "tools", "deliverables", "entry_requirements", "career_path"):
            for item in profile[section]:
                self.assertEqual(item["source"], JP.SOURCE_MODEL)
                self.assertEqual(item["evidence_refs"], [])

    def test_prompt_block_contains_tasks_and_source_labels(self):
        profile, _ = _build()
        block = JP.format_profile_block(profile)
        self.assertIn("【核心工作任务】", block)
        self.assertIn("按医嘱执行给药并观察患者反应", block)
        self.assertIn("招聘原文", block)
        self.assertIn("模型推断", block)
        self.assertIn("没有可用的岗位画像", JP.format_profile_block(None))

    def test_prompt_block_is_bounded(self):
        profile, _ = _build()
        block = JP.format_profile_block(profile, max_chars=80)
        self.assertLessEqual(len(block), 100)
        self.assertIn("已截断", block)


class JobProfileStoreTest(unittest.TestCase):
    def test_roundtrip_and_key_normalization(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JP.JobProfileStore(tmp)
            profile, _ = _build()
            store.save(profile, target_job=" 护士 ", jd_text="负责护理。\n", warnings=["w"])

            loaded = store.load("护士", "负责护理。")
            self.assertTrue(loaded.found)
            self.assertEqual(loaded.status, JP.STATUS_OK)
            self.assertEqual(loaded.profile["target_job"], "护士")
            self.assertEqual(loaded.warnings, ["w"])
            self.assertEqual(
                store.path_for("护士", "负责护理。"),
                store.path_for(" 护士 ", "负责护理。\r\n"),
                "岗位空白/原文换行不应造成缓存分裂",
            )

    def test_different_jd_is_a_different_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JP.JobProfileStore(tmp)
            profile, _ = _build()
            store.save(profile, target_job="护士", jd_text="原文 A")
            self.assertFalse(store.load("护士", "原文 B").found)

    def test_corrupt_cache_is_not_overwritten_or_deleted(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JP.JobProfileStore(tmp)
            profile, _ = _build()
            store.save(profile, target_job="护士", jd_text="原文 A")
            path = store.path_for("护士", "原文 A")
            path.write_text("{not json", encoding="utf-8")

            loaded = store.load("护士", "原文 A")
            self.assertFalse(loaded.found)
            self.assertEqual(loaded.status, JP.STATUS_CORRUPT)
            self.assertTrue(loaded.warning)
            self.assertEqual(path.read_text(encoding="utf-8"), "{not json")

    def test_schema_mismatch_is_ignored_without_rewrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JP.JobProfileStore(tmp)
            profile, _ = _build()
            store.save(profile, target_job="护士", jd_text="原文 A")
            path = store.path_for("护士", "原文 A")
            data = json.loads(path.read_text(encoding="utf-8"))
            data["schema_version"] = 999
            path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

            loaded = store.load("护士", "原文 A")
            self.assertFalse(loaded.found)
            self.assertEqual(loaded.status, JP.STATUS_SCHEMA_MISMATCH)
            self.assertIn("schema_version", loaded.warning)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["schema_version"], 999)


class JobNodeProfileTest(unittest.TestCase):
    def test_injected_profile_is_used_in_requirements_prompt(self):
        profile, _ = _build()
        captured = {}

        def fake_invoke(prompt, schema):
            captured["prompt"] = prompt
            return _analysis()

        state = {
            "target_job": "护士",
            "job_evidence": _evidence(),
            "retrieval_status": "user_pasted",
            "job_profile": profile,
            "enable_job_profile": True,
        }
        with mock.patch.object(JA, "structured_invoke", fake_invoke), \
             mock.patch.object(JP, "structured_invoke",
                               side_effect=AssertionError("有缓存时不应重新生成画像")):
            out = JA.job_node(state)

        self.assertEqual(out["job_profile_origin"], "cache")
        self.assertEqual(out["job_profile"]["core_tasks"][0]["name"], "按医嘱执行给药并观察患者反应")
        self.assertIn("【岗位画像", captured["prompt"])
        self.assertIn("按医嘱执行给药并观察患者反应", captured["prompt"])
        self.assertEqual(len(out["job_requirements"]), 3)

    def test_unknown_job_does_not_generate_profile(self):
        """陌生岗位没有知识库依据，不得让模型补画像冒充事实。"""
        generator = mock.Mock(side_effect=AssertionError("陌生岗位不应生成画像"))
        with mock.patch.object(JA, "generate_job_profile", generator), \
             mock.patch.object(JA, "structured_invoke", lambda prompt, schema: _analysis()):
            out = JA.job_node({
                "target_job": "宠物美容师",
                "job_evidence": _evidence(),
                "retrieval_status": "user_pasted",
                "enable_job_profile": True,
            })

        generator.assert_not_called()
        self.assertIsNone(out["job_profile"])
        self.assertEqual(out["job_profile_origin"], "unavailable")
        self.assertEqual(len(out["job_requirements"]), 3)

    def test_recognized_job_generation_failure_degrades(self):
        """知识库岗位的人工画像不可用时才生成；生成失败仍只降级。"""
        with mock.patch.object(JA, "builtin_profile", return_value=None), \
             mock.patch.object(JA, "generate_job_profile",
                               side_effect=JP.JobProfileError("模拟的画像失败")), \
             mock.patch.object(JA, "structured_invoke", lambda prompt, schema: _analysis()):
            out = JA.job_node({
                "target_job": "会计",
                "job_evidence": _evidence(),
                "retrieval_status": "user_pasted",
                "enable_job_profile": True,
            })

        self.assertIsNone(out["job_profile"])
        self.assertEqual(out["job_profile_origin"], "unavailable")
        self.assertTrue(any("生成失败" in item for item in out["job_profile_warnings"]))
        self.assertEqual(len(out["job_requirements"]), 3)

    def test_flag_off_does_not_generate_profile(self):
        generator = mock.Mock(side_effect=AssertionError("不应调用画像生成"))
        with mock.patch.object(JA, "generate_job_profile", generator), \
             mock.patch.object(JA, "structured_invoke", lambda prompt, schema: _analysis()):
            out = JA.job_node({
                "target_job": "宠物美容师",
                "job_evidence": _evidence(),
                "retrieval_status": "user_pasted",
                "enable_job_profile": False,
            })

        generator.assert_not_called()
        self.assertIsNone(out["job_profile"])
        self.assertEqual(out["job_profile_origin"], "unavailable")


class AnalyzeBoundaryWithProfileTest(unittest.TestCase):
    def _payload(self):
        return schemas.AnalyzeRequest(
            user_profile=schemas.UserProfileRequest(major="护理", grade="大三"),
            target_job="护士",
            jd_text="负责按医嘱执行给药并观察患者反应。",
        )

    def test_cached_profile_is_injected_without_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            user_dir = Path(tmp) / "user_1"
            growth = GrowthStore(directory=user_dir)
            store = JP.JobProfileStore(user_dir)
            profile, _ = _build()
            store.save(profile, target_job="护士", jd_text="负责按医嘱执行给药并观察患者反应。")
            cache_path = store.path_for("护士", "负责按医嘱执行给药并观察患者反应。")
            before = cache_path.read_bytes()

            captured = {}

            def fake_invoke(state):
                captured.update(state)
                return {"user_skills": [], "growth_diff": {}}

            with mock.patch.object(analyze_mod.workflow, "invoke", side_effect=fake_invoke):
                analyze_mod.run_workflow(self._payload(), baseline_store=growth)

            self.assertEqual(captured["job_profile"]["target_job"], "护士")
            self.assertTrue(captured["enable_job_profile"])
            self.assertEqual(cache_path.read_bytes(), before, "只读分析不得改写画像缓存")

    def test_missing_cache_is_not_written_by_readonly_analysis(self):
        with tempfile.TemporaryDirectory() as tmp:
            user_dir = Path(tmp) / "user_1"
            growth = GrowthStore(directory=user_dir)
            store = JP.JobProfileStore(user_dir)
            profile, _ = _build()

            def fake_invoke(state):
                return {
                    "user_skills": [],
                    "growth_diff": {},
                    "job_profile": profile,
                    "job_profile_origin": "generated",
                    "job_profile_warnings": [],
                }

            payload = self._payload()
            with mock.patch.object(analyze_mod.workflow, "invoke", side_effect=fake_invoke):
                analyze_mod.run_workflow(payload, baseline_store=growth)

            self.assertFalse(
                store.path_for("护士", "负责按医嘱执行给药并观察患者反应。").exists(),
                "run_workflow 只读，画像落盘只能发生在服务层保存路径",
            )

    def test_diagnostic_analysis_can_disable_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            growth = GrowthStore(directory=Path(tmp) / "user_1")
            captured = {}

            def fake_invoke(state):
                captured.update(state)
                return {"user_skills": [], "growth_diff": {}}

            with mock.patch.object(analyze_mod.workflow, "invoke", side_effect=fake_invoke):
                analyze_mod.run_workflow(
                    self._payload(), baseline_store=growth, enable_job_profile=False
                )

            self.assertFalse(captured["enable_job_profile"])


class SavePathPersistsProfileTest(unittest.TestCase):
    def _payload(self):
        return schemas.AnalyzeRequest(
            user_profile=schemas.UserProfileRequest(major="护理", grade="大三"),
            target_job="护士",
            jd_text="负责按医嘱执行给药并观察患者反应。",
        )

    def test_analyze_and_save_persists_generated_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            stores = Stores(
                growth=GrowthStore(directory=Path(tmp) / "用户"),
                loop=LoopStore(directory=Path(tmp) / "用户"),
            )
            profile, _ = _build()

            def fake_run_workflow(payload, baseline_store=None, **kwargs):
                return {
                    "learning_plan": [],
                    "project_plan": [],
                    "job_profile": profile,
                    "job_profile_origin": "generated",
                    "job_profile_warnings": [],
                }

            with mock.patch("backend.analyze.run_workflow", fake_run_workflow):
                services.run_analyze_and_save(stores, self._payload())

            loaded = JP.JobProfileStore(stores.growth.directory).load(
                "护士", "负责按医嘱执行给药并观察患者反应。"
            )
            self.assertTrue(loaded.found)
            self.assertEqual(loaded.profile["core_tasks"][0]["name"], "按医嘱执行给药并观察患者反应")

    def test_existing_round_branch_only_reads_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            stores = Stores(
                growth=GrowthStore(directory=Path(tmp) / "用户"),
                loop=LoopStore(directory=Path(tmp) / "用户"),
            )
            stores.loop.append_round(
                target_job="护士",
                learning_plan=[{"topic": "基础护理", "covers": ["临床护理"]}],
                project_plan=[],
                round_no=1,
            )
            profile, _ = _build()
            calls = []

            def fake_run_workflow(payload, baseline_store=None, **kwargs):
                calls.append(kwargs)
                return {
                    "learning_plan": [],
                    "project_plan": [],
                    "job_profile": profile,
                    "job_profile_origin": "generated",
                    "job_profile_warnings": [],
                }

            with mock.patch("backend.analyze.run_workflow", fake_run_workflow):
                services.run_analyze_and_save(stores, self._payload())

            self.assertEqual(calls[0].get("enable_job_profile"), False)
            self.assertFalse(
                JP.JobProfileStore(stores.growth.directory).path_for(
                    "护士", "负责按医嘱执行给药并观察患者反应。"
                ).exists(),
                "已有轮次的分支不生成也不保存画像",
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
