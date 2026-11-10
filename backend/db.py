"""SQLAlchemy 引擎、Session 与账号表。

数据库地址
----------
默认沿用 ``runs/app.db``（SQLite），设置环境变量 ``CAREER_DB_URL`` 后切换到
MySQL，例如::

    CAREER_DB_URL=mysql+pymysql://user:password@127.0.0.1:3306/career_agent?charset=utf8mb4

运行期连接失败会直接报错，**不会**悄悄退回 SQLite/JSON；两套存储绝不能同时写。
测试通过 ``init_db(url)`` / ``session_factory(url)`` 使用临时库。

线程模型
--------
SQLite 连接必须 ``check_same_thread=False``；MySQL 使用小连接池。
项目后端仍是单 worker（见 ``backend/config.py`` 的 ``TASK_WORKERS``），
但每个请求/工作线程都从工厂拿自己的 Session，不共享请求 Session。
"""

from __future__ import annotations

import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import DateTime, String, create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from backend import config

__all__ = [
    "Base", "User", "get_session", "init_db", "session_for",
    "database_url", "engine_for", "session_factory",
]


class Base(DeclarativeBase):
    pass


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"
    __table_args__ = {
        "mysql_engine": "InnoDB",
        "mysql_charset": "utf8mb4",
        "mysql_collate": "utf8mb4_unicode_ci",
    }

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)

    def public(self) -> dict:
        """返回给前端的用户信息：**永远不包含**密码哈希。"""
        return {
            "id": self.id,
            "username": self.username,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


_engines: dict[str, Engine] = {}
_factories: dict[str, sessionmaker] = {}
_cache_lock = threading.RLock()


def database_url() -> str:
    """当前进程使用的数据库地址；每次调用都重新读环境变量（便于测试覆盖）。"""
    configured = os.environ.get("CAREER_DB_URL", "").strip()
    if configured:
        return configured
    return f"sqlite:///{Path(config.AUTH_DB_PATH)}"


def engine_for(url: str) -> Engine:
    """按 URL 惰性缓存引擎。SQLite 打开外键约束，MySQL 使用保守连接池。"""
    with _cache_lock:
        if url in _engines:
            return _engines[url]

        if url.startswith("sqlite"):
            engine = create_engine(url, connect_args={"check_same_thread": False})

            @event.listens_for(engine, "connect")
            def _sqlite_foreign_keys(dbapi_connection, _record):  # pragma: no cover - 驱动回调
                cursor = dbapi_connection.cursor()
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.close()

        else:
            engine = create_engine(
                url,
                pool_pre_ping=True,
                pool_recycle=1800,
                pool_size=5,
                max_overflow=0,
                pool_timeout=5,
            )
        _engines[url] = engine
        return engine


def session_factory(url: str) -> sessionmaker:
    with _cache_lock:
        if url not in _factories:
            _factories[url] = sessionmaker(
                bind=engine_for(url), expire_on_commit=False
            )
        return _factories[url]


def session_for(db_path: Path) -> Session:
    """针对指定 SQLite 文件开一个 Session（认证测试沿用此入口）。"""
    return _factory(Path(db_path))()


def _factory(db_path: Path) -> sessionmaker:
    """兼容旧入口：按 SQLite 路径缓存 sessionmaker（建表幂等）。"""
    url = f"sqlite:///{db_path}"
    with _cache_lock:
        if url not in _factories:
            db_path.parent.mkdir(parents=True, exist_ok=True)
            engine = engine_for(url)
            _metadata().create_all(engine)
            _factories[url] = sessionmaker(bind=engine, expire_on_commit=False)
        return _factories[url]


def _metadata():
    """六张表的元数据。

    必须从 ``backend.models`` 取 Base，而不是本模块的 ``Base``：
    ``python -m backend.db`` 会先把本文件加载成 ``__main__``，随后
    ``backend.models`` 又按正规名字导入一次本模块，两个 ``Base`` 不共享
    ``metadata`` —— 用 ``__main__.Base`` 建表只会建出 ``users``。
    """
    from backend import models  # noqa: F401

    return models.Base.metadata


def init_db(url: str | None = None) -> None:
    """确保数据库与六张表就绪（幂等）。

    MySQL 的 DDL 会隐式提交，失败时不会“整体回滚”；因此这里只负责
    在空库/已存在的库上补齐结构，并在失败时把异常交给启动流程。
    """
    _metadata().create_all(engine_for(url or database_url()))


def get_session():
    """FastAPI 依赖：每请求一个 Session，请求结束自动关闭。"""
    with session_factory(database_url())() as session:
        yield session


def _cli(argv=None) -> int:
    import argparse
    import sys

    from sqlalchemy.engine import make_url

    parser = argparse.ArgumentParser(description="数据库结构命令")
    parser.add_argument("--init", action="store_true", help="幂等创建六张表")
    args = parser.parse_args(argv)
    if not args.init:
        parser.print_help()
        return 2
    url = database_url()
    try:
        init_db(url)
    except Exception as exc:  # 连接失败要直接报出来，不退回 SQLite/JSON
        print(f"建表失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    # 只打印方言，不打印可能含密码的连接串。
    print(f"数据库结构已就绪（{make_url(url).get_backend_name()}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
