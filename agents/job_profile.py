"""岗位画像：把「这个岗位实际在做什么」整理成固定结构，供下游复用。

它解决的不是"再多生成一段文字"，而是三件事：

1. **给全职业一个共同骨架**。现在 ``job_agent`` 只要技能名，项目节点只能靠
   岗位名和技能缺口猜。画像固定输出核心工作任务、工具、交付物、入行门槛与
   成长台阶，护理/法律/机械岗位才有事实可依，不必套用 AI 岗模板。
2. **让来源可回查**。模型只负责写内容并引用证据编号；``jd`` / ``standard`` /
   ``model`` 三档来源**由代码按编号回查后判定**，不采信模型自报。
3. **让同一岗位可复用**。画像按「岗位 + 招聘原文指纹」缓存到当前用户目录，
   第二次分析直接读同一份画像，结果稳定，也少一次模型调用。

边界
----
- 画像本身**不写成长历史、不写 loop.json**；缓存由服务层的保存路径显式调用。
- 生成失败不阻断分析：``job_node`` 退回只按岗位名与证据拆解，并如实带出警告。
- 缓存损坏/不可读/版本不符一律返回状态，不覆盖、不删除原文件。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from agents.llm import active_model_name, structured_invoke
from agents.user_jd import USER_JD_PROVENANCE

__all__ = [
    "SOURCE_JD", "SOURCE_STANDARD", "SOURCE_MODEL",
    "JobProfile", "JobProfileDraft", "ProfileEntry", "ProfileEntryDraft",
    "JobProfileError", "JobProfileStore", "ProfileLoadResult",
    "build_profile", "builtin_profile", "cache_key", "format_profile_block",
    "format_profile_evidence", "generate_job_profile", "jd_fingerprint",
    "validate_profile",
]

# ── 存储 ────────────────────────────────────────────────────────────────
SCHEMA_VERSION = 1
PROFILE_DIRNAME = "job_profiles"

STATUS_OK = "ok"
STATUS_EMPTY = "empty"
STATUS_CORRUPT = "corrupt"
STATUS_UNREADABLE = "unreadable"
STATUS_SCHEMA_MISMATCH = "schema_mismatch"
STATUS_KEY_MISMATCH = "key_mismatch"

WRITE_OK = "written"

# ── 来源 ────────────────────────────────────────────────────────────────
#: 有用户粘贴的招聘原文句子作依据。
SOURCE_JD = "jd"
#: 有内置知识库证据作依据（目前用户原文之外的其他证据都归这一档）。
SOURCE_STANDARD = "standard"
#: 没有任何可回查依据，纯模型推断。
SOURCE_MODEL = "model"

# ── 数量与长度上限（防止一份画像把后续提示词撑爆）──────────────────────
MAX_CORE_TASKS = 8
MAX_TOOLS = 8
MAX_DELIVERABLES = 6
MAX_ENTRY_REQUIREMENTS = 6
MAX_CAREER_STEPS = 5
MAX_ENTRY_NAME_CHARS = 80
MAX_ENTRY_DETAIL_CHARS = 200
MAX_SUMMARY_CHARS = 400
#: 画像注入岗位要求提示词时的字符预算。画像可以存得更全，但进提示词要控量。
PROMPT_BLOCK_MAX_CHARS = 2800

_REF_PATTERN = re.compile(r"^E\d{2,}$")
_NO_REF_TOKENS = ("", "无", "none", "n/a", "-")


class JobProfileError(RuntimeError):
    """画像输出不可用（没有核心工作任务、结构无法解析等）。"""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _one_line(text: Any) -> str:
    return " ".join(str(text or "").split()).strip()


def _norm(text: Any) -> str:
    return _one_line(text).lower().replace(" ", "")


def _truncate(text: Any, limit: int) -> tuple[str, bool]:
    value = _one_line(text)
    if len(value) <= limit:
        return value, False
    return value[:limit], True


def evidence_ref(index: int) -> str:
    """job_evidence 下标 → 证据编号；与 job_agent 的编号口径一致。"""
    return f"E{index + 1:02d}"


# ── 模型输出 / 落盘结构 ─────────────────────────────────────────────────
class ProfileEntryDraft(BaseModel):
    """模型填写的一条画像内容；来源由代码回查证据后判定。"""

    name: str = Field(default="", description="具体工作、工具或交付物的名称")
    detail: str = Field(default="", description="一句话说明它具体指什么")
    evidence_refs: list[str] = Field(
        default_factory=list,
        description="支撑该条目的证据编号，如 ['E01']；没有就填 ['无']",
    )

    @field_validator("evidence_refs", mode="before")
    @classmethod
    def _coerce_refs(cls, value):
        """容忍小模型把列表写成字符串或 null。合法性在代码侧回查。"""
        if value is None:
            return []
        if isinstance(value, str):
            text = value.strip()
            return [] if text.lower() in _NO_REF_TOKENS else [text]
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        return []


class JobProfileDraft(BaseModel):
    """画像生成节点的结构化输出。

    小模型经常把五个列表写成字符串数组（"按医嘱执行给药"），而不是
    ``{name, detail, evidence_refs}`` 对象。这里统一在入口兼容：字符串当成
    只有 name 的条目，键名写成 task/title/description 的也尽量接住。否则一次
    很正常的输出会因结构细节被判成"没有核心工作任务"，整份画像白生成。
    """

    summary: str = Field(default="", description="两句话概括岗位每天在做什么、交付什么")
    core_tasks: list[ProfileEntryDraft] = Field(default_factory=list, description="核心工作任务")
    tools: list[ProfileEntryDraft] = Field(default_factory=list, description="常用工具/方法/设备")
    deliverables: list[ProfileEntryDraft] = Field(default_factory=list, description="岗位交付物")
    entry_requirements: list[ProfileEntryDraft] = Field(default_factory=list, description="入行门槛")
    career_path: list[ProfileEntryDraft] = Field(default_factory=list, description="成长台阶")

    @field_validator(
        "core_tasks", "tools", "deliverables", "entry_requirements", "career_path",
        mode="before",
    )
    @classmethod
    def _coerce_entries(cls, value):
        if value is None:
            return []
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list):
            return []

        entries = []
        for raw in value:
            if isinstance(raw, str):
                name = raw.strip()
                entries.append({"name": name} if name else {})
                continue
            if isinstance(raw, ProfileEntryDraft):
                entries.append(raw.model_dump())
                continue
            if not isinstance(raw, dict):
                continue
            item = dict(raw)
            if not item.get("name"):
                item["name"] = (
                    item.get("task") or item.get("title")
                    or item.get("tool") or item.get("description") or ""
                )
            if not item.get("detail"):
                item["detail"] = item.get("description") or item.get("note") or ""
            if not item.get("evidence_refs"):
                item["evidence_refs"] = item.get("refs") or item.get("evidence") or []
            entries.append(item)
        return entries


class ProfileEntry(BaseModel):
    """落盘/对外的画像条目，带**代码判定**的来源与原文。"""

    name: str
    detail: str = ""
    source: Literal["jd", "standard", "model"] = SOURCE_MODEL
    evidence_refs: list[str] = Field(default_factory=list)
    source_notes: list[str] = Field(default_factory=list)


class JobProfile(BaseModel):
    """落盘/对外的岗位画像。"""

    target_job: str
    summary: str = ""
    core_tasks: list[ProfileEntry] = Field(default_factory=list)
    tools: list[ProfileEntry] = Field(default_factory=list)
    deliverables: list[ProfileEntry] = Field(default_factory=list)
    entry_requirements: list[ProfileEntry] = Field(default_factory=list)
    career_path: list[ProfileEntry] = Field(default_factory=list)


def validate_profile(raw: Any) -> dict:
    """校验一份画像结构；不合法时抛 ``ValidationError``，由调用方决定降级。"""
    if isinstance(raw, JobProfile):
        return raw.model_dump()
    return JobProfile.model_validate(raw).model_dump()


# ── 证据 → 来源 ─────────────────────────────────────────────────────────
def _valid_refs(entry: dict, evidence: list, name: str) -> tuple[list[int], list[str]]:
    """回查模型引用的编号，并补一层用户原文的字面兜底。

    编号只为**本轮真实存在的证据**保留；字面兜底只认用户原文，且只认名称在
    句子里逐字出现的情况 —— 它是"程序能自己确认"的那一档，不做语义联想。
    """
    by_ref = {evidence_ref(i): i for i in range(len(evidence))}
    indexes: list[int] = []
    invalid: list[str] = []

    for raw in entry.get("evidence_refs") or []:
        token = str(raw or "").strip().upper()
        if token.lower() in _NO_REF_TOKENS:
            continue
        if not _REF_PATTERN.match(token) or token not in by_ref:
            invalid.append(token)
            continue
        index = by_ref[token]
        if index not in indexes:
            indexes.append(index)

    needle = _norm(name)
    if len(needle) >= 4:
        for index, item in enumerate(evidence):
            if index in indexes:
                continue
            if item.get("provenance") != USER_JD_PROVENANCE:
                continue
            if needle in _norm(item.get("note")):
                indexes.append(index)
            if len(indexes) >= 3:
                break

    return sorted(indexes), invalid


def _source_for(indexes: list[int], evidence: list) -> str:
    if not indexes:
        return SOURCE_MODEL
    if any(evidence[i].get("provenance") == USER_JD_PROVENANCE for i in indexes):
        return SOURCE_JD
    return SOURCE_STANDARD


def _clean_section(
    raw_items: list,
    *,
    limit: int,
    label: str,
    evidence: list,
    warnings: list[str],
) -> list[dict]:
    """清理一个画像分区：去空、去重、限长、回查来源。"""
    result: list[dict] = []
    seen: set[str] = set()
    duplicates = 0
    invalid: list[str] = []
    truncated = 0
    omitted = 0

    for raw in raw_items or []:
        item = raw.model_dump() if isinstance(raw, BaseModel) else raw
        if not isinstance(item, dict):
            omitted += 1
            continue

        name, name_cut = _truncate(item.get("name"), MAX_ENTRY_NAME_CHARS)
        if not name:
            omitted += 1
            continue
        key = _norm(name)
        if key in seen:
            duplicates += 1
            continue
        seen.add(key)

        if len(result) >= limit:
            omitted += 1
            continue

        detail, detail_cut = _truncate(item.get("detail"), MAX_ENTRY_DETAIL_CHARS)
        indexes, bad_refs = _valid_refs(item, evidence, name)
        invalid.extend(bad_refs)
        truncated += int(name_cut or detail_cut)

        result.append({
            "name": name,
            "detail": detail,
            "source": _source_for(indexes, evidence),
            "evidence_refs": [evidence_ref(i) for i in indexes],
            "source_notes": [
                str(evidence[i].get("note") or "").strip()
                for i in indexes
                if str(evidence[i].get("note") or "").strip()
            ],
        })

    if duplicates:
        warnings.append(f"岗位画像的「{label}」有 {duplicates} 条重复，已合并")
    if omitted:
        warnings.append(f"岗位画像的「{label}」有 {omitted} 条为空或超出上限 {limit}，已忽略")
    if truncated:
        warnings.append(f"岗位画像的「{label}」有 {truncated} 处内容过长，已截断")
    if invalid:
        warnings.append(
            f"岗位画像的「{label}」引用了本轮不存在的证据编号 {sorted(set(invalid))}，已忽略"
        )
    return result


def build_profile(raw: Any, *, target_job: str, evidence: list) -> tuple[dict, list[str]]:
    """把模型输出清理成可落盘的画像。代码在这里决定来源，不问模型。"""
    if isinstance(raw, BaseModel):
        raw = raw.model_dump()
    if not isinstance(raw, dict):
        raise JobProfileError("岗位画像输出不是结构化对象")

    target = _one_line(target_job)
    if not target:
        raise JobProfileError("缺少目标岗位，无法生成岗位画像")

    evidence = list(evidence or [])
    warnings: list[str] = []
    summary, summary_cut = _truncate(raw.get("summary"), MAX_SUMMARY_CHARS)
    if summary_cut:
        warnings.append("岗位画像的 summary 过长，已截断")

    sections = {
        "core_tasks": _clean_section(
            raw.get("core_tasks") or [], limit=MAX_CORE_TASKS, label="核心工作任务",
            evidence=evidence, warnings=warnings,
        ),
        "tools": _clean_section(
            raw.get("tools") or [], limit=MAX_TOOLS, label="常用工具",
            evidence=evidence, warnings=warnings,
        ),
        "deliverables": _clean_section(
            raw.get("deliverables") or [], limit=MAX_DELIVERABLES, label="交付物",
            evidence=evidence, warnings=warnings,
        ),
        "entry_requirements": _clean_section(
            raw.get("entry_requirements") or [], limit=MAX_ENTRY_REQUIREMENTS, label="入行门槛",
            evidence=evidence, warnings=warnings,
        ),
        "career_path": _clean_section(
            raw.get("career_path") or [], limit=MAX_CAREER_STEPS, label="成长台阶",
            evidence=evidence, warnings=warnings,
        ),
    }

    if not sections["core_tasks"]:
        raise JobProfileError("岗位画像没有给出任何核心工作任务")

    profile = JobProfile(target_job=target, summary=summary, **sections).model_dump()
    return profile, warnings


def _catalog_entries(raw_items: list, limit: int) -> list[dict]:
    """把 YAML 里的画像条目转成落盘结构，来源统一标为知识库人工整理。"""
    result: list[dict] = []
    seen: set[str] = set()
    for raw in raw_items or []:
        if isinstance(raw, str):
            name, detail = raw, ""
        elif isinstance(raw, dict):
            name = raw.get("name")
            detail = raw.get("detail") or ""
        else:
            continue
        name, _ = _truncate(name, MAX_ENTRY_NAME_CHARS)
        detail, _ = _truncate(detail, MAX_ENTRY_DETAIL_CHARS)
        if not name:
            continue
        key = _norm(name)
        if key in seen:
            continue
        seen.add(key)
        result.append({
            "name": name,
            "detail": detail,
            "source": SOURCE_STANDARD,
            "evidence_refs": [],
            "source_notes": [],
        })
        if len(result) >= limit:
            break
    return result


def builtin_profile(source: Any, target_job: str) -> dict | None:
    """把岗位 YAML 里的 ``profile`` 段落转成可注入的画像。

    知识库中的人工整理画像不需要调用模型，也不会受小模型输出质量影响；
    结构不完整或没有核心工作任务时返回 ``None``，由调用方退回模型生成。
    """
    if not isinstance(source, dict):
        return None
    section = source.get("profile")
    if not isinstance(section, dict):
        return None

    target = _one_line(target_job) or _one_line(source.get("job_name"))
    if not target:
        return None

    sections = {
        "core_tasks": _catalog_entries(section.get("core_tasks"), MAX_CORE_TASKS),
        "tools": _catalog_entries(section.get("tools"), MAX_TOOLS),
        "deliverables": _catalog_entries(section.get("deliverables"), MAX_DELIVERABLES),
        "entry_requirements": _catalog_entries(
            section.get("entry_requirements"), MAX_ENTRY_REQUIREMENTS
        ),
        "career_path": _catalog_entries(section.get("career_path"), MAX_CAREER_STEPS),
    }
    if not sections["core_tasks"]:
        return None

    try:
        profile = JobProfile(
            target_job=target,
            summary=_truncate(section.get("summary"), MAX_SUMMARY_CHARS)[0],
            **sections,
        )
    except Exception:  # noqa: BLE001 - 坏数据只降级，不让岗位分析失败
        return None
    return profile.model_dump()


# ── 生成 ────────────────────────────────────────────────────────────────
def format_profile_evidence(evidence: list) -> str:
    """把本轮证据整理成画像提示词里的一段；编号由程序给出。"""
    if not evidence:
        return (
            "（本轮没有招聘原文或知识库证据。所有条目都按你的行业知识给出；"
            "evidence_refs 一律填 [\"无\"]，不得编造编号。）"
        )

    lines = []
    for index, item in enumerate(evidence):
        ref = evidence_ref(index)
        note = _one_line(item.get("note"))
        if item.get("provenance") == USER_JD_PROVENANCE:
            lines.append(f"{ref}｜招聘原文：{note}")
        else:
            lines.append(f"{ref}｜知识库参考：{note}")
    return "\n".join(lines)


PROMPT = """你是一名职业分析师，擅长把一个岗位拆解成**实际工作内容**，而不是招聘黑话。

【目标岗位】
{job}

【可回查的依据材料】
{evidence_block}

【任务】
请为这个岗位建立一份画像。先判断它属于哪个职业领域（技术、医护、法律、教育、
金融、机械、传媒……），然后**只按该职业的真实工作方式**来写：

- core_tasks：4~8 条核心工作任务。用动词开头，写"这个人每天/每周实际做什么"。
  例如护理岗可以是"按医嘱执行给药并观察反应"，不是"学习编程"。
- tools：3~8 项工作中实际使用的工具、设备、软件或方法。护理岗是电子病历系统、
  护理记录单、生命体征监测设备；软件岗才是 Python、Docker 等。
- deliverables：2~6 项岗位要交付给患者、客户、团队或系统的成果。
- entry_requirements：2~6 项入行门槛（学历、证书、经验、作品集、身体条件等）。
- career_path：2~5 个从入门到熟练的成长台阶，每个台阶写清"能独立做到什么"。
- summary：两句话概括这个岗位每天在做什么、最终交付什么。

【输出格式（必须严格遵守）】
core_tasks、tools、deliverables、entry_requirements、career_path 这五个列表里的
**每一条都必须是对象**，不能只写字符串。对象的形状固定为：
{{"name": "具体名称", "detail": "一句话说明", "evidence_refs": ["E01"]}}
没有依据时 evidence_refs 写 ["无"]。

【evidence_refs 的规矩】
1. 只能填上面出现过的编号（E01、E02……），不得编造。
2. **用户提供的招聘原文是本次最硬的事实依据**：写了的内容尽量引用支撑它的原文。
   原文没提的能力，不要因为"这类岗位通常要求"就补进来。
3. 没有可引用的依据时填 ["无"]。不要为了显得有依据而硬引用。
4. 不要写来源字段，来源由程序按编号回查后判定。

【不要做的事】
- 不要把岗位名硬套到 AI/互联网模板上；非技术岗不要出现 Python、Docker、RAG
  这类与岗位无关的词。
- 不要写"沟通能力强""学习能力强"这种放之四海而皆准的空话，除非它在原文里
  是明确的硬性要求。
- 不要重复：同一个工作、工具或交付物只出现一次。"""


def generate_job_profile(
    target_job: str,
    evidence: list,
    retrieval_status: str | None = None,
) -> tuple[dict, list[str]]:
    """调用模型生成画像并清理。失败时抛 :class:`JobProfileError`，不写任何文件。"""
    target = _one_line(target_job)
    if not target:
        raise JobProfileError("缺少目标岗位，无法生成岗位画像")

    prompt = PROMPT.format(
        job=target,
        evidence_block=format_profile_evidence(list(evidence or [])),
    )
    try:
        draft = structured_invoke(prompt, JobProfileDraft)
    except Exception as exc:  # noqa: BLE001 - 统一翻成"画像不可用"，由 job_node 降级
        raise JobProfileError(f"岗位画像模型调用失败（{type(exc).__name__}: {exc}）") from exc

    return build_profile(draft, target_job=target, evidence=list(evidence or []))


# ── 注入下游提示词 ──────────────────────────────────────────────────────
_SOURCE_LABELS = {
    SOURCE_JD: "招聘原文",
    SOURCE_STANDARD: "知识库参考",
    SOURCE_MODEL: "模型推断",
}
_SECTION_TITLES = (
    ("core_tasks", "核心工作任务"),
    ("tools", "常用工具与方法"),
    ("deliverables", "交付物"),
    ("entry_requirements", "入行门槛"),
    ("career_path", "成长台阶"),
)


def format_profile_block(profile: Any, max_chars: int = PROMPT_BLOCK_MAX_CHARS) -> str:
    """把画像压成提示词里的一段；无画像时明确说明，不让模型误以为有依据。"""
    if not profile:
        return "（本次没有可用的岗位画像，请按岗位名称与证据自行拆解。）"

    try:
        data = validate_profile(profile)
    except Exception:  # noqa: BLE001 - 注入阶段只降级，不让坏画像拖垮分析
        return "（岗位画像结构不合法，已忽略；请按岗位名称与证据自行拆解。）"

    parts: list[str] = []
    if data.get("summary"):
        parts.append(f"概述：{data['summary']}")
    for key, title in _SECTION_TITLES:
        items = data.get(key) or []
        if not items:
            continue
        lines = [f"【{title}】"]
        for item in items:
            label = _SOURCE_LABELS.get(item.get("source"), "模型推断")
            refs = "、".join(item.get("evidence_refs") or [])
            suffix = f"（依据：{label}{' ' + refs if refs else ''}）"
            detail = f"：{item.get('detail')}" if item.get("detail") else ""
            lines.append(f"- {item.get('name')}{detail}{suffix}")
        parts.append("\n".join(lines))

    block = "\n".join(parts)
    if len(block) > max_chars:
        block = block[:max_chars].rstrip() + "\n（画像过长，已截断）"
    return block


# ── 缓存 ────────────────────────────────────────────────────────────────
def jd_fingerprint(jd_text: Any) -> str:
    """招聘原文指纹：空白归一后哈希，换行/缩进不同不会造成缓存分裂。"""
    return hashlib.sha256(_one_line(jd_text).encode("utf-8")).hexdigest()


def cache_key(target_job: Any, jd_text: Any) -> str:
    """岗位 + 原文指纹 → 缓存键。岗位名大小写/空白不影响命中。"""
    material = f"{_norm(target_job)}\x00{jd_fingerprint(jd_text)}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


@dataclass
class ProfileLoadResult:
    """一次画像读取的结果；读不到时 ``profile`` 为 ``None``，状态说明原因。"""

    profile: dict | None
    status: str
    path: Path
    warning: str | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def found(self) -> bool:
        return self.profile is not None


class JobProfileStore:
    """当前用户目录下的岗位画像缓存（``job_profiles/<key>.json``）。

    目录由调用方注入（HTTP 层是 ``runs/users/user_{id}/``），测试指向临时目录。
    读取**永不抛异常**；写入用临时文件 + ``os.replace`` 原子替换。
    """

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)

    def path_for(self, target_job: Any, jd_text: Any) -> Path:
        return self.directory / PROFILE_DIRNAME / f"{cache_key(target_job, jd_text)}.json"

    def load(self, target_job: Any, jd_text: Any) -> ProfileLoadResult:
        path = self.path_for(target_job, jd_text)
        if not path.exists():
            return ProfileLoadResult(profile=None, status=STATUS_EMPTY, path=path)

        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            return ProfileLoadResult(
                profile=None,
                status=STATUS_UNREADABLE,
                path=path,
                warning=(
                    "岗位画像缓存读取失败"
                    f"（{type(exc).__name__}: {exc}）；本次重新生成，原文件未改动"
                ),
            )

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            return ProfileLoadResult(
                profile=None,
                status=STATUS_CORRUPT,
                path=path,
                warning=(
                    "岗位画像缓存不是合法 JSON"
                    f"（{exc}）；本次重新生成，原文件未覆盖也未删除"
                ),
            )

        if not isinstance(data, dict):
            return ProfileLoadResult(
                profile=None, status=STATUS_CORRUPT, path=path,
                warning="岗位画像缓存结构不合法；本次重新生成，原文件未覆盖也未删除",
            )

        expected_key = cache_key(target_job, jd_text)
        if data.get("schema_version") != SCHEMA_VERSION:
            return ProfileLoadResult(
                profile=None, status=STATUS_SCHEMA_MISMATCH, path=path,
                warning=(
                    f"岗位画像缓存 schema_version={data.get('schema_version')!r} "
                    f"与本程序（{SCHEMA_VERSION}）不一致；本次重新生成，原文件未改动"
                ),
            )
        if (
            data.get("key") != expected_key
            or data.get("jd_fingerprint") != jd_fingerprint(jd_text)
        ):
            return ProfileLoadResult(
                profile=None, status=STATUS_KEY_MISMATCH, path=path,
                warning="岗位画像缓存与本次岗位/原文不匹配；本次重新生成，原文件未改动",
            )

        profile_raw = data.get("profile")
        if not isinstance(profile_raw, dict):
            return ProfileLoadResult(
                profile=None, status=STATUS_CORRUPT, path=path,
                warning="岗位画像缓存缺少 profile 对象；本次重新生成，原文件未覆盖也未删除",
            )

        payload = dict(profile_raw)
        payload.setdefault("target_job", _one_line(target_job))
        try:
            profile = validate_profile(payload)
        except Exception as exc:  # noqa: BLE001 - 损坏缓存按不可用处理
            return ProfileLoadResult(
                profile=None,
                status=STATUS_CORRUPT,
                path=path,
                warning=(
                    "岗位画像缓存内容结构不合法"
                    f"（{type(exc).__name__}: {exc}）；本次重新生成，原文件未覆盖也未删除"
                ),
            )

        if _norm(profile.get("target_job")) != _norm(target_job):
            return ProfileLoadResult(
                profile=None, status=STATUS_KEY_MISMATCH, path=path,
                warning="岗位画像缓存属于另一个岗位；本次重新生成，原文件未改动",
            )

        warnings = data.get("warnings")
        if not isinstance(warnings, list):
            warnings = []
        return ProfileLoadResult(
            profile=profile,
            status=STATUS_OK,
            path=path,
            warnings=[str(item) for item in warnings if str(item).strip()],
        )

    def save(
        self,
        profile: Any,
        *,
        target_job: Any,
        jd_text: Any,
        warnings: list[str] | None = None,
    ) -> dict:
        """原子写入一份画像；结构不合法时抛 ``ValidationError``，不写半截文件。"""
        payload = profile.model_dump() if isinstance(profile, BaseModel) else dict(profile)
        payload["target_job"] = _one_line(target_job)
        data_profile = validate_profile(payload)

        key = cache_key(target_job, jd_text)
        path = self.path_for(target_job, jd_text)
        stamp = _now()
        record = {
            "schema_version": SCHEMA_VERSION,
            "key": key,
            "target_job": _one_line(target_job),
            "jd_fingerprint": jd_fingerprint(jd_text),
            "jd_provided": bool(_one_line(jd_text)),
            "created_at": stamp,
            "updated_at": stamp,
            "model": _active_model_safely(),
            "warnings": [str(item) for item in (warnings or []) if str(item).strip()],
            "profile": data_profile,
        }
        _atomic_write(path, record)
        return {"status": WRITE_OK, "path": str(path), "key": key}

    def delete(self, target_job: Any, jd_text: Any) -> bool:
        """显式删除一份缓存；仅测试/运维使用，业务路径不自动删除。"""
        path = self.path_for(target_job, jd_text)
        try:
            path.unlink()
        except FileNotFoundError:
            return False
        return True


def _active_model_safely() -> str:
    try:
        return str(active_model_name())
    except Exception:  # noqa: BLE001 - 只用于记录生成时的模型名，取不到不影响保存
        return "unknown"


def _atomic_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    handle, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=path.name + ".", suffix=".tmp"
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
