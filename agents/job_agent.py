"""Job Analysis Agent：把目标岗位拆解成结构化的技能要求。

证据驱动（阶段二）
------------------
除 ``target_job`` 外，本节点还读取 Retrieval Agent 写入的 ``job_evidence`` 与
``retrieval_status``，把原文依据作为**事实来源**注入提示词，并为每条岗位
要求标注来源：

- ``evidence_status``  supported / partially_supported / model_supplement
- ``evidence_refs``    实际支持该要求的证据编号（E01、E02… = job_evidence 的下标）
- ``source_companies`` 证据所属公司（来自证据记录，不来自模型文本）
- ``importance_basis`` profile_skill_importance / model_generated
- ``level_basis``      jd_text / profile_level / model_inferred

分工原则（与 Assessment Agent 一致：模型做语义，代码做判定）
------------------------------------------------------------
- **模型**负责文字生成与语义对应，并可在给定编号范围内提出证据引用；
- **程序**负责一切可判定的结论：编号回查、公司/原文/重要程度的取值、
  等级来源判定、多家公司等级冲突取最严。

三条硬约束
----------
1. 模型提出的编号必须在**本轮 job_evidence** 里能回查到，否则丢弃并告警；
2. 公司、原文、重要程度**只从回查到的证据记录里取**，不采信模型填写的文本；
3. 技能名只是"包含"某个规范名（如"Agent 与工作流编排"含 "Agent"）**不算**直接
   证据 —— 只有归一化后完全同名才算 ``supported``，别名/语义关联一律
   ``partially_supported``。规范名由规则层从 ``SkillVocabulary`` 解析，不问模型。

降级
----
没有粘贴原文、或原文一句都没切出来时，``job_evidence`` 为空 —— 岗位要求照常生成，
但每条都标记为 ``model_supplement`` + ``importance_basis = "model_generated"``，
系统本身可判定"这条没有 JD 依据"，不再只靠终端提示。

用户自带原文（``provenance == "user_pasted"`` 的证据）
-----------------------------------------------------
用户当场粘贴招聘要求时，``retrieval_agent`` 会把原文逐句切成证据送进来（见
``agents/user_jd``）。这些句子与内置资料走**同一套**归属与等级判定，只有两点不同：

- 多一层字面兜底（``_skill_appears_in``）：技能名在原话里逐字出现就判 ``supported``，
  不依赖模型是否老实填 ``evidence_refs``；
- 条数下限放宽到 1：一份广告本来就只写了两条要求是常态，不该为凑数把原文没提的
  能力编进来，也不该因此让整次分析失败。

每条要求都带 ``source_notes``（支撑它的原文句子），前端据此做逐条反显 ——
用户能不能一眼看出"这条是广告里写的"还是"模型自己加的"，全靠这个字段。
"""

import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from agents.external_sources import OFFICIAL_PROVENANCE
from agents.job_catalog import JobCatalog
from agents.job_profile import (
    builtin_profile,
    format_profile_block,
    generate_job_profile,
    validate_profile,
)
from agents.llm import structured_invoke
from agents.skill_dict import SkillVocabulary
from agents.user_jd import USER_JD_PROVENANCE
from agents.utils import (
    SKILL_LEVELS,
    check_non_empty,
    dedupe_by,
    log,
    normalize_level,
    resolve_canonical,
)
from graph.state import CareerState, check_update

# 证据编号：E01 = job_evidence[0]
_REF_PATTERN = re.compile(r"^E\d{2,}$")
_NO_REF_TOKENS = ("", "无", "none", "n/a", "-")

# 与 check_non_empty 的下限保持一致：技能名清理后若不足 3 条，宁可保留原始输出也不让节点崩
MIN_REQUIREMENTS = 3

# 提示词里的「岗位能力分类」清单。它们是**检查覆盖面的标签，不是技能名**，
# 小模型很容易原样抄进 skill 字段（如"Agent 与工作流编排""工程化与部署"），
# 于是既查不到知识库、又和真实技能名重复。
CATEGORY_LABELS = (
    "编程语言",
    "大模型调用与提示词工程",
    "Agent 与工作流编排",
    "RAG 检索增强",
    "后端服务",
    "工程化与部署",
)
CATEGORY_LABEL_SET = frozenset(
    str(label).strip().lower().replace(" ", "") for label in CATEGORY_LABELS
)

# JD 原文里的等级措辞 → 四档。**由强到弱排列，取第一个命中**，
# 这样"熟练掌握"会命中"熟练"(高级)，而不是先命中"掌握"(中级)。
_JD_LEVEL_WORDS = (
    ("精通", "高级"),
    ("熟练", "高级"),
    ("掌握", "中级"),
    ("熟悉", "中级"),
    ("会用", "初级"),
    ("了解", "入门"),
    ("听说", "入门"),
)

PROMPT = """你是一名资深招聘专家，长期负责「{job}」岗位的招聘与用人标准制定。
请分析这个岗位，拆解出它真正要求的核心技能。

【岗位依据材料】
{evidence_block}

【岗位画像（程序整理的岗位工作事实，优先作为拆解依据）】
{profile_block}

【岗位知识库·规范技能表（人工整理）】
{catalog_block}
{basis_note}

【岗位定位】先判断岗位所属领域，再按**该领域的真实用人标准**拆解：
- 如果是"大模型 / AI 应用开发"类岗位：它是把大模型能力落地成产品的**工程岗**，
  不是训练模型的算法岗。此时下列**能力分类**每一类都要至少覆盖一项，缺一不可：
  ① 编程语言            → 具体技能如 Python
  ② 大模型调用与提示词工程 → 具体技能如 LLM API、Prompt Engineering
  ③ Agent 与工作流编排    → 具体技能如 Agent、LangChain、LangGraph、Function Calling
                           （RAG 不能算作这一项，必须单独出现）
  ④ RAG 检索增强         → 具体技能如 RAG、Embedding
  ⑤ 后端服务            → 具体技能如 后端 API 开发、FastAPI
  ⑥ 工程化与部署         → 具体技能如 Docker、Git、Linux
  在此基础上再补充该岗位会考察的其他技能，不要用 PyTorch、深度学习理论顶替上面这些。
- 如果是算法 / 研究类岗位：才以机器学习、深度学习框架、数学基础为核心。
- 如果是其他信息技术岗位（开发 / 测试 / 运维 / 数据 / 安全等）：
  按该岗位真实的技术栈与工程要求拆解，不要套用上面的 AI 分类。
- 如果**不属于**信息技术领域（如医护、金融、法律、教育、传媒、机械等）：
  完全忽略上面的 AI 分类，按该行业的真实用人标准拆解专业知识、实操技能、
  行业工具与软件、资质证书、沟通协作等行业通用能力。

【要求】
1. 给出 6~10 项核心技能，覆盖该岗位最看重的不同能力层面
   （技术岗如：编程语言、框架、工程能力、计算机基础；
   非技术岗如：专业知识、实操技能、行业工具、证书资质、软技能）。
2. **skill 要写"具体技能名"，不要写上面的分类名。**
   ① ~ ⑥ 是帮你检查覆盖面的**分类标签**，不是技能名：
   写"Agent"而不是"Agent 与工作流编排"，写"后端 API 开发"而不是"后端服务"，
   写"Docker"或"Git"而不是"工程化与部署"，写"LLM API"而不是"大模型调用与提示词工程"。
   只有当某个分类名本身就是规范技能名（如 RAG）时才能直接用。
3. 只列这个岗位真正会考察的技能，不要凑数。优先采用上面证据里出现过的技能。
4. **同一个技能只写一次。** 不要用分类标签和具体技能名重复列同一件事
   （例如"大模型调用与提示词工程"和"Prompt Engineering"是同一条，只保留后者）。
5. importance 是 1~5 的整数，5 表示最重要。必须有区分度，
   不要把每项都写成 5，最多 2 项可以是 5。
6. level 是该岗位对这项技能的推荐能力等级（入门/初级/中级/高级）。
7. reason 用一句话说明这项技能在岗位里具体用来做什么。
8. evidence_refs 只能填上面【岗位依据材料】里出现过的编号（如 E01、E02），
   可以填多个；名称和证据里的技能不一样、但语义相关的也可以引用。
   确实找不到能支撑该项的证据时，填 ["无"]。
   **不得编造编号、公司名或原文**；上面没有证据时，所有项都填 ["无"]。
9. 全部用中文，技能名称保留英文原文（如 Python、FastAPI、Docker）。
10. **岗位画像里列出的核心工作任务、工具和交付物，要在技能要求中有对应项。**
    画像里没有的能力不要硬塞（尤其别给非技术岗加编程语言）；画像本身与证据
    冲突时，以用户提供的招聘原文为准。
11. 上面【岗位知识库·规范技能表】是该岗位的人工整理结果：优先从中选择，
    skill 必须与表中名称**逐字一致**；不要用近义词另造一个已在表里的技能。
    表外确实需要的技能可以补充，但不要为了凑数重复表内条目。"""

# 用户自带原文时的附加规矩。放在【岗位依据材料】后面，**覆盖**下面【岗位定位】
# 那套 AI 岗分类清单 —— 用户粘的是新媒体运营时，还硬要凑齐"RAG 检索增强"
# 这类条目，就是拿内置资料的口味冒充人家的岗位要求。
_USER_JD_NOTE = """【本次的规矩】
上面【用户提供的招聘要求原文】就是用户手头这个岗位的**原话**，是本次唯一的事实依据：
- 岗位要求**只能**从这些原话里得出。原文没提的能力一律不要列，哪怕你认为这类岗位
  通常应当要求它（这种"通常应当"请一律不要写进来）。
- 每一项都必须用 evidence_refs 引用支撑它的编号；找不到支撑原话的，就不要列这一项。
- 下面【岗位定位】里那份分类清单和【岗位知识库·规范技能表】**本次都不适用**：
  不能因为表里有某项就把它加进来；原文用词与规范名不同时，保留原文说法。
- 重要性 importance 按原话的措辞强弱判断：写着"必须/精通/熟练"的比"优先/加分/了解"的高。"""


class JobRequirement(BaseModel):
    """岗位对单项技能的要求。"""

    skill: str = Field(description="技能名称")
    importance: int = Field(description="重要程度，1~5 的整数，5 最重要")
    level: Literal["入门", "初级", "中级", "高级"] = Field(description="岗位要求的推荐能力等级")
    reason: str = Field(description="这项技能在岗位中的用途")
    evidence_refs: list[str] = Field(
        default_factory=list,
        description="支撑该项要求的证据编号，如 ['E01','E03']；没有就填 ['无']",
    )

    @field_validator("level", mode="before")
    @classmethod
    def _normalize_level(cls, value):
        return normalize_level(value)

    @field_validator("evidence_refs", mode="before")
    @classmethod
    def _coerce_refs(cls, value):
        """容忍小模型把列表写成字符串或 null。真正的合法性在程序侧回查。"""
        if value is None:
            return []
        if isinstance(value, str):
            text = value.strip()
            return [] if text.lower() in _NO_REF_TOKENS else [text]
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        return []


class JobAnalysis(BaseModel):
    """Job Agent 的结构化输出。"""

    requirements: list[JobRequirement] = Field(description="岗位核心技能要求列表")


# ── 技能词表（惰性，读不到就降级为"不做名称归一"）──────────────────────
_vocabulary: SkillVocabulary | None = None
_vocabulary_unavailable = False


def get_vocabulary() -> SkillVocabulary | None:
    """取技能词表。不可用时返回 None，本节点降级为"只靠模型引用"，不中断流程。"""
    global _vocabulary, _vocabulary_unavailable
    if _vocabulary is None and not _vocabulary_unavailable:
        try:
            _vocabulary = SkillVocabulary()
        except Exception as exc:  # 画像文件缺失 / 损坏
            _vocabulary_unavailable = True
            print(
                f"[Job Agent] 警告：技能词表不可用，跳过技能名归一（{type(exc).__name__}: {exc}）",
                flush=True,
            )
    return _vocabulary


# ── 岗位目录（惰性，读不到就当作没有知识库岗位）────────────────────────
_job_catalog: JobCatalog | None = None
_job_catalog_unavailable = False


def get_job_catalog() -> JobCatalog | None:
    """取岗位目录；不可用时返回 None，本节点退回只按岗位名与模型判断。"""
    global _job_catalog, _job_catalog_unavailable
    if _job_catalog is None and not _job_catalog_unavailable:
        try:
            _job_catalog = JobCatalog()
        except Exception as exc:
            _job_catalog_unavailable = True
            print(
                f"[Job Agent] 警告：岗位目录不可用，跳过知识库技能对齐"
                f"（{type(exc).__name__}: {exc}）",
                flush=True,
            )
    return _job_catalog


# ── 纯函数工具 ──────────────────────────────────────────────────────────
def _normalize_skill_name(name) -> str:
    return str(name or "").strip().lower().replace(" ", "")


def _skill_appears_in(skill_name, sentence: str) -> bool:
    """技能名是否在这句原文里逐字出现（忽略大小写与空格）。

    只认字面出现，不做语义联想 —— 这一层的全部价值就是"程序能自己确认"，
    一旦开始猜，它跟模型引用就没区别了。含 "/" 的名称拆开各算（"LangChain/LangGraph"）。
    """
    name = _normalize_skill_name(skill_name)
    if not name:
        return False
    haystack = _normalize_skill_name(sentence)
    variants = {name} | {
        _normalize_skill_name(part) for part in str(skill_name or "").split("/") if part.strip()
    }
    return any(variant and variant in haystack for variant in variants)


def evidence_ref(index: int) -> str:
    """job_evidence 下标 → 证据编号。"""
    return f"E{index + 1:02d}"


def _jd_level_from_note(note) -> tuple[str, str] | None:
    """从 JD 原文里抽出**明确写出**的等级措辞，返回 (措辞, 等级)；没写就返回 None。

    绝不能回退到 ``normalize_level`` 的默认值（初级）—— 那会把"JD 没写等级"
    伪装成"JD 写了初级"。技能有证据不等于技能等级有证据。
    """
    text = str(note or "")
    for word, level in _JD_LEVEL_WORDS:
        if word in text:
            return word, level
    return None


def _strictest(levels) -> str | None:
    """取最严格的等级（入门 < 初级 < 中级 < 高级）；没有合法值返回 None。"""
    known = [lv for lv in levels if lv in SKILL_LEVELS]
    if not known:
        return None
    return max(known, key=SKILL_LEVELS.index)


def _resolve_canonical(skill_name) -> str | None:
    """把要求名归一到知识库的规范技能名；仅唯一命中时采用。

    唯一性很关键：名字里恰好包含多个技能名时不猜，交给模型引用。
    """
    return resolve_canonical(skill_name, get_vocabulary())


def is_category_label(skill_name) -> bool:
    """判断这个名字是不是提示词里的"岗位能力分类标签"而非具体技能名。"""
    return _normalize_skill_name(skill_name) in CATEGORY_LABEL_SET


def drop_category_labels(requirements: list, dropped: list) -> list:
    """剔除**无法归一**的分类标签。

    分类标签不是技能名：能唯一归一到规范技能的（如"后端服务"→"后端 API 开发"）
    留下，由后面的规范名去重合并；归不了的（如"工程化与部署"，规范技能表里
    没有对应项）说明这句话没有落到任何具体技能上，丢弃并记录，避免它在下游被
    当成一个凭空存在的"技能"。

    另外两道保护：
    - 只对**已经有至少 3 条**的输出生效，清理后不足 3 条就整条回退（见 job_node）；
    - 分类标签之外的技能名一律不动。
    """
    kept = []
    for req in requirements:
        name = req.get("skill")
        if is_category_label(name) and _resolve_canonical(name) is None:
            dropped.append(str(name).strip())
            continue
        kept.append(req)
    return kept


def dedupe_by_canonical(requirements: list, merged: list) -> list:
    """按**规范技能名**去重，消除"分类标签 + 具体技能名"的重复项。

    例如模型同时给出"大模型调用与提示词工程"和"Prompt Engineering"，二者归一后
    是同一个技能，只保留一条 —— 否则下游会把同一个缺口当成两个，出现
    "Prompt Engineering 明显不足"与"大模型调用与提示词工程 已满足"并存的矛盾。

    同名组里**优先保留技能名本身就是规范名的那条**，下游拿到的才是干净名字。
    无法归一的技能按原始名各成一组，不做任何合并。
    """
    groups: dict[str, list] = {}
    order: list[str] = []
    for req in requirements:
        canonical = _resolve_canonical(req.get("skill"))
        key = canonical or f"__raw__{_normalize_skill_name(req.get('skill'))}"
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(req)

    result = []
    for key in order:
        items = groups[key]
        if len(items) > 1:
            merged.append(items[0].get("skill"))
        canonical = _resolve_canonical(items[0].get("skill"))
        if canonical:
            exact = [
                item
                for item in items
                if _normalize_skill_name(item.get("skill")) == _normalize_skill_name(canonical)
            ]
            result.append((exact or items)[0])
        else:
            result.append(items[0])
    return result


def profile_skill_names(profile: dict | None) -> list[str]:
    """岗位 YAML 的规范技能名（保序去重）。"""
    names: list[str] = []
    for item in (profile or {}).get("skills") or []:
        name = str(item.get("skill") or "").strip()
        if name and name not in names:
            names.append(name)
    return names


def format_catalog_skills_block(profile: dict | None) -> str:
    """把岗位知识库的技能表压成提示词里的一段。"""
    if not profile:
        return "（本次未匹配到岗位知识库的规范技能表，请按岗位名称与证据自行判断。）"
    lines: list[str] = []
    for item in profile.get("skills") or []:
        name = str(item.get("skill") or "").strip()
        if not name:
            continue
        meta: list[str] = []
        importance = item.get("importance")
        if isinstance(importance, int):
            meta.append(f"重要度 {importance}")
        level = str(item.get("level") or "").strip()
        if level:
            meta.append(f"建议 {level}")
        reason = str(item.get("reason") or "").strip()
        suffix = f"（{'，'.join(meta)}）" if meta else ""
        lines.append(f"- {name}{suffix}{'：' + reason if reason else ''}")
    return "\n".join(lines) or "（岗位知识库没有可用的规范技能名。）"


def rewrite_to_profile_skills(
    requirements: list, profile: dict | None, rewritten: list | None = None
) -> list:
    """把能唯一归一的模型用词改写成本岗位的规范技能名。

    只在本岗位启用 ``ground_requirements`` 时调用：AI 岗等有招聘原文证据的
    岗位保留模型原词，避免把原文里的具体说法悄悄换成另一个技能名。
    """
    if not profile or not profile.get("ground_requirements"):
        return requirements
    allowed = set(profile_skill_names(profile))
    if not allowed:
        return requirements
    by_name = {
        str(item.get("skill") or "").strip(): item
        for item in profile.get("skills") or []
        if str(item.get("skill") or "").strip()
    }
    result = []
    for req in requirements:
        name = str(req.get("skill") or "").strip()
        canonical = _resolve_canonical(name)
        if canonical in allowed and _normalize_skill_name(canonical) != _normalize_skill_name(name):
            if rewritten is not None:
                rewritten.append(f"{name}→{canonical}")
            source = by_name.get(canonical) or {}
            req = {**req, "skill": canonical, "profile_grounded": True}
            if isinstance(source.get("importance"), int):
                req["importance"] = max(1, min(5, source["importance"]))
            if str(source.get("level") or "") in SKILL_LEVELS:
                req["level"] = source["level"]
        result.append(req)
    return result


def restrict_to_profile_skills(
    requirements: list, profile: dict | None, dropped: list | None = None
) -> list:
    """只保留能落到本岗位规范技能上的要求。

    ``ground_requirements: true`` 的岗位以人工技能表为准。小模型很容易把
    "飞书""抖音后台""Python"这类工具或表外能力塞进来，进而让学习计划去教
    运营岗学 Python —— 这类表外项在这里丢弃，缺的规范技能由 merge 补全。
    """
    if not profile or not profile.get("ground_requirements"):
        return requirements
    allowed = set(profile_skill_names(profile))
    if not allowed:
        return requirements
    result = []
    for req in requirements:
        name = str(req.get("skill") or "").strip()
        canonical = _resolve_canonical(name)
        if name in allowed or canonical in allowed:
            result.append(req)
        elif dropped is not None:
            dropped.append(name)
    return result


def merge_profile_skills(
    requirements: list, profile: dict | None, added: list | None = None
) -> list:
    """把模型漏掉的规范技能按知识库补全。

    只在岗位 YAML 显式声明 ``ground_requirements: true`` 时生效。补进来的条目
    带有知识库的 importance / level / reason，来源由 :func:`apply_profile_basis`
    标为岗位画像，不冒充招聘原文。
    """
    if not profile or not profile.get("ground_requirements"):
        return requirements

    existing: set[str] = set()
    for req in requirements:
        canonical = _resolve_canonical(req.get("skill"))
        existing.add(canonical or _normalize_skill_name(req.get("skill")))

    result = list(requirements)
    for item in profile.get("skills") or []:
        name = str(item.get("skill") or "").strip()
        if not name or name in existing:
            continue
        canonical = _resolve_canonical(name) or name
        if canonical in existing:
            continue
        try:
            importance = int(item.get("importance") or 3)
        except (TypeError, ValueError):
            importance = 3
        level = str(item.get("level") or "初级")
        if level not in SKILL_LEVELS:
            level = "初级"
        result.append({
            "skill": name,
            "importance": max(1, min(5, importance)),
            "level": level,
            "reason": str(item.get("reason") or "岗位知识库中的规范技能要求"),
            "evidence_refs": [],
            "profile_grounded": True,
        })
        existing.add(canonical)
        if added is not None:
            added.append(name)
    return result


def apply_profile_basis(requirements: list) -> list:
    """知识库补全的条目：没有招聘原文证据时，评级来源如实标为岗位画像。"""
    for req in requirements:
        grounded = req.pop("profile_grounded", False)
        if grounded and req.get("evidence_status") == "model_supplement":
            req["importance_basis"] = "profile_skill_importance"
            req["level_basis"] = {"origin": "profile_level", "quote": None, "by_company": {}}
    return requirements


def resolve_requirements_basis(from_user_jd: bool, catalog_profile, requirements: list) -> tuple[str, bool]:
    """判定岗位要求本身有没有可靠依据，以及能否据此宣布目标达成。

    用户原文路径：要求必须全部能在原文里找到；模型补充项由
    ``_attach_evidence`` 标成 ``model_supplement``，有一项就不算全可靠。
    内置岗位画像路径：技能表由知识库限定并补全，可视为可靠。
    两者都没有（如"护士"这类目录外岗位且没粘原文）：只能算模型推断。
    """
    supplement = [
        str(req.get("skill") or "").strip()
        for req in requirements
        if req.get("evidence_status") == "model_supplement"
    ]
    if from_user_jd:
        return "user_pasted", not supplement
    if catalog_profile is not None:
        return "knowledge_base", True
    return "model_only", False


def format_evidence_block(evidence: list, retrieval_status: str | None) -> str:
    """把证据整理成提示词里的一段。编号由程序给出，模型只能引用这些编号。

    三种来源分开呈现，因为它们的可信度与用途完全不同：用户当场粘贴的原文是
    **这个岗位本身的说法**，内置资料摘要是**同类岗位的整理稿**，剩下那档留给
    历史遗留的知识库条目。混在一起，模型分不清哪句能当事实。
    """
    if not evidence:
        return (
            f"（本轮岗位知识库检索状态：{retrieval_status or '未提供'}，没有可用证据。"
            "所有 evidence_refs 一律填 [\"无\"]，岗位要求按你自己的判断拆解。）"
        )

    user_lines: list[str] = []
    official_lines: list[str] = []
    kb_lines: list[str] = []
    for index, item in enumerate(evidence):
        ref = evidence_ref(index)
        provenance = item.get("provenance")
        if provenance == USER_JD_PROVENANCE:
            user_lines.append(f"{ref}｜原文：{item.get('note')}")
        elif provenance == OFFICIAL_PROVENANCE:
            official_lines.append(
                f"{ref}｜技能：{item.get('skill')}"
                f"｜要求性质：{item.get('requirement_type') or '未标注'}"
                f"｜出处：{item.get('source_id')}"
                f"｜资料原句：{item.get('note')}"
            )
        else:
            kb_lines.append(
                f"{ref}｜公司：{item.get('company_slug') or '未标注'}"
                f"｜技能：{item.get('skill')}"
                f"｜要求性质：{item.get('requirement_type')}"
                f"｜章节：{item.get('context')}"
                f"｜原文：{item.get('note')}"
            )

    parts: list[str] = []
    if user_lines:
        parts.append(
            "【用户提供的招聘要求原文】以下每一条都是该岗位招聘广告里的原话，逐字摘自原文。\n"
            "本次分析**只以这些原话为事实依据**：不要引入原文里没提到的技能，"
            "也不要用下面内置知识库的条目顶替。\n" + "\n".join(user_lines)
        )
    if official_lines:
        parts.append(
            "【内置资料摘要｜不是这家公司的原话】以下每条都出自仓库里人工整理的"
            "公开标准或招聘要求整理稿，能回查到同一句话，但它们是**同类岗位的通行说法**。\n"
            "引用编号即可；表述时按「同类岗位通行要求」理解，"
            "不要写成「该招聘岗位明确要求的原文」。\n" + "\n".join(official_lines)
        )
    if kb_lines:
        parts.append(
            "【内置岗位知识库证据】来自同类岗位的历史整理，仅供参考。\n"
            + "\n".join(kb_lines)
        )
    return "\n\n".join(parts)


def _attach_evidence(requirement: dict, evidence: list, rejected: list) -> dict:
    """给一条岗位要求回填来源信息。所有结论由程序算，不采信模型填写的文本。"""
    by_ref = {evidence_ref(i): i for i in range(len(evidence))}

    # 1) 校验模型提出的编号：必须是本轮真实存在的证据编号
    model_indexes: list[int] = []
    for raw in requirement.get("evidence_refs") or []:
        token = str(raw or "").strip()
        if token.lower() in _NO_REF_TOKENS:
            continue
        if not _REF_PATTERN.match(token) or token not in by_ref:
            rejected.append(token)
            continue
        position = by_ref[token]
        if position not in model_indexes:
            model_indexes.append(position)

    # 2) 规则层：技能名归一后按规范名直接取证据，不问模型
    canonical = _resolve_canonical(requirement.get("skill"))
    rule_indexes = (
        [i for i, item in enumerate(evidence) if item.get("skill") == canonical]
        if canonical
        else []
    )

    # 2.5) 用户原文这一路的兜底规则：技能名在这句原话里**逐字出现**就直接归属。
    # 小模型经常不按要求填 evidence_refs，若只信它的引用，一份写得明明白白的原文
    # 会被整体标成"无依据"、匹配度归零 —— 模型偷懒的代价不该由用户承担。
    text_indexes = [
        i
        for i, item in enumerate(evidence)
        if item.get("provenance") == USER_JD_PROVENANCE
        and _skill_appears_in(requirement.get("skill"), str(item.get("note") or ""))
    ]

    # 用户原文这一路**不收模型自报的编号**。实测：3B 模型会把"Python"这种它自己
    # 想到的技能挂到压根没提它的句子上，还给出 E01/E06 这种合法但无关的编号 ——
    # 而"每条要求都能反查到原话"正是这一路存在的理由，宁可少认一条也不能认错。
    from_user_jd = any(item.get("provenance") == USER_JD_PROVENANCE for item in evidence)

    # 规则层能定就完全用规则层的（模型那几条是冗余的）；否则才用模型引用
    if rule_indexes:
        indexes = rule_indexes
        exact_name = _normalize_skill_name(requirement.get("skill")) == _normalize_skill_name(canonical)
        status = "supported" if exact_name else "partially_supported"
    elif text_indexes:
        # 原话里逐字写着这个技能 —— 这是能有的最强依据
        indexes = text_indexes
        status = "supported"
    elif model_indexes and not from_user_jd:
        indexes = model_indexes
        status = "partially_supported"
    else:
        indexes = []
        status = "model_supplement"

    if not indexes:
        return {
            **requirement,
            "evidence_status": "model_supplement",
            "evidence_refs": [],
            "source_companies": [],
            "source_notes": [],
            "importance_basis": "model_generated",
            "level_basis": {"origin": "model_inferred", "quote": None, "by_company": {}},
        }

    picked = [evidence[i] for i in indexes]

    # 3) 等级依据：JD 原文措辞优先 → 画像推荐等级 → 模型推断
    by_company: dict[str, dict] = {}
    for item in picked:
        found = _jd_level_from_note(item.get("note"))
        if not found:
            continue
        word, level = found
        company = item.get("company_slug") or "未标注公司"
        previous = by_company.get(company)
        if previous is None or SKILL_LEVELS.index(level) > SKILL_LEVELS.index(previous["level"]):
            by_company[company] = {"level": level, "quote": word}

    if by_company:
        level = _strictest([entry["level"] for entry in by_company.values()])
        quote = next(entry["quote"] for entry in by_company.values() if entry["level"] == level)
        level_basis = {"origin": "jd_text", "quote": quote, "by_company": by_company}
    else:
        profile_level = _strictest([item.get("level") for item in picked])
        if profile_level:
            level = profile_level
            level_basis = {"origin": "profile_level", "quote": None, "by_company": {}}
        else:
            level = requirement.get("level")
            level_basis = {"origin": "model_inferred", "quote": None, "by_company": {}}

    # 4) 重要程度：沿用画像的技能级数据；没有才用模型值并标记
    profile_importance = [
        item["importance"] for item in picked if isinstance(item.get("importance"), int)
    ]
    if profile_importance:
        importance = max(1, min(5, max(profile_importance)))
        importance_basis = "profile_skill_importance"
    else:
        importance = requirement.get("importance")
        importance_basis = "model_generated"

    return {
        **requirement,
        "importance": importance,
        "level": level,
        "evidence_status": status,
        "evidence_refs": [evidence_ref(i) for i in indexes],
        "source_companies": sorted(
            {item.get("company_slug") for item in picked if item.get("company_slug")}
        ),
        # 支撑本条要求的**原文句子**。前端逐条反显靠它 —— 用户要能一眼看出
        # 这条要求是从广告里哪句话来的，还是模型自己加的。
        "source_notes": [item.get("note") for item in picked if item.get("note")],
        "importance_basis": importance_basis,
        "level_basis": level_basis,
    }


def job_node(state: CareerState) -> dict:
    """LangGraph 节点：读取 target_job + job_evidence，写入 job_requirements。

    输出字段与既有结构兼容：``skill`` / ``importance`` / ``level`` / ``reason``
    语义不变，其余为本次新增的来源标注。
    """
    target_job = (state.get("target_job") or "").strip()
    if not target_job:
        raise ValueError("state 中缺少 target_job，无法分析岗位要求")

    evidence = list(state.get("job_evidence") or [])
    retrieval_status = state.get("retrieval_status")

    # 岗位目录：能认出岗位时，把人工整理的规范技能表注入提示词，并在模型
    # 漏项时补全。认不出（如"护士"）就完全退回原来的模型判断路径。
    catalog = get_job_catalog()
    catalog_profile = None
    if catalog is not None:
        try:
            catalog_profile = catalog.resolve_profile(target_job)
        except Exception as exc:  # noqa: BLE001 - 目录问题只降级
            print(
                f"[Job Agent] 警告：岗位目录查询失败，跳过知识库技能对齐"
                f"（{type(exc).__name__}: {exc}）",
                flush=True,
            )

    # 岗位画像：缓存 → 知识库人工画像 → 模型生成（仅在允许时）。
    # 任何一步失败都只降级，不让整次岗位分析失败 —— 技能要求仍可照常产出。
    profile_warnings = list(state.get("job_profile_warnings") or [])
    cached_profile = state.get("job_profile")
    profile = None
    profile_origin = "unavailable"

    if cached_profile:
        try:
            profile = validate_profile(cached_profile)
            profile_origin = "cache"
        except Exception as exc:  # noqa: BLE001 - 坏缓存只降级，不中断分析
            profile_warnings.append(
                f"注入的岗位画像结构不合法，已忽略（{type(exc).__name__}: {exc}）"
            )

    if profile is None and catalog_profile is not None:
        profile = builtin_profile(catalog_profile, target_job)
        if profile is not None:
            profile_origin = "knowledge_base"
            print(
                f"[Job Agent] 使用岗位知识库中的人工整理画像："
                f"{catalog_profile.get('job_name') or target_job}",
                flush=True,
            )

    # 只有认得出的岗位才允许模型补画像：陌生岗位（如"护士"）没有知识库依据，
    # 生成出来的画像会把猜测包装成事实，所以保持 "unavailable" 让下游如实提示。
    if (
        profile is None
        and catalog_profile is not None
        and state.get("enable_job_profile")
    ):
        try:
            profile, generated_warnings = generate_job_profile(
                target_job, evidence, retrieval_status
            )
            profile_warnings.extend(generated_warnings)
            profile_origin = "generated"
        except Exception as exc:  # noqa: BLE001 - 画像失败不阻断六阶段
            profile_warnings.append(
                f"岗位画像生成失败，本次退回只按岗位名称与证据分析（{exc}）"
            )

    for warning in profile_warnings:
        print(f"[Job Agent] 警告：{warning}", flush=True)

    # 下一轮沿用上一轮快照：岗位要求是同一份 JD 拆出来的，不能因为模型这次
    # 换了个说法就让"同一目标"的门槛悄悄漂移。快照不合法时退回正常生成路径。
    snapshot_requirements = state.get("snapshot_job_requirements")
    if snapshot_requirements:
        reused: list[dict] = []
        for req in snapshot_requirements:
            if not isinstance(req, dict):
                continue
            skill = str(req.get("skill") or "").strip()
            if not skill:
                continue
            item = dict(req)
            item["skill"] = skill
            try:
                item["importance"] = max(1, min(5, int(item.get("importance") or 3)))
            except (TypeError, ValueError):
                item["importance"] = 3
            if item.get("level") not in SKILL_LEVELS:
                item["level"] = "初级"
            reused.append(item)

        if reused:
            reused.sort(key=lambda r: r["importance"], reverse=True)
            evidence = [
                item for item in (state.get("job_evidence") or [])
                if isinstance(item, dict)
            ]
            from_user_jd = any(
                item.get("provenance") == USER_JD_PROVENANCE for item in evidence
            )
            snapshot_basis = str(state.get("snapshot_job_requirements_basis") or "").strip()
            if snapshot_basis in ("user_pasted", "knowledge_base", "model_only"):
                snapshot_grounded = bool(state.get("snapshot_job_requirements_grounded"))
            else:
                snapshot_basis, snapshot_grounded = resolve_requirements_basis(
                    from_user_jd, catalog_profile, reused
                )
            print(
                f"[Job Agent] 本轮沿用上一轮保存的 {len(reused)} 条岗位要求快照，"
                f"未重新调用模型拆解",
                flush=True,
            )
            log("Job Agent")
            return check_update(
                {
                    "job_requirements": reused,
                    "job_profile": profile,
                    "job_profile_warnings": profile_warnings,
                    "job_profile_origin": profile_origin,
                    "job_requirements_basis": snapshot_basis,
                    "job_requirements_grounded": snapshot_grounded,
                },
                "Job Agent",
            )
        profile_warnings.append("上一轮的岗位要求快照不可用，本次重新生成岗位要求")

    # 用户自带原文时，岗位要求只能从原文来；此时一份广告本来就只写了 2 条要求
    # 是可能的，不该为了凑够 3 条把没提的能力编进来，更不该因此让整次分析崩掉。
    from_user_jd = any(item.get("provenance") == USER_JD_PROVENANCE for item in evidence)
    requirement_floor = 1 if from_user_jd else MIN_REQUIREMENTS

    prompt = PROMPT.format(
        job=target_job,
        evidence_block=format_evidence_block(evidence, retrieval_status),
        profile_block=format_profile_block(profile),
        catalog_block=format_catalog_skills_block(catalog_profile),
        basis_note=_USER_JD_NOTE if from_user_jd else "",
    )
    analysis = structured_invoke(prompt, JobAnalysis)

    requirements = []
    for req in analysis.requirements:
        item = req.model_dump()
        # 小模型偶尔会给出越界的 importance，这里兜一下底
        item["importance"] = max(1, min(5, item["importance"]))
        requirements.append(item)

    # 模型经常把同一个技能列两遍（如 Git），先按原样名字去重
    requirements = dedupe_by(requirements, key="skill")

    # 非原文路径下，把模型用的近义词改写成本岗位的规范技能名。用户粘了原文时
    # 不改写：原文怎么说就怎么保留，避免把原话悄悄换成另一个技能名。
    rewritten: list[str] = []
    if not from_user_jd:
        requirements = rewrite_to_profile_skills(requirements, catalog_profile, rewritten)
        if rewritten:
            print(
                f"[Job Agent] 已按岗位知识库对齐技能名：{sorted(set(rewritten))}",
                flush=True,
            )

    # 再把"岗位能力分类标签"和"同一个技能的不同说法"清掉。
    # 两步都可能减少条数，所以先试算，清理后仍够 3 条才采用，否则整条回退 ——
    # 宁可让一条标签漏过去，也不要因此把节点搞崩。
    dropped_labels: list[str] = []
    merged_dupes: list[str] = []
    cleaned = dedupe_by_canonical(drop_category_labels(requirements, dropped_labels), merged_dupes)
    if len(cleaned) >= MIN_REQUIREMENTS:
        requirements = cleaned
        if dropped_labels:
            print(
                f"[Job Agent] 警告：模型把岗位能力分类当成了技能名，已丢弃无法对应具体技能的"
                f"{sorted(set(dropped_labels))}",
                flush=True,
            )
        if merged_dupes:
            print(
                f"[Job Agent] 警告：以下技能名归一后重复，已合并为规范技能名：{sorted(set(merged_dupes))}",
                flush=True,
            )
    else:
        print(
            f"[Job Agent] 警告：技能名清理后只剩 {len(cleaned)} 条，低于下限 {MIN_REQUIREMENTS}，"
            f"已保留模型原始输出（未丢弃分类标签）",
            flush=True,
        )

    # 知识库岗位：表外项丢弃、漏掉的规范技能补全。用户原文路径不动 ——
    # 原文没有的能力不能因为知识库里有就加进来，原文有的也不能被表限制掉。
    added: list[str] = []
    dropped_extras: list[str] = []
    if not from_user_jd:
        requirements = restrict_to_profile_skills(requirements, catalog_profile, dropped_extras)
        if dropped_extras:
            print(
                f"[Job Agent] 已丢弃知识库技能表之外的项：{sorted(set(dropped_extras))}",
                flush=True,
            )
        requirements = merge_profile_skills(requirements, catalog_profile, added)
        if added:
            print(
                f"[Job Agent] 已按岗位知识库补全模型漏项：{added}",
                flush=True,
            )

    check_non_empty(requirements, "Job Agent 的岗位技能列表", minimum=requirement_floor)

    # 回填证据来源（会覆盖 importance / level，所以必须排在排序之前）
    rejected: list[str] = []
    requirements = [_attach_evidence(req, evidence, rejected) for req in requirements]
    requirements = apply_profile_basis(requirements)
    requirements_basis, requirements_grounded = resolve_requirements_basis(
        from_user_jd, catalog_profile, requirements
    )
    if rejected:
        print(
            f"[Job Agent] 警告：模型提出了本轮不存在的证据编号，已丢弃 {sorted(set(rejected))}",
            flush=True,
        )

    # 按重要程度降序，方便后续 Agent 优先处理关键缺口
    requirements.sort(key=lambda r: r["importance"], reverse=True)

    log("Job Agent")
    return check_update(
        {
            "job_requirements": requirements,
            "job_profile": profile,
            "job_profile_warnings": profile_warnings,
            "job_profile_origin": profile_origin,
            "job_requirements_basis": requirements_basis,
            "job_requirements_grounded": requirements_grounded,
        },
        "Job Agent",
    )
