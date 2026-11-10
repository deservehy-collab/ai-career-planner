"""共享的 LLM 配置与结构化输出工具：本地 Ollama 与云端 API 双通道。

所有 Agent 一律通过 ``get_llm()`` / ``structured_invoke()`` 取模型，不直接持有实例。
当前生效通道由 ``set_channel()`` 切换，并由 backend 在启动时从 ``runs/settings.json`` 恢复。

云端通道走 OpenAI 兼容协议（默认智谱 GLM-4.7-Flash，免费档）：
换任何 OpenAI 兼容厂商只需改 ``.env`` 里的三行
（``CLOUD_BASE_URL`` / ``CLOUD_API_KEY`` / ``CLOUD_MODEL``），代码一行不动。
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import re
import threading
import time
from typing import Type, TypeVar

from langchain_ollama import ChatOllama
from pydantic import BaseModel

logger = logging.getLogger("agents.llm")

# ── 通道常量 ────────────────────────────────────────────────────────────
CHANNEL_LOCAL = "local"  # 本地 Ollama（qwen2.5:3b）
CHANNEL_CLOUD = "cloud"  # OpenAI 兼容云端 API（默认智谱）
CHANNELS = (CHANNEL_LOCAL, CHANNEL_CLOUD)

# ── 本地模型参数 ─────────────────────────────────────────────────────────
MODEL_NAME = "qwen2.5:3b"

#: 单次生成的 token 硬上限。**必须设**：Ollama 默认 num_predict=-1（无限生成），
#: 小模型偶尔不吐 EOS 就会一直说到天荒地老，而它独占 Ollama 唯一的推理槽，
#: 之后所有请求（含另外六个节点）全部排队饿死 —— 且客户端断开也停不掉它。
MAX_OUTPUT_TOKENS = 2048

#: 上下文窗口。Ollama 默认 4096，塞满后会自动丢弃早期 token 续命（context shift），
#: 模型在这种状态下最容易不吐 EOS，是上面那种失控的触发条件。
CONTEXT_TOKENS = 8192

# ── 云端默认值（只做兜底，以 .env 为准）──────────────────────────────────
CLOUD_BASE_URL_DEFAULT = "https://open.bigmodel.cn/api/paas/v4"
CLOUD_MODEL_DEFAULT = "glm-4.7-flash"

# ── 通道状态（进程内全局；持久化在 backend，app 启动时恢复）────────────────
_channel: str = CHANNEL_LOCAL

_local = None
_cloud = None

#: 云端鉴权失败过的标记（key 无效时置位），状态页据此提示"已自动落回本地"
_cloud_auth_failed = False

#: 云端连续限流/超时后，在这段时间内直接用本地模型 —— 否则工作流每个节点
#: 都要先等一轮重试（2s + 5s），一次分析会白等几十秒。
CLOUD_DEGRADED_COOLDOWN = 60.0
_cloud_degraded_until: float = 0.0

#: 本次运行收集的降级提示。用 contextvar 而不是 thread-local：LangGraph 的
#: 并行节点跑在线程池里，context 会随任务复制过去，各分支写的是同一个列表；
#: thread-local 会让那些分支的提示在汇总时凭空消失。
_warning_capture: contextvars.ContextVar[list[str] | None] = contextvars.ContextVar(
    "career_llm_warnings", default=None
)
_warning_lock = threading.Lock()


def begin_warning_capture() -> None:
    """开始收集本次运行中的降级提示（进入工作流前调用）。"""
    _warning_capture.set([])


def take_warnings() -> list[str]:
    """取走并清空本次运行收集到的降级提示。"""
    entries = list(_warning_capture.get() or [])
    _warning_capture.set([])
    return entries


def _warn_once(message: str) -> None:
    """写日志，并在捕获期内记一条给用户看的提示（同一条不重复记录）。"""
    logger.warning(message)
    entries = _warning_capture.get()
    if entries is None:
        return
    with _warning_lock:
        if message not in entries:
            entries.append(message)


def get_channel() -> str:
    """当前生效通道。"""
    return _channel


def set_channel(value: str) -> None:
    """切换通道。非法值直接抛 ValueError，调用方（API 层）转成 422。"""
    global _channel, _cloud_auth_failed, _cloud_degraded_until
    if value not in CHANNELS:
        raise ValueError(f"未知通道 {value!r}，只能是 {' 或 '.join(CHANNELS)}")
    _channel = value
    _cloud_auth_failed = False
    _cloud_degraded_until = 0.0


def cloud_settings() -> dict:
    """云端三要素，env 优先，缺省用智谱默认值。"""
    return {
        "base_url": (os.environ.get("CLOUD_BASE_URL") or CLOUD_BASE_URL_DEFAULT).rstrip("/"),
        "api_key": (os.environ.get("CLOUD_API_KEY") or "").strip(),
        "model": (os.environ.get("CLOUD_MODEL") or CLOUD_MODEL_DEFAULT).strip(),
    }


def cloud_configured() -> bool:
    """云端通道是否可用：只认 API key。没 key 时前端把云端选项置灰。"""
    return bool(cloud_settings()["api_key"])


def cloud_auth_failed() -> bool:
    """最近一次云端调用是否因 key 无效而落回过本地。"""
    return _cloud_auth_failed


def mark_cloud_auth_failed() -> None:
    """外部调用方（如聊天流）在云端 401 时置位，状态页据此提示。"""
    global _cloud_auth_failed
    _cloud_auth_failed = True


def cloud_degraded() -> bool:
    """当前是否处于"云端刚失败、暂时直接用本地"的冷却期。"""
    return time.monotonic() < _cloud_degraded_until


def active_model_name() -> str:
    """当前通道实际使用的模型名（状态页展示用）。"""
    if _channel == CHANNEL_CLOUD and cloud_configured():
        return cloud_settings()["model"]
    return MODEL_NAME


def _local_instance() -> ChatOllama:
    global _local
    if _local is None:
        _local = ChatOllama(
            model=MODEL_NAME,
            temperature=0.2,
            num_predict=MAX_OUTPUT_TOKENS,
            num_ctx=CONTEXT_TOKENS,
        )
    return _local


def _cloud_instance():
    global _cloud
    if _cloud is None:
        settings = cloud_settings()
        if not settings["api_key"]:
            raise RuntimeError("未配置 CLOUD_API_KEY，云端通道不可用")
        try:
            from langchain_openai import ChatOpenAI
        except ImportError as exc:
            raise RuntimeError(
                "云端通道需要 langchain-openai 依赖，请先 pip install langchain-openai"
            ) from exc
        _cloud = ChatOpenAI(
            model=settings["model"],
            api_key=settings["api_key"],
            base_url=settings["base_url"],
            temperature=0.2,
            max_tokens=MAX_OUTPUT_TOKENS,
            timeout=120,
        )
    return _cloud


def get_llm():
    """当前通道的模型实例。云端未配 key / 缺依赖时自动落回本地并记一条警告。"""
    if _channel == CHANNEL_CLOUD:
        if not cloud_configured():
            logger.warning("通道选了云端但未配置 CLOUD_API_KEY，本次回落本地 Ollama")
            return _local_instance()
        try:
            return _cloud_instance()
        except RuntimeError as exc:
            logger.warning("云端实例不可用（%s），本次回落本地 Ollama", exc)
            return _local_instance()
    return _local_instance()


T = TypeVar("T", bound=BaseModel)

_JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)
_CODE_FENCE = re.compile(r"^\s*```[a-zA-Z]*\s*|\s*```\s*$")


def _extract_json(text: str) -> dict:
    """从模型输出里挖出第一个 JSON 对象（容忍 ```json 包裹和前后废话）。"""
    cleaned = _CODE_FENCE.sub("", text.strip())
    match = _JSON_OBJECT.search(cleaned)
    if not match:
        raise ValueError(f"输出里找不到 JSON 对象：{text[:200]}")
    return json.loads(match.group(0))


#: 云端限流/超时的重试间隔（秒）。免费档 429 通常几秒内恢复；
#: 超时再给一次机会兜住网络抖动，再多只是让用户干等。
CLOUD_RETRY_DELAYS = (2.0, 5.0)

#: 值得重试的报错特征。余额不足（1113）、鉴权失败、schema 校验失败都不在内：
#: 它们重试多少次结果都一样，只会让一次失败的分析多等十几秒。
_TRANSIENT_MARKERS = (
    "429", "rate limit", "rate_limit", "too many requests",
    "1305", "访问量过大", "timeout", "timed out",
)


def _is_rate_limited(exc: Exception) -> bool:
    """区分"被限流"和"网络超时" —— 给用户的提示不一样。"""
    name = type(exc).__name__.lower()
    text = str(exc).lower()
    return "ratelimit" in name or any(
        marker in text
        for marker in ("429", "rate limit", "rate_limit", "too many requests", "1305", "访问量过大")
    )


def _is_transient_cloud_failure(exc: Exception) -> bool:
    """只认限流与超时这两类可以靠重试解决的故障。"""
    name = type(exc).__name__.lower()
    if "ratelimit" in name or "timeout" in name:
        return True
    text = str(exc).lower()
    return any(marker in text for marker in _TRANSIENT_MARKERS)


def _auth_warning() -> str:
    return (
        "云端 API key 无效或已过期（401），本次已自动改用本地模型 "
        f"{MODEL_NAME}。请检查 .env 里的 CLOUD_API_KEY，修正后到「系统状态」页"
        "重新切换云端即可重试。"
    )


def _transient_warning(exc: Exception) -> str:
    cause = (
        "被限流（429，免费档常见）"
        if _is_rate_limited(exc)
        else "超时或网络中断"
    )
    return (
        f"云端模型{cause}，自动重试后仍未成功，本次已改用本地模型 {MODEL_NAME}，"
        "生成仍会继续。本次结果质量以本机模型为准；稍后可在「系统状态」页切回云端重试。"
    )


def _is_auth_failure(exc: Exception) -> bool:
    """判断云端报错是不是"key 无效"这一类。只看类型名与文本特征，不依赖 SDK 异常层级。"""
    name = type(exc).__name__.lower()
    text = str(exc).lower()
    if any(word in name for word in ("authentication", "permission", "unauthorized")):
        return True
    return any(token in text for token in ("401", "invalid api key", "无效的api key", "unauthorized"))


def _invoke_local(prompt: str, schema: Type[T]) -> T:
    """本地 Ollama 的结构化输出。小模型的 json_schema 支持偶尔不稳定，所以准备了两条路：

    1. Ollama 原生 json_schema 结构化输出（首选）
    2. format="json" + 手动解析（降级）

    两条都失败时抛 RuntimeError，并把两次的原始错误带上，方便定位。
    """
    llm = _local_instance()
    try:
        result = llm.with_structured_output(schema, method="json_schema").invoke(prompt)
        # 正常情况下已经是 schema 实例，个别版本会返回 dict
        return result if isinstance(result, schema) else schema.model_validate(result)
    except Exception as structured_error:
        try:
            raw = llm.invoke(prompt, format="json").content
            return schema.model_validate(_extract_json(raw))
        except Exception as parse_error:
            raise RuntimeError(
                f"[{schema.__name__}] 本地模型 {MODEL_NAME} 没能返回可解析的结构化结果。"
                f"请确认 Ollama 已启动（ollama serve），且模型已拉取（ollama pull {MODEL_NAME}）。\n"
                f"  json_schema 尝试：{type(structured_error).__name__}: {structured_error}\n"
                f"  JSON 解析尝试：{type(parse_error).__name__}: {parse_error}"
            ) from parse_error


_OUTPUT_CONTRACT = (
    "\n\n【输出格式（最高优先级）】\n"
    "只输出一个 JSON 对象，不要 Markdown 代码块、表格、标题或任何解释。\n"
    "字段名、层级与枚举值必须严格符合下面的 JSON Schema，不得改名或增删字段：\n"
)


def _cloud_prompt(prompt: str, schema: Type[T]) -> str:
    """给云端提示词补一段强约束的输出格式。

    部分 OpenAI 兼容模型（如 glm-4.7-flash）会无视 function calling，
    先写一大段 Markdown 分析；输出额度耗尽后连 JSON 都没有，整次分析失败。
    把 schema 原文和"只输出 JSON"写进提示词后，工具调用路径能稳定返回结构化结果。
    本地 Ollama 走 json_schema 强约束，不需要这段（也避免白占 8K 上下文）。
    """
    try:
        schema_text = json.dumps(schema.model_json_schema(), ensure_ascii=False)
    except Exception:  # noqa: BLE001 - 生成不出 schema 时保持原提示词，不阻断调用
        return prompt
    return (
        f"{prompt}{_OUTPUT_CONTRACT}{schema_text}\n"
        "现在直接输出结果 JSON（不要复述上面的 schema）："
    )


def _invoke_cloud(prompt: str, schema: Type[T]) -> T:
    """云端的结构化输出。两条路：

    1. 函数调用（with_structured_output，OpenAI 兼容厂商的标准做法）
    2. 普通对话 + 手动解析（个别厂商不支持 function calling 时降级）

    鉴权类报错（key 无效）不做第二次尝试 —— 换一条路也过不了鉴权，直接上抛交给
    structured_invoke 决定是否落回本地。
    """
    prompt = _cloud_prompt(prompt, schema)
    llm = _cloud_instance()
    try:
        result = llm.with_structured_output(schema).invoke(prompt)
        return result if isinstance(result, schema) else schema.model_validate(result)
    except Exception as structured_error:
        # 鉴权/限流/超时不在本函数内换第二条路：换路也是同一个云端，只会多打一次
        # 请求。交给 structured_invoke 统一延迟重试或降级。
        if (
            _is_auth_failure(structured_error)
            or _is_transient_cloud_failure(structured_error)
        ):
            raise
        try:
            raw = llm.invoke(prompt).content
            return schema.model_validate(_extract_json(raw))
        except Exception as parse_error:
            if (
                _is_auth_failure(parse_error)
                or _is_transient_cloud_failure(parse_error)
            ):
                raise
            raise RuntimeError(
                f"[{schema.__name__}] 结构化输出失败（云端 {cloud_settings()['model']}）\n"
                f"  函数调用尝试：{type(structured_error).__name__}: {structured_error}\n"
                f"  JSON 解析尝试：{type(parse_error).__name__}: {parse_error}"
            ) from parse_error


def _fallback_to_local(prompt: str, schema: Type[T], cloud_error: Exception, reason: str) -> T:
    """云端降级到本地。本地也失败时，给出能照着做的启动命令，而不是只甩堆栈。"""
    try:
        return _invoke_local(prompt, schema)
    except Exception as local_error:
        raise RuntimeError(
            f"{reason}，本地模型 {MODEL_NAME} 也未能完成本次生成。"
            f"请确认 Ollama 已启动（ollama serve）且模型已拉取（ollama pull {MODEL_NAME}），"
            f"或稍后重试云端。\n"
            f"  云端原因：{type(cloud_error).__name__}: {cloud_error}\n"
            f"  本地原因：{type(local_error).__name__}: {local_error}"
        ) from local_error


def structured_invoke(prompt: str, schema: Type[T]) -> T:
    """让模型按 schema 输出结构化结果，返回校验过的 Pydantic 对象。

    路由规则：
    - 本地通道：``_invoke_local``；
    - 云端通道（已配 key）：``_invoke_cloud``，限流/超时先延迟重试；
    - 云端不可继续时**自动降级到本地**并记一条提示（由 backend 收进结果的
      ``llm_warnings``）：key 无效、重试后仍限流/超时、冷却期内、未配 key；
    - 其余云端错误（如余额不足）如实上抛 —— 换本地可能掩盖用户该处理的问题。
    """
    global _cloud_auth_failed, _cloud_degraded_until

    if _channel != CHANNEL_CLOUD:
        return _invoke_local(prompt, schema)

    if not cloud_configured():
        _warn_once(
            "当前通道是云端，但 .env 里没有配置 CLOUD_API_KEY，本次已改用本地模型 "
            f"{MODEL_NAME}。请配置 key，或到「系统状态」页把通道切回本地。"
        )
        return _invoke_local(prompt, schema)

    if _cloud_auth_failed:
        _warn_once(
            "云端 API key 此前鉴权失败（401），本次运行继续使用本地模型 "
            f"{MODEL_NAME}。请检查 .env 里的 CLOUD_API_KEY，修正后到「系统状态」页"
            "重新切换云端即可重试。"
        )
        return _invoke_local(prompt, schema)

    if cloud_degraded():
        # 本次运行已经记过限流/超时提示时不再重复；冷却期来自上一次运行才补一条。
        entries = _warning_capture.get()
        with _warning_lock:
            if entries is not None and not entries:
                entries.append(
                    "云端模型刚发生限流/超时，为避免每个步骤都重复等待，本次运行继续使用本地模型 "
                    f"{MODEL_NAME}。稍后可在「系统状态」页切回云端重试。"
                )
        return _invoke_local(prompt, schema)

    for attempt in range(len(CLOUD_RETRY_DELAYS) + 1):
        try:
            return _invoke_cloud(prompt, schema)
        except Exception as exc:
            if _is_auth_failure(exc):
                _cloud_auth_failed = True
                _warn_once(_auth_warning())
                return _fallback_to_local(prompt, schema, exc, "云端 key 鉴权失败")
            if _is_transient_cloud_failure(exc) and attempt < len(CLOUD_RETRY_DELAYS):
                delay = CLOUD_RETRY_DELAYS[attempt]
                logger.warning(
                    "云端调用被限流或超时（%s），%.0f 秒后第 %d 次重试",
                    type(exc).__name__, delay, attempt + 1,
                )
                time.sleep(delay)
                continue
            if _is_transient_cloud_failure(exc):
                _cloud_degraded_until = time.monotonic() + CLOUD_DEGRADED_COOLDOWN
                _warn_once(_transient_warning(exc))
                return _fallback_to_local(prompt, schema, exc, "云端自动重试后仍限流/超时")
            raise

    # 循环正常只会通过 return / raise 结束；这里兜底保证函数总有返回值。
    return _invoke_local(prompt, schema)
