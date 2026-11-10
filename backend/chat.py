"""AI 助手聊天接口：``POST /api/chat/stream`` —— 真流式（SSE），服务端无会话存储。

与求职规划工作流的边界
----------------------
聊天是**独立入口**：本模块不 import ``graph.workflow``、不调用七节点流水线，
也不读写 ``runs/growth.json`` / ``runs/loop.json``。普通提问因此不可能覆盖或
触发职业规划的业务状态（这条边界由 tests/test_chat_api.py 用文件系统快照强制）。

为什么不复用 ``agents/llm.py`` 的全局 ``llm``
--------------------------------------------
那个实例是为结构化输出（``with_structured_output``）配置的，
流水线每次调用都要它。聊天只需要一段人话 + 逐 token 推送 + **可被中途取消**，
所以这里用标准库 ``urllib`` 直连上游：

- 本地通道：Ollama 的 ``POST /api/chat``（``stream: true``）NDJSON 流；
- 云端通道：OpenAI 兼容的 ``POST /chat/completions`` SSE 流（Authorization 头）。

两条路都不经过 langchain，因此：

- 不新增任何依赖；
- 客户端断开（点「停止生成」/关页面）时能显式关闭上游响应，Ollama 随即停止推理
  —— 前端的停止按钮因此是真实生效的，不是 UI 假象。

模型名与地址的唯一来源
----------------------
- 模型：``agents.llm.MODEL_NAME``（全项目唯一定义，backend/config.py 也从这里导入），
  聊天与流水线用的是同一个模型，不另立第二套。
- 地址：环境变量 ``OLLAMA_HOST`` 优先（``ChatOllama`` 未传 base_url 时就是这个口径），
  否则退回 ``backend.config.OLLAMA_BASE_URL``。两处都指向同一个解析函数，不散落硬编码。

前端协议（SSE，UTF-8）
----------------------
``event: start`` → ``data: {"model": "qwen2.5:3b"}``
``event: delta`` → ``data: {"text": "增量文本"}``
``event: done``  → ``data: {"chars": 128}``
``event: error`` → ``data: {"message": "给普通用户看的一句话"}``

请求前的硬故障（Ollama 未启动 / 模型缺失 / 输入非法）走标准 HTTP 状态码
（503 / 422），响应体是 JSON；流开始之后的故障只能以 ``error`` 事件告知，
因为响应头已经发出。错误文本一律是人话，**不含异常堆栈、绝对路径或环境变量**。
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, StringConstraints, field_validator

from agents.llm import (
    CHANNEL_CLOUD, CONTEXT_TOKENS, MODEL_NAME, cloud_configured, cloud_settings,
    get_channel, mark_cloud_auth_failed,
)
from backend import config

__all__ = ["router", "build_model_messages", "resolve_base_url"]

logger = logging.getLogger("backend.chat")

router = APIRouter(prefix="/api/chat", tags=["chat"])

# ── 聊天约束（集中在本模块，前端只需照做）────────────────────────────────
#: 单条用户消息的字符上限（超出 → 422，避免超长 prompt 把本地小模型拖死）
CHAT_MAX_INPUT_CHARS = 2000
#: 送进模型的历史消息条数上限（不含 system 与本轮提问）
CHAT_MAX_HISTORY_MESSAGES = 10
#: 送进模型的总字符预算（system + 历史 + 本轮），从最近一轮往前累加，超了丢最早的
CHAT_MAX_CONTEXT_CHARS = 6000
#: 单条历史消息的字符上限。**超出是裁剪而不是 422**，见 ChatHistoryItem
CHAT_MAX_HISTORY_ITEM_CHARS = 4000
#: 客户端一次最多提交的 history 条数（只校验，超出 → 422）
CHAT_MAX_HISTORY_SUBMITTED = 60
#: 聊天温度：比流水线的 0.2 略高，问答更自然；不影响任何规划业务
CHAT_TEMPERATURE = 0.4
#: 单次回答的 token 上限。聊天答案不需要规划那么长，收紧到 800；
#: 不设的话 Ollama 会无限生成并独占推理槽（见 agents/llm.py 的 MAX_OUTPUT_TOKENS）。
CHAT_MAX_OUTPUT_TOKENS = 800
#: 上游 Ollama 的 socket 超时（秒）。首次加载模型可能较慢，给足余量。
CHAT_UPSTREAM_TIMEOUT = 180.0

#: 系统提示：限定角色与范围。刻意不提"内部规则/数据"，避免小模型编造规划结果。
SYSTEM_PROMPT = (
    "你是「AI Career Agent」里的 AI 助手，服务中国大学生与应届生的求职与职业成长。"
    "请用简体中文回答，聚焦：岗位方向选择、技能清单与学习顺序、简历与项目包装、"
    "面试准备、实习与校招节奏。"
    "回答要求：条理清晰，能用列表就用列表，代码或命令用 markdown 代码块；"
    "不确定的事实直说不确定，不要编造具体公司的内部数据、薪资数字或个人经历；"
    "不要声称你已经看过用户的画像、成长记录或岗位分析结果（那些数据只在用户当轮消息里提供）。"
    "如果用户想要完整的职业规划方案，引导他使用左侧的「岗位分析」。"
)

#: 流开始后的失败文案（不含异常类型与堆栈）
STREAM_FAILURE_MESSAGE = (
    "本地模型在生成过程中断开或超时了。可能是 Ollama 正在加载模型、显存不足或响应过慢，"
    "请稍等几秒重试；若持续失败，请检查「系统状态」页里的 Ollama 与模型是否就绪。"
)

#: 云端流失败文案。不能复用本地那条 —— 免费档失败最常见的原因是 429 限流，
#: 告诉用户去查 Ollama 只会把人带偏。
CLOUD_STREAM_FAILURE_MESSAGE = (
    "云端模型这次没能返回结果。免费档最常见的是访问量限流，也可能是网络波动。"
    "请稍等十几秒再试；若持续失败，可以在「系统状态」页先切回本地模型。"
)

#: 流开始前的 429 / 连接失败重试间隔（秒）。限流通常几秒内恢复，
#: 流一旦开始就不能重试，否则会把前半段回答重复推给前端。
CLOUD_RETRY_DELAYS = (2.0, 5.0)

VALID_ROLES = ("user", "assistant")


# ── 请求模型 ────────────────────────────────────────────────────────────
ChatInputText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=CHAT_MAX_INPUT_CHARS),
]


class ChatHistoryItem(BaseModel):
    """历史消息：只接受 user / assistant 两种角色，其他一律 422。"""

    role: str = Field(description="user | assistant")
    # 刻意**不写 max_length**：超长由下面的 validator 裁剪。
    # 用 Field(max_length=…) 会在 validator 之前判 422，用户追问就被上一轮的长回答钉死了。
    content: str = Field(min_length=1)

    @field_validator("role")
    @classmethod
    def check_role(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in VALID_ROLES:
            raise ValueError(f"role 只能是 {' 或 '.join(VALID_ROLES)}")
        return normalized

    @field_validator("content")
    @classmethod
    def check_content(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("content 不能是空白")
        # 长回答回传时**裁剪**：模型一条答复就可能超过上限，
        # 如果这里报 422，用户只是正常追问却被挡住，等于把会话弄死。
        # 真正约束上下文的是 CHAT_MAX_CONTEXT_CHARS（build_model_messages 里做）。
        return stripped[:CHAT_MAX_HISTORY_ITEM_CHARS]


class ChatRequest(BaseModel):
    """``POST /api/chat/stream`` 的请求体。

    会话状态由**前端**持有：本轮 ``message`` 之前的对话以 ``history`` 回传，
    服务端不保存任何聊天内容（没有聊天表，也不写 runs/）。
    """

    message: ChatInputText = Field(description="本轮用户提问")
    history: list[ChatHistoryItem] = Field(default_factory=list, description="本轮之前的对话")

    @field_validator("history")
    @classmethod
    def check_history_size(cls, value: list[ChatHistoryItem]) -> list[ChatHistoryItem]:
        if len(value) > CHAT_MAX_HISTORY_SUBMITTED:
            raise ValueError(f"history 最多提交 {CHAT_MAX_HISTORY_SUBMITTED} 条")
        return value


# ── 上下文裁剪 ──────────────────────────────────────────────────────────
def build_model_messages(
    message: str, history: list[ChatHistoryItem] | None = None
) -> list[dict[str, str]]:
    """把前端传来的会话裁成**有界**的 Ollama messages。

    裁剪规则（顺序不能反）：

    1. 只保留最后 ``CHAT_MAX_HISTORY_MESSAGES`` 条历史；
    2. 从最近的一条往前累加字符数，超过 ``CHAT_MAX_CONTEXT_CHARS`` 就停止收录
       —— 丢的是**最早**的消息，最近上下文优先；
    3. 开头固定加 system，结尾固定加本轮提问。

    本地小模型的上下文越长越慢，这一步是**必须的**，不是优化项。
    """
    items = list(history or [])[-CHAT_MAX_HISTORY_MESSAGES:]

    kept: list[dict[str, str]] = []
    used = len(SYSTEM_PROMPT) + len(message)
    for item in reversed(items):
        cost = len(item.content)
        if used + cost > CHAT_MAX_CONTEXT_CHARS:
            break
        used += cost
        kept.append({"role": item.role, "content": item.content})
    kept.reverse()

    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        *kept,
        {"role": "user", "content": message},
    ]


# ── Ollama 调用 ────────────────────────────────────────────────────────
def resolve_base_url() -> str:
    """聊天使用的 Ollama 地址：``OLLAMA_HOST`` 优先，否则用 config 里的常量。"""
    env_host = (os.environ.get("OLLAMA_HOST") or "").strip()
    if not env_host:
        return config.OLLAMA_BASE_URL.rstrip("/")
    if env_host.startswith(("http://", "https://")):
        return env_host.rstrip("/")
    return f"http://{env_host.rstrip('/')}"


def _post(target: str, payload: dict[str, Any], timeout: float) -> Any:
    request = urllib.request.Request(
        target,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    return urllib.request.urlopen(request, timeout=timeout)  # noqa: S310 - 地址来自本地配置


def _probe_models(base_url: str) -> tuple[bool, list[str], str | None]:
    """探一下 Ollama 是否可达并列出已装模型。只读 /api/tags，不生成内容。"""
    target = f"{base_url}{config.OLLAMA_TAGS_PATH}"
    try:
        with urllib.request.urlopen(target, timeout=3.0) as response:  # noqa: S310
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        logger.warning("Ollama 探测失败：%s", exc)
        return False, [], exc
    names = [str(item.get("name") or "") for item in (payload.get("models") or [])]
    return True, names, None


def _model_installed(names: list[str]) -> bool:
    """按 ``name:tag`` 的 name 部分比对，与 backend/services.py 的口径一致。"""
    wanted = MODEL_NAME.split(":")[0]
    return any(name.split(":")[0] == wanted for name in names)


def _sse(event: str, data: dict[str, Any]) -> str:
    """一条 SSE 消息。ensure_ascii=False 保证中文按 UTF-8 原样下发。"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _stream_upstream(messages: list[dict[str, str]]) -> Iterator[str]:
    """把 Ollama 的 NDJSON 流转成前端约定的 SSE 事件。

    ``with response`` 会在上游连接关闭时通知 Ollama，但**这不构成可靠的停止手段**：
    Ollama 0.34.4 实测在客户端断开后仍继续生成（日志里 n_gen 持续上涨，请求返回 500
    而任务未终止），且它只有单个推理槽，一次失控生成会饿死之后的所有请求。
    真正兜住这件事的是 ``num_predict`` —— 所以它必须留在这里。
    """
    payload = {
        "model": MODEL_NAME,
        "messages": messages,
        "stream": True,
        "options": {
            "temperature": CHAT_TEMPERATURE,
            "num_predict": CHAT_MAX_OUTPUT_TOKENS,
            "num_ctx": CONTEXT_TOKENS,
        },
    }
    base_url = resolve_base_url()
    emitted = 0
    response = None
    # 先落一帧 start：前端据此确认"请求已被接受、正在等模型出字"，
    # 而不是把"首次加载模型可能要几秒"误判成请求卡死。
    yield _sse("start", {"model": MODEL_NAME})
    try:
        try:
            response = _post(f"{base_url}/api/chat", payload, CHAT_UPSTREAM_TIMEOUT)
            with response:
                for raw_line in response:
                    line = raw_line.strip()
                    if not line:
                        continue
                    try:
                        chunk = json.loads(line.decode("utf-8"))
                    except (UnicodeDecodeError, ValueError):
                        # 单行坏了不致命：跳过并如实记日志（不把原始内容透给前端）
                        logger.warning("Ollama 返回了无法解析的一行响应，已跳过")
                        continue
                    text = ((chunk.get("message") or {}).get("content")) or ""
                    if text:
                        emitted += len(text)
                        yield _sse("delta", {"text": text})
                    if chunk.get("done"):
                        break
        except (urllib.error.URLError, OSError, ValueError) as exc:
            logger.warning("聊天流式调用失败：%s", type(exc).__name__)
            yield _sse("error", {"message": STREAM_FAILURE_MESSAGE})
            return
    except GeneratorExit:
        # 客户端主动断开（停止生成 / 关闭页面）：正常收尾，不发事件也不向上抛噪声
        logger.info("客户端中断了聊天流，已生成 %d 字", emitted)
        raise
    yield _sse("done", {"chars": emitted})


#: 云端鉴权失败的文案（key 无效/过期，指导用户自查而不是甩堆栈）
CLOUD_AUTH_FAILURE_MESSAGE = (
    "云端 API 拒绝了请求：key 无效或已过期。请检查 .env 里的 CLOUD_API_KEY，"
    "或到「系统状态」页把通道切回本地模型。"
)


def _stream_upstream_cloud(messages: list[dict[str, str]]) -> Iterator[str]:
    """把 OpenAI 兼容的云端 SSE 流转成前端约定的 SSE 事件。

    协议与本地分支完全一致（start / delta / done / error），前端零改动。
    差异只在：目标地址、Authorization 头、以及 delta 的取值路径
    （``choices[0].delta.content`` 而非 ``message.content``）。
    """
    settings = cloud_settings()
    payload = {
        "model": settings["model"],
        "messages": messages,
        "stream": True,
        "temperature": CHAT_TEMPERATURE,
        "max_tokens": CHAT_MAX_OUTPUT_TOKENS,
    }
    emitted = 0
    yield _sse("start", {"model": settings["model"]})
    try:
        request = urllib.request.Request(
            f"{settings['base_url']}/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {settings['api_key']}",
            },
            method="POST",
        )
        attempt = 0
        while True:
            try:
                response = urllib.request.urlopen(request, timeout=CHAT_UPSTREAM_TIMEOUT)  # noqa: S310
                break
            except urllib.error.HTTPError as exc:
                if exc.code in (401, 403):
                    mark_cloud_auth_failed()
                    yield _sse("error", {"message": CLOUD_AUTH_FAILURE_MESSAGE})
                    return
                if exc.code == 429 and attempt < len(CLOUD_RETRY_DELAYS):
                    delay = CLOUD_RETRY_DELAYS[attempt]
                    attempt += 1
                    logger.info("云端聊天被限流（429），%.0f 秒后第 %d 次重试", delay, attempt)
                    time.sleep(delay)
                    continue
                logger.warning("云端聊天调用失败：HTTP %s", exc.code)
                yield _sse("error", {"message": CLOUD_STREAM_FAILURE_MESSAGE})
                return
            except (urllib.error.URLError, OSError, ValueError) as exc:
                if attempt < len(CLOUD_RETRY_DELAYS):
                    delay = CLOUD_RETRY_DELAYS[attempt]
                    attempt += 1
                    logger.info(
                        "云端聊天连接失败（%s），%.0f 秒后第 %d 次重试",
                        type(exc).__name__, delay, attempt,
                    )
                    time.sleep(delay)
                    continue
                logger.warning("云端聊天流式调用失败：%s", type(exc).__name__)
                yield _sse("error", {"message": CLOUD_STREAM_FAILURE_MESSAGE})
                return

        with response:
            for raw_line in response:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line or not line.startswith("data:"):
                    continue
                data = line[len("data:"):].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except ValueError:
                    logger.warning("云端返回了无法解析的一行响应，已跳过")
                    continue
                text = ""
                try:
                    text = chunk["choices"][0]["delta"].get("content") or ""
                except (KeyError, IndexError, TypeError):
                    pass
                if text:
                    emitted += len(text)
                    yield _sse("delta", {"text": text})
    except GeneratorExit:
        logger.info("客户端中断了云端聊天流，已生成 %d 字", emitted)
        raise
    yield _sse("done", {"chars": emitted})


# ── 路由 ────────────────────────────────────────────────────────────────
@router.post("/stream", summary="AI 助手流式对话")
def post_chat_stream(payload: ChatRequest) -> StreamingResponse:
    """校验输入 → 确认当前通道的模型可用 → 返回 SSE 流。

    通道由 ``agents.llm.get_channel()`` 决定（/api/status/llm-channel 切换）：

    - **本地**：探测 Ollama 可达且模型已装，否则 503；
    - **云端**：确认 CLOUD_API_KEY 已配置，否则 503 —— 这时提示用户回
      「系统状态」页切回本地，不替用户做决定。

    鉴权与业务接口同口径：在 ``backend/app.py`` 里以 ``auth_required`` 注册，
    所以聊天同样要求 Bearer token（测试可覆盖该依赖）。
    """
    messages = build_model_messages(payload.message, payload.history)

    if get_channel() == CHANNEL_CLOUD:
        if not cloud_configured():
            raise HTTPException(
                status_code=503,
                detail="当前通道是云端，但 .env 里没有配置 CLOUD_API_KEY。"
                       "请先配置 key，或到「系统状态」页把通道切回本地模型。",
            )
        logger.info(
            "聊天请求：model=%s（云端）本轮 %d 字，上下文 %d 条消息 / %d 字",
            cloud_settings()["model"], len(payload.message), len(messages),
            sum(len(item["content"]) for item in messages),
        )
        return StreamingResponse(
            _stream_upstream_cloud(messages),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",  # 反向代理不缓冲，保证真增量
            },
        )

    # 本地通道：先探模型，不启动/没装模型时直接 503 给人话，绝不让前端无限转圈
    reachable, names, _error = _probe_models(resolve_base_url())
    if not reachable:
        raise HTTPException(
            status_code=503,
            detail="本地模型服务 Ollama 没启动或不可达。请先运行 `ollama serve`，"
                   "再打开「系统状态」页确认 Ollama 变为就绪。",
        )
    if not _model_installed(names):
        raise HTTPException(
            status_code=503,
            detail=f"Ollama 里没有聊天模型 {MODEL_NAME}，请先执行 `ollama pull {MODEL_NAME}`。",
        )

    logger.info(
        "聊天请求：model=%s 本轮 %d 字，上下文 %d 条消息 / %d 字",
        MODEL_NAME, len(payload.message), len(messages),
        sum(len(item["content"]) for item in messages),
    )

    return StreamingResponse(
        _stream_upstream(messages),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # 反向代理不缓冲，保证真增量
        },
    )
