"""面试陪练：按当前档案的 JD、简历与成长事实出题（``POST /api/interview/questions``）。

边界
----
- **不读写业务数据**：档案由 ``get_plan`` 只读取一次，简历由前端把草稿带上来；
  本模块不调用工作流、不写 ``runs/``、不改六表。
- **不编造经历**：提示词只允许引用材料里的事实；材料不足时要求在要点里写
  「需要补充：……」，而不是替学生编一段项目。
- **失败说人话**：模型未启动 / 云端没配 key → 503；输出不是合法 JSON → 502。
  两类错误都不把异常堆栈、绝对路径或环境变量透给前端。

模型调用
--------
复用聊天接口的通道判断与探测（``backend.chat``），但这里要的是一次性完整回答
而不是逐字推送，所以直接用标准库 ``urllib`` 发非流式请求：本地走 Ollama
``/api/chat``，云端走 OpenAI 兼容 ``/chat/completions``。不新增依赖。
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from agents.llm import (
    CHANNEL_CLOUD, CONTEXT_TOKENS, MODEL_NAME, cloud_configured, cloud_settings,
    get_channel, mark_cloud_auth_failed,
)
from backend import chat
from backend.auth import get_current_user
from backend.deps import create_archive_factory
from backend.plan_repository import get_plan
from backend.resume_api import ResumeData

__all__ = [
    "router", "build_evidence", "build_messages", "parse_questions",
    "generate_questions", "ensure_category_coverage", "MAX_JD_CHARS", "MAX_QUESTIONS",
]

logger = logging.getLogger("backend.interview")

router = APIRouter(tags=["interview"])

#: 送进提示词的 JD 字符上限。本地小模型上下文很紧，原文再长也不会提高出题质量。
MAX_JD_CHARS = 3000
#: 材料条数 / 技能条数上限，防止简历写满时把上下文撑爆。
MAX_FACTS = 10
MAX_SKILLS = 20
#: 一次最多要 6 道；解析时允许到 8 道，多出来的截断。
MAX_QUESTIONS = 8
MIN_QUESTIONS = 3
#: 出题温度比聊天低：要稳定 JSON，不要发散。
INTERVIEW_TEMPERATURE = 0.3
INTERVIEW_MAX_OUTPUT_TOKENS = 1400
INTERVIEW_UPSTREAM_TIMEOUT = 150.0
#: 云端限流重试间隔（秒），与聊天接口同口径。
CLOUD_RETRY_DELAY = 2.0
#: 小模型偶尔会写坏 JSON（实测 1/8 出现多余引号）；这类失败换一次采样通常就好。
MODEL_ATTEMPTS = 2

CATEGORIES = ("技术", "项目", "动机", "反问")

#: 交给 Ollama 的结构化输出约束。纯 ``format="json"`` 会让 3B 模型只吐一个对象，
#: 实测 6/6 跑偏；按 schema 约束才能稳定得到 6 条数组。
QUESTION_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "category": {"type": "string", "enum": list(CATEGORIES)},
            "question": {"type": "string"},
            "why": {"type": "string"},
            "key_points": {"type": "array", "items": {"type": "string"}},
            "follow_up": {"type": "string"},
        },
        "required": ["category", "question", "why", "key_points", "follow_up"],
    },
    "minItems": 6,
    "maxItems": 6,
}

SYSTEM_PROMPT = (
    "你是一名校招技术面试官，正在为一名学生准备面试题。"
    "只能使用用户材料里出现的事实：材料没写的经历、公司、数字一律不许编造。"
    "如果某个问题需要学生补充真实经历，就在 key_points 里直接写"
    "「需要补充：……」并说明要补什么。"
    "只输出 JSON 数组，不要输出 markdown、解释或前后缀文字。"
)

UNAVAILABLE_LOCAL = (
    "本地模型服务 Ollama 没启动或不可达。请先运行 `ollama serve`，"
    "再打开「系统状态」页确认 Ollama 变为就绪。"
)
UNAVAILABLE_CLOUD = (
    "当前通道是云端，但 .env 里没有配置 CLOUD_API_KEY。"
    "请先配置 key，或到「系统状态」页把通道切回本地模型。"
)
UNAVAILABLE_RUNTIME = (
    "模型这次没有返回结果，可能是正在加载、显存不足或网络波动。"
    "请稍等十几秒重试；若持续失败，请检查「系统状态」页里的模型是否就绪。"
)


class InterviewUnavailable(RuntimeError):
    """模型通道不可用（→ 503）。消息直接给用户看。"""


class InterviewFormatError(RuntimeError):
    """模型输出不是约定的 JSON（→ 502）。"""


def _clean(value: object, limit: int) -> str:
    if value is None:
        return ""
    return str(value).strip()[:limit]


def _resume_facts(resume: dict) -> tuple[list[str], list[str]]:
    """从简历草稿提取「可出题」的事实。联系方式一律不进来。"""
    facts: list[str] = []
    skills: list[str] = []

    for group in resume.get("skills") or []:
        if not isinstance(group, dict):
            continue
        for item in group.get("items") or []:
            text = _clean(item, 60)
            if text and text not in skills:
                skills.append(text)

    for key in ("internships", "projects", "campus", "research"):
        for entry in resume.get(key) or []:
            if not isinstance(entry, dict):
                continue
            title = _clean(entry.get("title"), 80)
            role = _clean(entry.get("role"), 40)
            when = _clean(entry.get("time"), 40)
            bullets = [
                text for text in (
                    _clean(item, 120) for item in (entry.get("bullets") or [])
                ) if text
            ][:3]
            if not (title or bullets):
                continue
            head = " ｜ ".join(part for part in (when, title, role) if part)
            facts.append(f"{head}：{'；'.join(bullets)}" if bullets else head)

    summary = _clean(resume.get("summary"), 400)
    if summary:
        facts.append(f"自我评价：{summary}")

    certificates = [text for text in (_clean(item, 40) for item in (resume.get("certificates") or [])) if text]
    if certificates:
        facts.append("证书：" + "、".join(certificates[:6]))

    languages = [text for text in (_clean(item, 30) for item in (resume.get("languages") or [])) if text]
    if languages:
        facts.append("语言：" + "、".join(languages[:4]))

    return facts[:MAX_FACTS], skills[:MAX_SKILLS]


def _plan_facts(plan: dict) -> tuple[list[str], list[str]]:
    """没有简历草稿时退回档案里的画像与能力表，保证至少能出岗位相关题。"""
    profile = plan.get("profile_snapshot") or {}
    facts: list[str] = []
    skills: list[str] = []

    major = _clean(profile.get("major"), 40)
    if major:
        facts.append(f"专业：{major}")
    grade = _clean(profile.get("grade"), 20)
    if grade:
        facts.append(f"年级：{grade}")

    for project in profile.get("projects") or []:
        text = _clean(project, 120)
        if text and len(facts) < MAX_FACTS:
            facts.append(f"项目：{text}")

    for item in plan.get("current_skills") or []:
        text = _clean(item.get("skill") if isinstance(item, dict) else item, 60)
        if text and text not in skills:
            skills.append(text)

    for item in profile.get("skills") or []:
        text = _clean(item, 60)
        if text and text not in skills:
            skills.append(text)

    return facts[:MAX_FACTS], skills[:MAX_SKILLS]


def build_evidence(plan: dict | None, resume: dict | None) -> dict:
    """把一次出题需要的全部材料收拢成有界字典。

    ``source`` 只是给前端说明依据来源，不参与评分；简历草稿为空时退回档案，
    避免用户没打开过简历页就得到一份与岗位无关的通用题。
    """
    plan = plan or {}
    target_job = _clean(plan.get("target_job"), 200)
    jd_text = _clean(plan.get("jd_text"), MAX_JD_CHARS)

    facts, skills = _resume_facts(resume or {})
    source = "resume"
    if not facts and not skills:
        facts, skills = _plan_facts(plan)
        source = "plan"

    return {
        "target_job": target_job,
        "jd_text": jd_text,
        "facts": facts,
        "skills": skills,
        "source": source,
    }


def build_messages(evidence: dict) -> list[dict[str, str]]:
    """拼出一次非流式请求的 messages。材料全部有上限，长度可预期。"""
    lines: list[str] = [f"【目标岗位】{evidence['target_job'] or '未提供'}"]
    if evidence["jd_text"]:
        lines.append(f"【招聘要求原文】\n{evidence['jd_text']}")
    else:
        lines.append("【招聘要求原文】未提供，请围绕目标岗位的通用要求出题。")

    if evidence["skills"]:
        lines.append("【已写进简历/档案的技能】" + "、".join(evidence["skills"]))
    if evidence["facts"]:
        lines.append("【候选人材料】")
        lines.extend(f"- {item}" for item in evidence["facts"])
    else:
        lines.append("【候选人材料】暂无。项目题要明确提醒学生补充真实经历。")

    lines.append(
        "【出题要求】\n"
        "必须输出 6 个对象，category 顺序固定为：技术、技术、项目、项目、动机、反问。\n"
        "每道题是一个 JSON 对象：\n"
        '{"category":"技术|项目|动机|反问","question":"问题正文",'
        '"why":"这道题在核什么，一句话",'
        '"key_points":["答题要点 2-4 条，只能引用材料事实"],'
        '"follow_up":"一句追问"}\n'
        "限制：question ≤ 60 字；why ≤ 40 字；key_points 2-4 条、每条 ≤ 60 字；"
        "follow_up ≤ 40 字。材料不足以支撑项目题时仍要出题，"
        "但要点写成「需要补充：……」。只输出 JSON 数组本身。"
    )

    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": "\n\n".join(lines)},
    ]


def _strip_fence(raw: str) -> str:
    text = (raw or "").strip()
    if not text.startswith("```"):
        return text
    text = text[3:]
    if "\n" in text:
        text = text.split("\n", 1)[1]
    if text.rstrip().endswith("```"):
        text = text.rstrip()[:-3]
    return text.strip()


def _slice(text: str, opener: str, closer: str) -> str | None:
    start = text.find(opener)
    end = text.rfind(closer)
    if start == -1 or end <= start:
        return None
    return text[start:end + 1]


def _load_payload(raw: str) -> Any:
    text = _strip_fence(raw)
    candidates = [text, _slice(text, "[", "]"), _slice(text, "{", "}")]
    for candidate in candidates:
        if not candidate:
            continue
        try:
            return json.loads(candidate)
        except (TypeError, ValueError):
            continue
    raise InterviewFormatError("模型返回的内容不是 JSON。")


def _normalize_item(item: object) -> dict | None:
    if not isinstance(item, dict):
        return None
    question = _clean(item.get("question"), 200)
    if not question:
        return None

    category = _clean(item.get("category"), 10)
    if category not in CATEGORIES:
        category = "技术"

    points: list[str] = []
    for point in item.get("key_points") or []:
        text = _clean(point, 200)
        if text:
            points.append(text)
        if len(points) >= 4:
            break

    return {
        "category": category,
        "question": question,
        "why": _clean(item.get("why"), 200),
        "key_points": points,
        "follow_up": _clean(item.get("follow_up"), 200),
    }


def parse_questions(raw: str) -> list[dict]:
    """把模型输出收敛成前端可直接渲染的题目列表。

    容忍：代码围栏、JSON 前后的解释文字、``{"questions": [...]}`` 包装。
    拒绝：完全没有 JSON、去重后不足 ``MIN_QUESTIONS`` 道 —— 那说明这次返回
    不可用，宁可让用户重试，也不要把半截内容当结果展示。
    """
    payload = _load_payload(raw)
    if isinstance(payload, dict):
        payload = payload.get("questions") or payload.get("items") or payload.get("data")
    if not isinstance(payload, list):
        raise InterviewFormatError("模型返回的结构不是题目数组。")

    questions: list[dict] = []
    seen: set[str] = set()
    for item in payload:
        normalized = _normalize_item(item)
        if normalized is None or normalized["question"] in seen:
            continue
        seen.add(normalized["question"])
        questions.append(normalized)
        if len(questions) >= MAX_QUESTIONS:
            break

    if len(questions) < MIN_QUESTIONS:
        raise InterviewFormatError("模型返回的题目太少或格式不对。")
    return questions


def _post_json(target: str, payload: dict, headers: dict[str, str], timeout: float) -> Any:
    request = urllib.request.Request(
        target,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    return urllib.request.urlopen(request, timeout=timeout)  # noqa: S310 - 地址来自本地配置


def _call_local(messages: list[dict[str, str]]) -> str:
    reachable, names, _error = chat._probe_models(chat.resolve_base_url())
    if not reachable:
        raise InterviewUnavailable(UNAVAILABLE_LOCAL)
    if not chat._model_installed(names):
        raise InterviewUnavailable(
            f"Ollama 里没有模型 {MODEL_NAME}，请先执行 `ollama pull {MODEL_NAME}`。"
        )

    payload = {
        "model": MODEL_NAME,
        "messages": messages,
        "stream": False,
        # 用 schema 约束输出：3B 模型会偶发写出多余引号，提示词约束不住。
        "format": QUESTION_SCHEMA,
        "options": {
            "temperature": INTERVIEW_TEMPERATURE,
            "num_predict": INTERVIEW_MAX_OUTPUT_TOKENS,
            "num_ctx": CONTEXT_TOKENS,
        },
    }
    try:
        with _post_json(f"{chat.resolve_base_url()}/api/chat", payload, {}, INTERVIEW_UPSTREAM_TIMEOUT) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        logger.warning("面试出题调用本地模型失败：%s", type(exc).__name__)
        raise InterviewUnavailable(UNAVAILABLE_RUNTIME) from exc

    content = ((data.get("message") or {}).get("content")) or ""
    if not str(content).strip():
        raise InterviewUnavailable(UNAVAILABLE_RUNTIME)
    return str(content)


def _call_cloud(messages: list[dict[str, str]]) -> str:
    settings = cloud_settings()
    if not settings["api_key"]:
        raise InterviewUnavailable(UNAVAILABLE_CLOUD)

    payload = {
        "model": settings["model"],
        "messages": messages,
        "stream": False,
        "temperature": INTERVIEW_TEMPERATURE,
        "max_tokens": INTERVIEW_MAX_OUTPUT_TOKENS,
    }
    headers = {"Authorization": f"Bearer {settings['api_key']}"}
    target = f"{settings['base_url']}/chat/completions"

    attempt = 0
    while True:
        try:
            with _post_json(target, payload, headers, INTERVIEW_UPSTREAM_TIMEOUT) as response:
                data = json.loads(response.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                mark_cloud_auth_failed()
                raise InterviewUnavailable(
                    "云端 API 拒绝了请求：key 无效或已过期。请检查 .env 里的 "
                    "CLOUD_API_KEY，或到「系统状态」页把通道切回本地模型。"
                ) from exc
            if exc.code == 429 and attempt == 0:
                attempt += 1
                logger.info("云端出题被限流（429），%.0f 秒后重试", CLOUD_RETRY_DELAY)
                import time

                time.sleep(CLOUD_RETRY_DELAY)
                continue
            logger.warning("云端出题调用失败：HTTP %s", exc.code)
            raise InterviewUnavailable(UNAVAILABLE_RUNTIME) from exc
        except (urllib.error.URLError, OSError, ValueError) as exc:
            logger.warning("云端出题调用失败：%s", type(exc).__name__)
            raise InterviewUnavailable(UNAVAILABLE_RUNTIME) from exc

    try:
        content = data["choices"][0]["message"].get("content") or ""
    except (KeyError, IndexError, TypeError) as exc:
        raise InterviewUnavailable(UNAVAILABLE_RUNTIME) from exc
    if not str(content).strip():
        raise InterviewUnavailable(UNAVAILABLE_RUNTIME)
    return str(content)


def ensure_category_coverage(questions: list[dict], target_job: str) -> list[dict]:
    """「反问」固定由代码生成，不采信模型。

    本地 3B 模型会把普通技术题错标成反问（实测把它写成"Spring Boot 有哪些优势"），
    而这一栏的含义是**候选人问面试官**的问题，语义一错整组就废。这里直接丢掉
    模型给的反问题，补一条问团队预期的通用题：不涉及候选人经历，不会编造。
    技术/项目题必须来自材料，不拿通用问题凑数。
    """
    kept = [item for item in questions if item["category"] != "反问"]
    if len(kept) >= MAX_QUESTIONS:
        return kept

    job = (target_job or "这个岗位")[:20]
    return [
        *kept,
        {
            "category": "反问",
            "question": f"关于{job}，入职后前三个月团队最看重什么？",
            "why": "把关注点放在岗位预期与考核上，也能了解团队分工。",
            "key_points": ["问清前三个月的目标与考核标准", "问清协作对象和上手路径"],
            "follow_up": "",
        },
    ]


def generate_questions(evidence: dict) -> list[dict]:
    """调模型出题。输出不是 JSON 时自动再采样一次，仍失败才抛给路由。"""
    messages = build_messages(evidence)
    channel = get_channel()
    if channel == CHANNEL_CLOUD and not cloud_configured():
        raise InterviewUnavailable(UNAVAILABLE_CLOUD)

    attempt = 0
    while True:
        attempt += 1
        raw = _call_cloud(messages) if channel == CHANNEL_CLOUD else _call_local(messages)
        try:
            return ensure_category_coverage(parse_questions(raw), evidence["target_job"])
        except InterviewFormatError:
            if attempt >= MODEL_ATTEMPTS:
                raise
            logger.info("面试出题输出不是 JSON，自动重试（第 %d 次）", attempt)


class InterviewRequest(BaseModel):
    plan_id: str = Field(min_length=1, max_length=64)
    resume: ResumeData | None = Field(default=None, description="前端带上的简历草稿，可为空")


@router.post("/interview/questions", summary="按档案与简历生成面试题（不写业务数据）")
def post_interview_questions(
    payload: InterviewRequest,
    current: dict = Depends(get_current_user),
    factory=Depends(create_archive_factory),
) -> dict:
    plan = get_plan(factory, int(current["id"]), payload.plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="这份档案不存在或已经被删除了。")

    evidence = build_evidence(plan, payload.resume.model_dump() if payload.resume else None)
    if not evidence["target_job"]:
        raise HTTPException(status_code=422, detail="这份档案没有目标岗位，先补全档案再出题。")

    try:
        questions = generate_questions(evidence)
    except InterviewUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except InterviewFormatError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"模型这次没有按格式返回题目，请再试一次（{exc}）。",
        ) from exc

    channel = get_channel()
    return {
        "target_job": evidence["target_job"],
        "channel": channel,
        "model": cloud_settings()["model"] if channel == CHANNEL_CLOUD else MODEL_NAME,
        "questions": questions,
        "sources": {
            "resume": evidence["source"] == "resume",
            "has_jd": bool(evidence["jd_text"]),
            "fact_count": len(evidence["facts"]),
            "skill_count": len(evidence["skills"]),
        },
    }
