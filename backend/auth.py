"""认证：注册 / 登录 / JWT 签发与校验 / get_current_user 依赖。

安全口径
--------
- 密码用 PBKDF2-HMAC-SHA256（24 万轮 + 随机盐）哈希后入库，**不存明文**；
  校验走 ``hmac.compare_digest``，避免按字节短路比较带来的计时侧信道。
- JWT 密钥优先取环境变量 ``JWT_SECRET``；没有就在首次使用时生成 32 字节随机
  密钥并持久化到 ``runs/jwt_secret.key`` —— 不硬编码任何默认密钥。
- 登录失败统一回"用户名或密码错误"，不区分"用户不存在"，避免账号枚举。
- 登录 / 注册带**进程内滑动窗口限速**（阈值见 backend/config.py 的 ``AUTH_*``
  与 ``REGISTER_*``）：登录按 IP+用户名统计**失败**次数（成功登录即清零，
  正常用户输错一两次不受影响），注册按 IP 统计**成功建号**数（422/409 不
  计数）。防在线爆破与批量灌库；多 worker 部署时各进程独立计数，与单 worker
  的部署口径一致。
- ``get_current_user`` 校验 token 签名后**按主键查库**确认用户仍存在：
  JWT 是无状态的，签发后 24 小时内一直有效，用户被删除后其余业务接口不能
  继续认它（此前只有 ``/api/auth/me`` 查库）。
- 注册不再自动认领旧 JSON 数据（见 ``scripts/migrate_legacy_plans.py``）：
  第一个注册的账号认领单用户时代留在数据根目录的 ``loop.json``/``growth.json``。
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import secrets
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend import config
from backend.db import User, get_session

__all__ = [
    "router", "get_current_user", "hash_password", "verify_password",
    "create_token", "decode_token", "Credentials",
]

logger = logging.getLogger("backend.auth")

# ── 密码哈希 ────────────────────────────────────────────────────────────
_PBKDF2_ITERATIONS = 240_000


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${_PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algorithm, iterations, salt_hex, digest_hex = stored.split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iterations)
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest.hex(), digest_hex)


# ── JWT ────────────────────────────────────────────────────────────────
_secret_cache: str | None = None


class _SlidingWindowLimiter:
    """进程内滑动窗口限速器（认证端点防爆破）。

    只防**单进程内**的暴力尝试 —— 与 ``services._STORE_WRITE_LOCK`` 同款边界：
    多 worker 部署时各进程各算各的；当前部署口径本就是单 worker（见
    ``backend/config.py`` 的 TASK_WORKERS）。键在窗口外自动过期、空键顺手清理，
    不会随时间无限增长；``reset()`` 供测试整体清空。
    """

    def __init__(self, limit: int, window_seconds: float) -> None:
        self._limit = limit
        self._window = window_seconds
        self._lock = threading.Lock()
        self._hits: dict[str, deque[float]] = {}

    def limited(self, key: str) -> bool:
        """窗口内命中次数已达上限（只读判断，**不**记录新命中）。"""
        now = time.monotonic()
        with self._lock:
            hits = self._hits.get(key)
            if hits is None:
                return False
            while hits and now - hits[0] > self._window:
                hits.popleft()
            if not hits:
                del self._hits[key]
                return False
            return len(hits) >= self._limit

    def record(self, key: str) -> None:
        """记一次命中。"""
        now = time.monotonic()
        with self._lock:
            hits = self._hits.setdefault(key, deque())
            hits.append(now)
            while hits and now - hits[0] > self._window:
                hits.popleft()

    def clear(self, key: str) -> None:
        """清掉这个键的计数（登录成功时用）。"""
        with self._lock:
            self._hits.pop(key, None)

    def reset(self) -> None:
        """清空全部计数（测试用）。"""
        with self._lock:
            self._hits.clear()


#: 登录限速按「IP + 用户名」：同一账号被集中试错才触发，不同用户互不影响。
_login_limiter = _SlidingWindowLimiter(config.AUTH_FAILURE_LIMIT, config.AUTH_FAILURE_WINDOW_SECONDS)
#: 注册限速按「IP」：防同一来源批量建号。
_register_limiter = _SlidingWindowLimiter(config.REGISTER_LIMIT, config.REGISTER_WINDOW_SECONDS)


def _jwt_secret() -> str:
    global _secret_cache
    if _secret_cache:
        return _secret_cache
    env_secret = os.environ.get("JWT_SECRET")
    if env_secret:
        _secret_cache = env_secret
        return _secret_cache
    path = Path(config.JWT_SECRET_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        _secret_cache = path.read_text(encoding="utf-8").strip()
    else:
        _secret_cache = secrets.token_hex(32)
        path.write_text(_secret_cache, encoding="utf-8")
    return _secret_cache


def create_token(user: User) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user.id),
        "username": user.username,
        "iat": now,
        "exp": now + timedelta(hours=config.JWT_TTL_HOURS),
    }
    return jwt.encode(payload, _jwt_secret(), algorithm=config.JWT_ALGORITHM)


def decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, _jwt_secret(), algorithms=[config.JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="登录已过期，请重新登录",
            headers={"WWW-Authenticate": "Bearer"},
        ) from None
    except jwt.InvalidTokenError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="登录凭证无效，请重新登录",
            headers={"WWW-Authenticate": "Bearer"},
        ) from None


# ── get_current_user 依赖 ──────────────────────────────────────────────
bearer_scheme = HTTPBearer(auto_error=False)

Credentials = HTTPAuthorizationCredentials


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    session: Session = Depends(get_session),
) -> dict:
    """校验请求头里的 Bearer token，并确认用户仍存在，返回 {id, username}。

    测试通过 ``app.dependency_overrides[get_current_user]`` 绕过真实鉴权
    （见 tests/conftest.py），所以路由里必须写 ``Depends(get_current_user)``。

    校验签名后还要**查库**：JWT 无状态、有效期内一直自洽，用户被删除后
    业务接口不能继续认它的 token。按主键取一行，SQLite 下开销可忽略；
    username 以库里的为准（JWT 载荷只是签发时的快照）。
    """
    if credentials is None or credentials.scheme.lower() != "bearer" or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="未登录：请先登录后再访问该功能",
            headers={"WWW-Authenticate": "Bearer"},
        )
    payload = decode_token(credentials.credentials)
    user = session.get(User, int(payload["sub"]))
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="该账号已不存在，请重新登录",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return {"id": user.id, "username": user.username}


# ── 请求模型与校验 ──────────────────────────────────────────────────────
_USERNAME_RE = re.compile(r"^[A-Za-z0-9_\u4e00-\u9fa5]+$")


def _validate_username(value: str) -> str:
    value = value.strip()
    if not (config.USERNAME_MIN_LENGTH <= len(value) <= config.USERNAME_MAX_LENGTH):
        raise ValueError(
            f"用户名长度需在 {config.USERNAME_MIN_LENGTH}~{config.USERNAME_MAX_LENGTH} 个字符之间"
        )
    if not _USERNAME_RE.match(value):
        raise ValueError("用户名只能包含中英文、数字与下划线")
    return value


class RegisterRequest(BaseModel):
    username: str = Field(min_length=1, description="用户名：中英文/数字/下划线")
    password: str = Field(min_length=1, description="密码：至少 6 位")

    @field_validator("username")
    @classmethod
    def check_username(cls, value: str) -> str:
        return _validate_username(value)

    @field_validator("password")
    @classmethod
    def check_password(cls, value: str) -> str:
        if len(value) < config.PASSWORD_MIN_LENGTH:
            raise ValueError(f"密码至少 {config.PASSWORD_MIN_LENGTH} 位")
        if len(value) > 128:
            raise ValueError("密码过长（最多 128 位）")
        return value


class LoginRequest(BaseModel):
    username: str = Field(min_length=1)
    password: str = Field(min_length=1)

    @field_validator("username")
    @classmethod
    def check_username(cls, value: str) -> str:
        return value.strip()


# ── 路由 ───────────────────────────────────────────────────────────────
router = APIRouter(prefix="/api/auth", tags=["auth"])


def _auth_response(user: User) -> dict:
    return {"token": create_token(user), "token_type": "Bearer", "user": user.public()}


def _client_ip(request: Request) -> str:
    """限速键的 IP 部分。``request.client`` 在某些测试/ASGI 环境下可能是 None。"""
    return request.client.host if request.client is not None else "unknown"


@router.post("/register", status_code=201, summary="注册")
def register(
    payload: RegisterRequest, request: Request, session: Session = Depends(get_session)
) -> dict:
    """注册新用户并**直接登录**（返回 token，注册后不用再输一次密码）。

    注册不再自动认领旧 JSON 数据：旧数据只通过迁移脚本显式导入。
    """
    ip = _client_ip(request)
    if _register_limiter.limited(ip):
        raise HTTPException(status_code=429, detail="注册过于频繁，请稍后再试")
    exists = session.scalar(select(User.id).where(User.username == payload.username))
    if exists:
        raise HTTPException(status_code=409, detail=f"用户名 {payload.username} 已被占用")
    user = User(username=payload.username, password_hash=hash_password(payload.password))
    session.add(user)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(status_code=409, detail=f"用户名 {payload.username} 已被占用") from None
    session.refresh(user)
    # 只数"成功建号"：422（校验失败）/ 409（重名）不消耗注册额度
    _register_limiter.record(ip)
    return _auth_response(user)


@router.post("/login", summary="登录")
def login(
    payload: LoginRequest, request: Request, session: Session = Depends(get_session)
) -> dict:
    """校验用户名密码并签发 JWT。

    失败限速按「IP + 用户名」统计：窗口内失败达到 ``AUTH_FAILURE_LIMIT`` 次
    返回 429；成功登录清零计数 —— 正常用户输错一两次完全不受影响。
    """
    key = f"{_client_ip(request)}|{payload.username}"
    if _login_limiter.limited(key):
        raise HTTPException(status_code=429, detail="失败次数过多，请稍后再试")
    user = session.scalar(select(User).where(User.username == payload.username))
    if user is None or not verify_password(payload.password, user.password_hash):
        # 不区分"用户不存在 / 密码错误"，防账号枚举
        _login_limiter.record(key)
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    _login_limiter.clear(key)
    return _auth_response(user)


@router.get("/me", summary="当前登录用户")
def me(current: dict = Depends(get_current_user),
       session: Session = Depends(get_session)) -> dict:
    """返回当前 token 对应的用户；用户已被删除时同样报 401。"""
    user = session.get(User, current["id"])
    if user is None:
        raise HTTPException(status_code=401, detail="该账号已不存在，请重新注册或登录")
    return user.public()


@router.post("/logout", summary="退出登录")
def logout(current: dict = Depends(get_current_user)) -> dict:
    """JWT 是无状态 token，退出 = 前端丢弃 token；这里只作确认与界面语义对齐。"""
    return {"ok": True}

