"""模型通道（local / cloud）API 与 agents.llm 双通道逻辑的测试。

覆盖：
- GET/PUT ``/api/status/llm-channel``：默认 local、非法值 422、
  未配 key 切云端 422、切换持久化到 settings.json、重启恢复；
- ``agents.llm``：非法通道 ValueError、云端未配置回落本地、
  key 鉴权失败仅这一次回落本地（structured_invoke）；
- ``backend.chat``：云端通道的 SSE 流（Authorization 头 / delta 解析 /
  401 时给错误事件并置鉴权失败标记）、云端未配 key 时 503。

隔离：settings.json 跟随 ``AUTH_DB_PATH`` 动态指向临时目录；
通道状态是进程级全局，tearDown 一律 ``set_channel("local")`` 并还原云端 env。
"""

import json
import os
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fastapi.testclient import TestClient  # noqa: E402

import agents.llm as llm_mod  # noqa: E402
from backend import chat, services, settings  # noqa: E402
from backend.app import app  # noqa: E402

_CLOUD_ENV_KEYS = ("CLOUD_API_KEY", "CLOUD_BASE_URL", "CLOUD_MODEL")


class LlmChannelApiTests(unittest.TestCase):
    def setUp(self):
        self._saved_env = {k: os.environ.get(k) for k in _CLOUD_ENV_KEYS}
        for key in _CLOUD_ENV_KEYS:
            os.environ.pop(key, None)
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self._patch = mock.patch("backend.config.AUTH_DB_PATH", root / "app.db")
        self._patch.start()
        llm_mod.set_channel("local")
        llm_mod._cloud = None  # 实例缓存跟着 env 走，测试间必须丢弃
        self.client = TestClient(app)

    def tearDown(self):
        llm_mod.set_channel("local")
        llm_mod._cloud = None
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self._patch.stop()
        try:
            self.tmp.cleanup()
        except PermissionError:
            pass

    def _get(self):
        return self.client.get("/api/status/llm-channel")

    # ── 默认与非法值 ─────────────────────────────────────────────────────
    def test_default_is_local_and_cloud_unconfigured(self):
        body = self._get().json()
        self.assertEqual(body["channel"], "local")
        self.assertFalse(body["cloud_configured"])
        self.assertEqual(body["active_model"], llm_mod.MODEL_NAME)
        # 云端卡片展示的是"切过去会用的模型"，不能跟着当前通道变成本地模型
        self.assertEqual(body["cloud_model"], llm_mod.CLOUD_MODEL_DEFAULT)
        self.assertFalse(body["cloud_auth_failed"])
        # 设置文件还没写过，不应存在
        self.assertFalse(settings.settings_path().exists())

    def test_health_channel_exposes_cloud_model_for_status_page(self):
        ollama = {
            "ok": True,
            "base_url": "http://localhost:11434",
            "models": [llm_mod.MODEL_NAME],
            "chat_model": llm_mod.MODEL_NAME,
            "chat_model_ready": True,
            "error": None,
        }
        with mock.patch.object(services, "_ollama_status", return_value=ollama):
            response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        # 状态页的“云端 API”卡片在本地通道下也要显示切过去会用的模型名
        self.assertEqual(
            response.json()["llm_channel"]["cloud_model"], llm_mod.CLOUD_MODEL_DEFAULT,
        )

    def test_invalid_channel_value_rejected_422(self):
        response = self.client.put("/api/status/llm-channel", json={"channel": "magic"})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self._get().json()["channel"], "local")

    def test_put_cloud_without_key_rejected_422(self):
        response = self.client.put("/api/status/llm-channel", json={"channel": "cloud"})
        self.assertEqual(response.status_code, 422)
        self.assertIn("CLOUD_API_KEY", response.json()["detail"])
        self.assertEqual(self._get().json()["channel"], "local")
        self.assertFalse(settings.settings_path().exists())

    # ── 切换与持久化 ─────────────────────────────────────────────────────
    def test_switch_to_cloud_persists_and_restores(self):
        os.environ["CLOUD_API_KEY"] = "test-key"
        response = self.client.put("/api/status/llm-channel", json={"channel": "cloud"})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["channel"], "cloud")
        self.assertTrue(body["cloud_configured"])
        self.assertEqual(body["active_model"], llm_mod.CLOUD_MODEL_DEFAULT)
        self.assertEqual(body["cloud_model"], llm_mod.CLOUD_MODEL_DEFAULT)

        # 落盘内容与 GET 一致
        saved = json.loads(settings.settings_path().read_text(encoding="utf-8"))
        self.assertEqual(saved["llm_channel"], "cloud")
        self.assertEqual(self._get().json()["channel"], "cloud")

        # 模拟重启：进程级状态归零后由设置恢复
        llm_mod.set_channel("local")
        llm_mod.set_channel(settings.load_channel())
        self.assertEqual(llm_mod.get_channel(), "cloud")

    def test_switch_back_to_local_persists(self):
        os.environ["CLOUD_API_KEY"] = "test-key"
        self.client.put("/api/status/llm-channel", json={"channel": "cloud"})
        response = self.client.put("/api/status/llm-channel", json={"channel": "local"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["channel"], "local")
        self.assertEqual(settings.load_channel(), "local")

    def test_put_failure_save_oserror_returns_500_with_hint(self):
        with mock.patch.object(settings, "save_channel", side_effect=OSError("disk full")):
            response = self.client.put("/api/status/llm-channel", json={"channel": "local"})
        self.assertEqual(response.status_code, 500)
        self.assertIn("settings.json", response.json()["detail"])

    def test_load_channel_survives_corrupt_file(self):
        settings.settings_path().write_text("not json at all", encoding="utf-8")
        self.assertEqual(settings.load_channel(), "local")

    # ── 聊天接口随通道走 ─────────────────────────────────────────────────
    def test_chat_stream_cloud_without_key_503(self):
        llm_mod.set_channel("cloud")
        response = self.client.post(
            "/api/chat/stream", json={"message": "你好", "history": []}
        )
        self.assertEqual(response.status_code, 503)
        self.assertIn("CLOUD_API_KEY", response.json()["detail"])


class LlmModuleUnitTests(unittest.TestCase):
    def setUp(self):
        self._saved_env = {k: os.environ.get(k) for k in _CLOUD_ENV_KEYS}
        for key in _CLOUD_ENV_KEYS:
            os.environ.pop(key, None)
        llm_mod.set_channel("local")
        llm_mod._cloud = None

    def tearDown(self):
        llm_mod.set_channel("local")
        llm_mod._cloud = None
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_set_channel_invalid_raises_value_error(self):
        with self.assertRaises(ValueError):
            llm_mod.set_channel("quantum")

    def test_cloud_settings_env_override(self):
        os.environ["CLOUD_BASE_URL"] = "https://example.com/v1/"
        os.environ["CLOUD_MODEL"] = "custom-model"
        settings_dict = llm_mod.cloud_settings()
        self.assertEqual(settings_dict["base_url"], "https://example.com/v1")
        self.assertEqual(settings_dict["model"], "custom-model")
        self.assertEqual(settings_dict["api_key"], "")

    def test_get_llm_falls_back_to_local_when_cloud_unconfigured(self):
        llm_mod.set_channel("cloud")
        instance = llm_mod.get_llm()
        self.assertIs(instance, llm_mod._local_instance())

    def test_structured_invoke_auth_failure_falls_back_to_local_once(self):
        os.environ["CLOUD_API_KEY"] = "bad-key"
        llm_mod.set_channel("cloud")

        class DummySchema(llm_mod.BaseModel):
            value: str

        sentinel = DummySchema(value="local-result")

        def boom(_prompt, _schema):
            raise RuntimeError("Error code: 401 - invalid api key")

        with mock.patch.object(llm_mod, "_invoke_cloud", side_effect=boom), \
             mock.patch.object(llm_mod, "_invoke_local", return_value=sentinel) as local_mock:
            result = llm_mod.structured_invoke("prompt", DummySchema)

        self.assertIs(result, sentinel)
        local_mock.assert_called_once()
        self.assertTrue(llm_mod.cloud_auth_failed())

    def test_structured_invoke_retries_rate_limited_cloud(self):
        os.environ["CLOUD_API_KEY"] = "good-key"
        llm_mod.set_channel("cloud")

        class DummySchema(llm_mod.BaseModel):
            value: str

        sentinel = DummySchema(value="cloud-result")
        attempts = [RuntimeError("Error code: 429 - rate limit reached"), sentinel]

        def fake(_prompt, _schema):
            item = attempts.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        with mock.patch.object(llm_mod, "_invoke_cloud", side_effect=fake), \
             mock.patch.object(llm_mod.time, "sleep") as sleep_mock:
            result = llm_mod.structured_invoke("prompt", DummySchema)

        self.assertIs(result, sentinel)
        sleep_mock.assert_called_once_with(llm_mod.CLOUD_RETRY_DELAYS[0])

    def test_structured_invoke_non_auth_cloud_error_propagates(self):
        os.environ["CLOUD_API_KEY"] = "good-key"
        llm_mod.set_channel("cloud")
        original = RuntimeError("upstream overloaded: 500")

        with mock.patch.object(llm_mod, "_invoke_cloud", side_effect=original), \
             mock.patch.object(llm_mod, "_invoke_local") as local_mock:
            with self.assertRaises(RuntimeError) as ctx:
                llm_mod.structured_invoke("prompt", type("S", (llm_mod.BaseModel,), {}))
        self.assertIs(ctx.exception, original)
        local_mock.assert_not_called()


class ChatCloudStreamTests(unittest.TestCase):
    """cloud 通道的 SSE 生成器：不发起真实网络请求，urlopen 全部打桩。"""

    def setUp(self):
        self._saved_env = {k: os.environ.get(k) for k in _CLOUD_ENV_KEYS}
        os.environ["CLOUD_API_KEY"] = "test-key"

    def tearDown(self):
        llm_mod.set_channel("local")
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    class _FakeResponse:
        def __init__(self, lines):
            self._lines = lines

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def __iter__(self):
            return iter(self._lines)

    def test_cloud_stream_parses_delta_and_sends_authorization(self):
        lines = [
            b'data: {"choices":[{"delta":{"content":"\xe4\xbd\xa0"}}]}\n\n',
            b'data: {"choices":[{"delta":{"content":"\xe5\xa5\xbd"}}]}\n\n',
            b"data: [DONE]\n\n",
        ]
        captured = {}

        def fake_urlopen(request, timeout=None):
            captured["url"] = request.full_url
            captured["auth"] = request.get_header("Authorization")
            captured["body"] = json.loads(request.data.decode("utf-8"))
            return self._FakeResponse(lines)

        with mock.patch.object(chat.urllib.request, "urlopen", side_effect=fake_urlopen):
            events = list(chat._stream_upstream_cloud([{"role": "user", "content": "你好"}]))

        texts = "".join(
            json.loads(event.split("data: ", 1)[1])["text"]
            for event in events
            if event.startswith("event: delta")
        )
        self.assertEqual(texts, "你好")
        self.assertTrue(events[0].startswith("event: start"))
        self.assertTrue(events[-1].startswith("event: done"))
        self.assertIn('"chars": 2', events[-1])

        self.assertEqual(captured["auth"], "Bearer test-key")
        self.assertEqual(captured["url"], f"{llm_mod.CLOUD_BASE_URL_DEFAULT}/chat/completions")
        self.assertTrue(captured["body"]["stream"])
        self.assertEqual(captured["body"]["model"], llm_mod.CLOUD_MODEL_DEFAULT)

    def test_cloud_stream_401_emits_error_event_and_marks_auth_failed(self):
        def fake_urlopen(request, timeout=None):
            raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, None)

        with mock.patch.object(chat.urllib.request, "urlopen", side_effect=fake_urlopen):
            events = list(chat._stream_upstream_cloud([{"role": "user", "content": "你好"}]))

        self.assertTrue(events[-1].startswith("event: error"))
        self.assertIn("CLOUD_API_KEY", events[-1])
        self.assertTrue(llm_mod.cloud_auth_failed())

    def test_cloud_stream_retries_429_then_succeeds(self):
        calls = {"n": 0}
        lines = [
            b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n',
            b"data: [DONE]\n\n",
        ]

        def fake_urlopen(request, timeout=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise urllib.error.HTTPError(
                    request.full_url, 429, "Too Many Requests", {}, None
                )
            return self._FakeResponse(lines)

        with mock.patch.object(chat.urllib.request, "urlopen", side_effect=fake_urlopen), \
             mock.patch.object(chat.time, "sleep") as sleep_mock:
            events = list(chat._stream_upstream_cloud([{"role": "user", "content": "hi"}]))

        self.assertEqual(calls["n"], 2)
        sleep_mock.assert_called_once_with(chat.CLOUD_RETRY_DELAYS[0])
        self.assertTrue(any(event.startswith("event: delta") for event in events))
        self.assertFalse(any(event.startswith("event: error") for event in events))

    def test_cloud_stream_429_exhausted_reports_cloud_message(self):
        calls = {"n": 0}

        def fake_urlopen(request, timeout=None):
            calls["n"] += 1
            raise urllib.error.HTTPError(
                request.full_url, 429, "Too Many Requests", {}, None
            )

        with mock.patch.object(chat.urllib.request, "urlopen", side_effect=fake_urlopen), \
             mock.patch.object(chat.time, "sleep"):
            events = list(chat._stream_upstream_cloud([{"role": "user", "content": "hi"}]))

        self.assertEqual(calls["n"], len(chat.CLOUD_RETRY_DELAYS) + 1)
        self.assertTrue(events[-1].startswith("event: error"))
        self.assertIn("云端", events[-1])
        # 云端失败不能再让用户去查 Ollama
        self.assertNotIn("Ollama", events[-1])


if __name__ == "__main__":
    unittest.main()
