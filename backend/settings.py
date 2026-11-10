"""进程设置持久化：``runs/settings.json``。

目前只有一项：``llm_channel``（模型通道选择，local / cloud）。

路径**锚定在认证库所在目录**（默认 runs/，见 backend/config.py 的动态路径注释）：
测试把 ``AUTH_DB_PATH`` 指到临时目录时，设置文件自动跟着隔离，不会碰项目 runs/。
"""

from __future__ import annotations

import json
from pathlib import Path

from agents.llm import CHANNEL_LOCAL, CHANNELS
from backend import config

__all__ = ["settings_path", "load_channel", "save_channel"]

SETTINGS_FILENAME = "settings.json"


def settings_path() -> Path:
    """设置文件路径（动态读取，测试可整体隔离）。"""
    return Path(config.AUTH_DB_PATH).parent / SETTINGS_FILENAME


def _load() -> dict:
    """读设置。文件不存在 / 损坏都返回空 dict —— 设置丢了不该挡住服务启动。"""
    path = settings_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def load_channel() -> str:
    """读取持久化的通道。非法值一律回落默认 local。"""
    value = _load().get("llm_channel")
    return value if value in CHANNELS else CHANNEL_LOCAL


def save_channel(channel: str) -> None:
    """把通道写进 settings.json（整体读-改-写，与其他 store 同风格）。"""
    data = _load()
    data["llm_channel"] = channel
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
