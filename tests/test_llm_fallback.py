"""云端通道失败降级（P1）的单元测试。

覆盖用户能感知的四种情况：
1. 免费档 429 重试耗尽 → 自动改用本地，并记一条提示；
2. 降级冷却期内不再反复打云端（否则每个节点都要多等 7 秒）；
3. key 无效（401）→ 自动改用本地，且后续调用不再试云端；
4. 云端 + 本地都失败 → 错误信息里给出 `ollama serve` / 拉模型的命令。

隔离：不连接任何网络，`_invoke_cloud` / `_invoke_local` / `time.sleep` 全部打桩。
"""

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import agents.llm as llm_mod  # noqa: E402

_CLOUD_ENV_KEYS = ("CLOUD_API_KEY", "CLOUD_BASE_URL", "CLOUD_MODEL")

_RATE_LIMIT = "Error code: 429 - rate limit reached"
_AUTH_FAILURE = "Error code: 401 - invalid api key"


class DummySchema(llm_mod.BaseModel):
    value: str


def _sentinel(text="local-result"):
    return DummySchema(value=text)


class CloudPromptContractTest(unittest.TestCase):
    def test_prompt_requires_json_and_includes_schema(self):
        prompt = llm_mod._cloud_prompt("基础提示", DummySchema)
        self.assertTrue(prompt.startswith("基础提示"))
        self.assertIn("只输出一个 JSON 对象", prompt)
        self.assertIn('"value"', prompt)
        self.assertIn("不要复述上面的 schema", prompt)

    def test_schema_failure_keeps_original_prompt(self):
        class BrokenSchema:
            @staticmethod
            def model_json_schema():
                raise RuntimeError("无法生成 schema")

        self.assertEqual(llm_mod._cloud_prompt("基础提示", BrokenSchema), "基础提示")


class LlmFallbackTest(unittest.TestCase):
    def setUp(self):
        self._saved_env = {key: os.environ.get(key) for key in _CLOUD_ENV_KEYS}
        os.environ["CLOUD_API_KEY"] = "test-key"
        llm_mod.set_channel("cloud")
        llm_mod.begin_warning_capture()
        llm_mod.take_warnings()

    def tearDown(self):
        llm_mod.set_channel("local")
        llm_mod.take_warnings()
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_rate_limit_exhausted_falls_back_to_local_with_warning(self):
        sentinel = _sentinel()
        with mock.patch.object(
            llm_mod, "_invoke_cloud", side_effect=RuntimeError(_RATE_LIMIT)
        ) as cloud_mock, \
             mock.patch.object(llm_mod, "_invoke_local", return_value=sentinel) as local_mock, \
             mock.patch.object(llm_mod.time, "sleep") as sleep_mock:
            llm_mod.begin_warning_capture()
            result = llm_mod.structured_invoke("prompt", DummySchema)
            warnings = llm_mod.take_warnings()

        self.assertIs(result, sentinel)
        self.assertEqual(cloud_mock.call_count, 3, "首次 + 两次延迟重试")
        self.assertEqual(
            [call.args[0] for call in sleep_mock.call_args_list],
            list(llm_mod.CLOUD_RETRY_DELAYS),
        )
        local_mock.assert_called_once()
        self.assertTrue(llm_mod.cloud_degraded())
        self.assertEqual(len(warnings), 1)
        self.assertIn("429", warnings[0])
        self.assertIn("本地模型", warnings[0])

    def test_cooldown_skips_cloud_and_reuses_local(self):
        with mock.patch.object(llm_mod, "_invoke_cloud", side_effect=RuntimeError(_RATE_LIMIT)), \
             mock.patch.object(llm_mod, "_invoke_local", return_value=_sentinel()), \
             mock.patch.object(llm_mod.time, "sleep"):
            llm_mod.begin_warning_capture()
            llm_mod.structured_invoke("prompt", DummySchema)
            llm_mod.take_warnings()

        self.assertTrue(llm_mod.cloud_degraded())
        llm_mod.begin_warning_capture()
        with mock.patch.object(llm_mod, "_invoke_cloud") as cloud_mock, \
             mock.patch.object(llm_mod, "_invoke_local", return_value=_sentinel()) as local_mock:
            result = llm_mod.structured_invoke("prompt", DummySchema)
            warnings = llm_mod.take_warnings()

        self.assertEqual(result.value, "local-result")
        cloud_mock.assert_not_called()
        local_mock.assert_called_once()
        self.assertEqual(len(warnings), 1)
        self.assertIn("继续使用本地模型", warnings[0])

    def test_auth_failure_falls_back_and_skips_cloud_afterwards(self):
        with mock.patch.object(llm_mod, "_invoke_cloud", side_effect=RuntimeError(_AUTH_FAILURE)) as cloud_mock, \
             mock.patch.object(llm_mod, "_invoke_local", return_value=_sentinel()) as local_mock:
            llm_mod.begin_warning_capture()
            first = llm_mod.structured_invoke("prompt", DummySchema)
            first_warnings = llm_mod.take_warnings()

            llm_mod.begin_warning_capture()
            second = llm_mod.structured_invoke("prompt", DummySchema)
            second_warnings = llm_mod.take_warnings()

        self.assertIs(first, second)
        self.assertEqual(cloud_mock.call_count, 1, "key 无效后不应再打云端")
        self.assertEqual(local_mock.call_count, 2)
        self.assertTrue(llm_mod.cloud_auth_failed())
        self.assertIn("401", first_warnings[0])
        self.assertIn("CLOUD_API_KEY", first_warnings[0])
        self.assertIn("继续使用本地模型", second_warnings[0])

    def test_local_failure_message_is_actionable(self):
        with mock.patch.object(llm_mod, "_invoke_cloud", side_effect=RuntimeError(_AUTH_FAILURE)), \
             mock.patch.object(
                 llm_mod, "_invoke_local",
                 side_effect=RuntimeError("Connection refused"),
             ):
            with self.assertRaises(RuntimeError) as ctx:
                llm_mod.structured_invoke("prompt", DummySchema)

        message = str(ctx.exception)
        self.assertIn("ollama serve", message)
        self.assertIn("ollama pull", message)
        self.assertIn("云端原因", message)
        self.assertIn("本地原因", message)

    def test_missing_key_falls_back_with_guidance(self):
        os.environ.pop("CLOUD_API_KEY", None)
        llm_mod.set_channel("cloud")
        with mock.patch.object(llm_mod, "_invoke_local", return_value=_sentinel()) as local_mock:
            llm_mod.begin_warning_capture()
            result = llm_mod.structured_invoke("prompt", DummySchema)
            warnings = llm_mod.take_warnings()

        self.assertEqual(result.value, "local-result")
        local_mock.assert_called_once()
        self.assertEqual(len(warnings), 1)
        self.assertIn("CLOUD_API_KEY", warnings[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
