"""pytest 全局夹具：把鉴权依赖换成固定测试用户。

认证（backend/auth.py）是晚于既有 API 测试加入的功能；这些测试从项目
``runs/`` 隔离与 store 覆盖的角度设计，不应该被迫每个用例都注册账号、
带 token。这里在**收集阶段**就覆盖 ``get_current_user``，让全部既有
API 测试保持原样通过。

鉴权本身的行为由 tests/test_auth_api.py 用**未覆盖**的真实依赖单独测。
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.app import app  # noqa: E402
from backend.auth import get_current_user  # noqa: E402

app.dependency_overrides[get_current_user] = lambda: {"id": 0, "username": "test-user"}
