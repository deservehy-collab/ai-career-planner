"""认证接口（/api/auth/*）与业务接口鉴权行为的测试。

conftest.py 为了不让既有 API 测试被鉴权打挂，全局覆盖了 get_current_user；
本用例组在 setUp 里**临时摘掉**这份覆盖，走真实的 token 校验路径，
tearDown 再放回去，保证其它测试不受顺序影响。

隔离策略：数据库文件与 JWT 密钥都指到临时目录，全程不碰项目 runs/。
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fastapi.testclient import TestClient  # noqa: E402

import backend.auth as auth  # noqa: E402
from backend import db  # noqa: E402
from backend.app import app  # noqa: E402
from backend.auth import get_current_user  # noqa: E402


class PasswordHashTests(unittest.TestCase):
    def test_roundtrip(self):
        stored = auth.hash_password("s3cret-pw")
        self.assertTrue(stored.startswith("pbkdf2_sha256$"))
        self.assertTrue(auth.verify_password("s3cret-pw", stored))
        self.assertFalse(auth.verify_password("wrong-pw", stored))

    def test_salted(self):
        self.assertNotEqual(auth.hash_password("same"), auth.hash_password("same"))

    def test_tampered_hash_rejected(self):
        stored = auth.hash_password("good-pw")
        bad = stored[:-4] + ("aaaa" if not stored.endswith("aaaa") else "bbbb")
        self.assertFalse(auth.verify_password("good-pw", bad))


class AuthApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self._patches = [
            mock.patch("backend.config.AUTH_DB_PATH", root / "app.db"),
            mock.patch("backend.config.JWT_SECRET_PATH", root / "jwt.key"),
            mock.patch.object(auth, "_secret_cache", None),
        ]
        for p in self._patches:
            p.start()
        # 建表不再挂在请求路径上，测试内直接初始化（与 lifespan 启动做的事一致）
        db.init_db()
        # 摘掉 conftest 的全局覆盖，走真实鉴权
        self._saved_override = app.dependency_overrides.pop(get_current_user, None)
        self.client = TestClient(app)

    def tearDown(self):
        if self._saved_override is not None:
            app.dependency_overrides[get_current_user] = self._saved_override
        for p in self._patches:
            p.stop()
        try:
            self.tmp.cleanup()
        except PermissionError:
            # Windows 上 SQLite 连接可能拖到 teardown 之后才释放，清不掉就算了
            pass

    def _register(self, username="张三_01", password="pw123456"):
        return self.client.post("/api/auth/register",
                                json={"username": username, "password": password})

    def _bearer(self, token):
        return {"Authorization": f"Bearer {token}"}

    # ── 注册 / 登录 ─────────────────────────────────────────────────────
    def test_register_returns_token_and_user_without_hash(self):
        response = self._register()
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertTrue(body["token"])
        self.assertEqual(body["user"]["username"], "张三_01")
        self.assertNotIn("password_hash", body["user"])

    def test_duplicate_username_rejected(self):
        self.assertEqual(self._register().status_code, 201)
        self.assertEqual(self._register().status_code, 409)

    def test_weak_password_and_bad_username_rejected(self):
        self.assertEqual(self.client.post(
            "/api/auth/register", json={"username": "ok_user", "password": "123"}
        ).status_code, 422)
        self.assertEqual(self.client.post(
            "/api/auth/register", json={"username": "bad name!", "password": "pw123456"}
        ).status_code, 422)

    def test_login_success_and_wrong_password(self):
        self._register()
        ok = self.client.post("/api/auth/login",
                              json={"username": "张三_01", "password": "pw123456"})
        self.assertEqual(ok.status_code, 200)
        self.assertTrue(ok.json()["token"])
        bad = self.client.post("/api/auth/login",
                              json={"username": "张三_01", "password": "wrong-pw"})
        self.assertEqual(bad.status_code, 401)
        # 不区分"用户不存在 / 密码错误"，避免账号枚举
        missing = self.client.post("/api/auth/login",
                                   json={"username": "查无此人", "password": "whatever1"})
        self.assertEqual(missing.json()["detail"], bad.json()["detail"])

    # ── /me 与业务接口鉴权 ──────────────────────────────────────────────
    def test_me_requires_valid_token(self):
        self.assertEqual(self.client.get("/api/auth/me").status_code, 401)
        token = self._register().json()["token"]
        me = self.client.get("/api/auth/me", headers=self._bearer(token))
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.json()["username"], "张三_01")

    def test_business_endpoint_requires_token(self):
        self.assertEqual(self.client.get("/api/growth/history").status_code, 401)
        token = self._register().json()["token"]
        self.assertEqual(self.client.get("/api/growth/history",
                                         headers=self._bearer(token)).status_code, 200)

    def test_tampered_token_rejected(self):
        self.assertEqual(self.client.get(
            "/api/growth/history", headers=self._bearer("not.a.jwt")
        ).status_code, 401)

    def test_expired_token_rejected(self):
        from backend.db import User
        user = User(id=1, username="张三_01", password_hash="x")
        with mock.patch("backend.config.JWT_TTL_HOURS", -1):  # 负数 TTL = 签发即过期
            expired = auth.create_token(user)
        response = self.client.get("/api/auth/me", headers=self._bearer(expired))
        self.assertEqual(response.status_code, 401)
        self.assertIn("过期", response.json()["detail"])

    def test_logout_confirms(self):
        token = self._register().json()["token"]
        response = self.client.post("/api/auth/logout", headers=self._bearer(token))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])


if __name__ == "__main__":
    unittest.main()
