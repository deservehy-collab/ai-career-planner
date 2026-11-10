"""``GET /api/health`` 的鉴权口径测试（P0-②）。

要证明的是**路由真的挂了鉴权依赖**，而不是"测试全绿"：
``tests/conftest.py`` 在收集阶段全局覆盖了 ``get_current_user``，只要跑全量，
所有接口看起来都是已登录的 —— 那证明不了未登录会被拒绝。所以本文件
**先撤掉那个覆盖**再断 401，跑完再恢复，单独跑或跟全量一起跑都成立。

零副作用：
- 401 分支在进处理函数之前就被拦下，**不会**触发 Ollama 探测；
- 200 分支把 ``services.health`` 换成固定返回值，同样不连任何服务；
- store 依赖注入临时目录，不读项目 ``runs/``。
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from agents.growth_store import GrowthStore  # noqa: E402
from agents.loop_store import LoopStore  # noqa: E402
from backend import services  # noqa: E402
from backend.app import app  # noqa: E402
from backend.auth import get_current_user  # noqa: E402
from backend.deps import create_stores  # noqa: E402
from backend.services import Stores  # noqa: E402

NO_TOKEN_DETAIL = "未登录：请先登录后再访问该功能"


class _RealAuthCase(unittest.TestCase):
    """撤掉 conftest 的全局鉴权覆盖，用**真实**依赖跑请求。"""

    def setUp(self):
        self.client = TestClient(app)
        self._saved_override = app.dependency_overrides.pop(get_current_user, None)
        self.addCleanup(self._restore_auth)

        directory = Path(tempfile.mkdtemp())
        stores = Stores(growth=GrowthStore(directory=directory),
                        loop=LoopStore(directory=directory))
        app.dependency_overrides[create_stores] = lambda: stores
        self.addCleanup(app.dependency_overrides.pop, create_stores, None)

    def _restore_auth(self):
        if self._saved_override is not None:
            app.dependency_overrides[get_current_user] = self._saved_override
        else:
            app.dependency_overrides.pop(get_current_user, None)

    def _as_logged_in(self):
        app.dependency_overrides[get_current_user] = lambda: {"id": 1, "username": "u"}


class HealthAuthTest(_RealAuthCase):
    def test_health_without_token_is_401(self):
        response = self.client.get("/api/health")

        self.assertEqual(response.status_code, 401, "未登录访问 /api/health 必须被拒绝")
        self.assertEqual(response.json(), {"detail": NO_TOKEN_DETAIL})
        self.assertEqual(response.headers.get("www-authenticate"), "Bearer")

    def test_health_with_bogus_token_is_401(self):
        response = self.client.get(
            "/api/health", headers={"Authorization": "Bearer not-a-real-jwt"}
        )

        self.assertEqual(response.status_code, 401)
        self.assertIn("登录凭证无效", response.json()["detail"])

    def test_health_with_token_still_returns_the_report(self):
        self._as_logged_in()
        report = {"status": "ok", "ollama": {"reachable": True}}

        with mock.patch.object(services, "health", return_value=report) as probe:
            response = self.client.get("/api/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), report)
        probe.assert_called_once()  # 登录后照常拿到全量探测结果


class OtherEndpointsStillGuardedTest(_RealAuthCase):
    """同口径回归：撤掉覆盖后，业务接口确实也一直要求登录（不只是 health）。"""

    def test_tasks_list_without_token_is_401(self):
        self.assertEqual(self.client.get("/api/tasks").status_code, 401)

    def test_runs_lookup_without_token_is_401(self):
        self.assertEqual(self.client.get("/api/runs/whatever").status_code, 401)


if __name__ == "__main__":
    unittest.main(verbosity=2)
