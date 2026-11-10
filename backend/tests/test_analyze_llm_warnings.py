"""分析结果必须带上"云端降级到本地"的提示（P1）。

只有把提示随结果返回，前端才能告诉用户"这次实际用的是本机模型"；
失败时还要清空捕获，避免串到下一次运行。
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agents import llm  # noqa: E402
from backend import schemas  # noqa: E402
import backend.analyze as analyze_mod  # noqa: E402


def _payload() -> schemas.AnalyzeRequest:
    return schemas.AnalyzeRequest(
        user_profile=schemas.UserProfileRequest(
            major="会计学", grade="大三", skills=["Excel"], projects=[],
        ),
        target_job="会计",
    )


class AnalyzeLlmWarningsTest(unittest.TestCase):
    def test_warning_is_attached_to_projected_result(self):
        def fake_invoke(_state):
            llm._warn_once("云端模型被限流（429），本次已改用本地模型 qwen2.5:3b。")
            return {"user_skills": [], "growth_diff": {}}

        with mock.patch.object(analyze_mod.workflow, "invoke", side_effect=fake_invoke):
            result = analyze_mod.run_workflow(_payload())

        self.assertEqual(len(result["llm_warnings"]), 1)
        self.assertIn("本地模型", result["llm_warnings"][0])

    def test_next_run_does_not_reuse_previous_warnings(self):
        def warn_invoke(_state):
            llm._warn_once("上一次运行的降级提示")
            return {"user_skills": [], "growth_diff": {}}

        def clean_invoke(_state):
            return {"user_skills": [], "growth_diff": {}}

        with mock.patch.object(analyze_mod.workflow, "invoke", side_effect=warn_invoke):
            analyze_mod.run_workflow(_payload())

        with mock.patch.object(analyze_mod.workflow, "invoke", side_effect=clean_invoke):
            result = analyze_mod.run_workflow(_payload())

        self.assertEqual(result["llm_warnings"], [])

    def test_failure_clears_capture(self):
        def boom(_state):
            raise RuntimeError("model exploded")

        with mock.patch.object(analyze_mod.workflow, "invoke", side_effect=boom):
            with self.assertRaises(RuntimeError):
                analyze_mod.run_workflow(_payload())

        self.assertEqual(llm.take_warnings(), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
