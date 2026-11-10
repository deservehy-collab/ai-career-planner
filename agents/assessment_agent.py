"""Assessment Agent：比对 user_skills 与 job_requirements，产出 skill_gaps。

设计要点（都是为了让 3B 小模型不出错）：
1. 模型只负责"语义对应"——学生会的 LangGraph 算不算岗位要的"Agent 与工作流编排"。
2. 等级、状态、优先级这些算术判断全部由代码算，小模型做序数比较非常不可靠。
3. 学生的水平只能从 Profile Agent 的原数据里查，不允许模型自己评定，
   否则模型会把学生没学过的技能（如 FastAPI）直接判成"已掌握"。
4. supported_by 的取值用动态枚举约束成「学生已有的技能名 + 无」，让模型选不出格。
5. 名字完全一致的直接由代码匹配，不问模型。
6. **"已满足"必须挂直接证据**：等级达标但支撑技能只有自述、或只是跨技能语义
   关联时，状态降为「未验证」/「疑似具备」，不再给出会让用户停止练习的假结论。
"""

from typing import Literal

from pydantic import BaseModel, Field, create_model, field_validator

from agents.llm import structured_invoke
from agents.skill_dict import SkillVocabulary
from agents.source_attribution import requirement_source
from agents.utils import log, normalize_level, resolve_canonical
from graph.state import CareerState, check_update

PROMPT = """你是一名能力评估专家。请判断学生的已有技能能否支撑目标岗位的要求。
技能与岗位要求可能属于任何专业领域（不限于信息技术），按语义对应判断即可。

【学生已掌握技能】
{user_skills}

【目标岗位要求】
{job_requirements}

【任务】对上面【目标岗位要求】里的**每一项**，选出学生已有的**哪一项技能**最能支撑它。

【规则】
1. 必须逐项覆盖，不能遗漏，输出条数要和岗位要求条数完全一致。
2. skill 照抄岗位要求里的技能名称原文，一个字都不要改。
3. supported_by 只能从这几个里选一个：{skill_options}，或者填"无"。
   - 名字完全一样的，直接选它（岗位要 Python，学生有 Python，就选 Python）。
   - 名字不一样的，按语义判断（岗位要"Agent 与工作流编排"，学生有 LangGraph，就选 LangGraph）。
   - 一项要求能被学生多个技能支撑时，选**掌握程度最高**的那个。
   - 只有当学生这些技能确实都跟该要求不沾边时，才填"无"。
4. required_level 照抄岗位要求里给的等级。这是**岗位要求**的水平，不是学生水平。
5. gap 描述**学生缺什么**、接下来要补什么，不要描述学生已经会什么。
6. 支撑技能可以不是要求本身，但必须真的能迁移过去；系统会把"跨技能支撑"
   标成「疑似具备」，不会算「已满足」。不要为了凑结论硬选不相关的技能。
7. 全部用中文。"""

_LEVEL_RANK = {"无": 0, "入门": 1, "初级": 2, "中级": 3, "高级": 4}
_ALREADY_CLAIMS = ("已掌握", "已具备", "已经掌握", "掌握了", "已有")

# 等级判定（代码算）与证据判定（代码算）分开：
# status 只回答"等级够不够"，display_status 回答"能不能当真算已满足"。
SATISFIED_STATUS = "已满足"
PARTIAL_STATUS = "部分满足"
INSUFFICIENT_STATUS = "明显不足"
SUSPECTED_STATUS = "疑似具备"
UNVERIFIED_STATUS = "未验证"

VERIFICATION_DIRECT = "direct"
VERIFICATION_INFERRED = "inferred"
VERIFICATION_UNVERIFIED = "unverified"

# 只有项目经历与成长记录能支撑"已满足"；自述与未标注都只算线索。
_STRONG_EVIDENCE_KINDS = ("project", "growth")


def _norm_name(name) -> str:
    """技能名归一化，用于和岗位要求对号（忽略空格和大小写）。"""
    return str(name or "").strip().lower().replace(" ", "")


# ── 技能词表（惰性；读不到就只按原始名字比对）──────────────────────────
_vocabulary: SkillVocabulary | None = None
_vocabulary_unavailable = False


def get_vocabulary() -> SkillVocabulary | None:
    global _vocabulary, _vocabulary_unavailable
    if _vocabulary is None and not _vocabulary_unavailable:
        try:
            _vocabulary = SkillVocabulary()
        except Exception as exc:  # noqa: BLE001 - 词表问题只降级
            _vocabulary_unavailable = True
            print(
                f"[Assessment Agent] 警告：技能词表不可用，跳过别名归一"
                f"（{type(exc).__name__}: {exc}）",
                flush=True,
            )
    return _vocabulary


def _unique_user_canonicals(user_skills: list) -> dict[str, str]:
    """规范技能名 → 学生技能原名；同一规范名对应多个学生技能时视为歧义，不采用。"""
    vocabulary = get_vocabulary()
    counts: dict[str, int] = {}
    names: dict[str, str] = {}
    for skill in user_skills:
        raw = str(skill.get("skill") or "").strip()
        canonical = resolve_canonical(raw, vocabulary)
        if not canonical:
            continue
        counts[canonical] = counts.get(canonical, 0) + 1
        names.setdefault(canonical, raw)
    return {canonical: name for canonical, name in names.items() if counts[canonical] == 1}


def _judge_status(current_level: str, required_level: str) -> str:
    """已满足 / 部分满足 / 明显不足 —— **只比较等级**，不掺证据判断。"""
    diff = _LEVEL_RANK[required_level] - _LEVEL_RANK[current_level]
    if diff <= 0:
        return SATISFIED_STATUS
    if diff == 1:
        return PARTIAL_STATUS
    return INSUFFICIENT_STATUS


def _judge_verification(status: str, support_kind: str, evidence_kind: str) -> str:
    """证据强度：direct（项目/成长） / inferred（跨技能语义） / unverified（自述或没有）。

    规则只有一条主线：**同一技能 + 项目或成长记录 = 直接证据**。
    跨技能支撑无论对方证据多硬，都只能算"疑似具备"—— 教育心理学学得再好，
    也不能证明会做教学设计。
    """
    if support_kind == "same" and evidence_kind in _STRONG_EVIDENCE_KINDS:
        return VERIFICATION_DIRECT
    if support_kind == "related" and status != INSUFFICIENT_STATUS:
        return VERIFICATION_INFERRED
    return VERIFICATION_UNVERIFIED


def _display_status(status: str, verification: str) -> str:
    """给用户看的状态：等级够了但证据不足时，不再显示"已满足"。"""
    if status != SATISFIED_STATUS:
        return status
    if verification == VERIFICATION_DIRECT:
        return SATISFIED_STATUS
    if verification == VERIFICATION_INFERRED:
        return SUSPECTED_STATUS
    return UNVERIFIED_STATUS


def _judge_priority(status: str, importance: int, display_status: str | None = None) -> str:
    """缺口优先级：高 / 中 / 低。

    等级达标但证据不足的项不能按"已满足"给低优先级 —— 那会让学习路线直接
    跳过它们。重要度 >= 4 时提到"中"，确保进入学习/验证计划。
    """
    shown = display_status or status
    if shown == SATISFIED_STATUS:
        return "低"
    if shown in (SUSPECTED_STATUS, UNVERIFIED_STATUS):
        return "中" if importance >= 4 else "低"
    if status == INSUFFICIENT_STATUS:
        if importance >= 4:
            return "高"
        return "中" if importance == 3 else "低"
    # 部分满足
    return "中" if importance >= 4 else "低"


def _clean_gap_text(text: str, status: str, current_level: str, required_level: str) -> str:
    """校正差距描述与代码算出来的结论的矛盾。

    模型经常写出和数据打架的话，例如：
    - 学生明明是"无"，却写"学生已掌握 FastAPI，需要提升到高级"；
    - 岗位要求"中级"，却写"提升到高级"；
    - 已经"已满足"了，还写"需要提升"。
    这些描述对下游没有价值，一律替换成和数据一致的表述。
    """
    if status == SATISFIED_STATUS:
        return f"已达到岗位要求（{required_level}）"

    text = (text or "").strip()
    if text in ("无", "None", "none", "-", "暂无", "不适用"):
        text = ""  # 模型用"无"表示没什么可说的，按空处理
    fallback = (
        f"当前完全不具备这项能力；岗位要求达到「{required_level}」"
        if current_level == "无"
        else f"当前「{current_level}」，岗位要求「{required_level}」"
    )
    if not text:
        return fallback
    # 学生明明是"无"却写"已掌握"——矛盾
    if current_level == "无" and any(claim in text for claim in _ALREADY_CLAIMS):
        return fallback
    # 描述里提到的等级和岗位要求对不上（例如要求"中级"却写"提升到高级"）——也是矛盾
    mentioned = [level for level in _LEVEL_RANK if level != "无" and level in text]
    if mentioned and required_level not in mentioned:
        return fallback
    return text


def _evidence_gap_text(requirement: str, required_level: str, supported_by: str,
                       display_status: str) -> str:
    """等级够但证据不够时，把"缺什么证据"说清楚，而不是只说"已达到要求"。"""
    if display_status == SUSPECTED_STATUS:
        return (
            f"等级上可由「{supported_by}」迁移，但两者不是同一技能；"
            f"缺少「{requirement}」的直接项目或成长记录，暂按疑似具备处理。"
        )
    if display_status == UNVERIFIED_STATUS:
        return (
            f"等级达到「{required_level}」，但依据只有自述、没有项目或成长记录佐证；"
            "补充一条可验证产出后再算已满足。"
        )
    return ""


def _format_skills(skills: list) -> str:
    if not skills:
        return "（无）"
    return "\n".join(f"- {s.get('skill')}（{s.get('level')}）" for s in skills)


def _format_requirements(requirements: list) -> str:
    if not requirements:
        return "（无）"
    return "\n".join(
        f"- {r.get('skill')}（重要度 {r.get('importance')}，要求 {r.get('level')}）"
        for r in requirements
    )


class GapItem(BaseModel):
    """模型对单项岗位要求的判断。

    注意这里**没有** current_level 字段：学生水平必须由代码从 Profile Agent
    的原始数据里查，不能由模型自己说了算。
    """

    skill: str = Field(description="照抄岗位要求的技能名称")
    required_level: Literal["入门", "初级", "中级", "高级"] = Field(description="照抄岗位要求的等级")
    supported_by: str = Field(description="支撑这项要求的学生技能名，没有就填'无'")
    gap: str = Field(description="一句话说明学生缺什么")

    @field_validator("required_level", mode="before")
    @classmethod
    def _normalize_required(cls, value):
        return normalize_level(value)


def _build_gap_schema(skill_names: list):
    """把 supported_by 收窄成「学生已有技能名 + 无」的枚举。

    枚举会写进 JSON schema 交给 Ollama 约束解码，模型没机会填出格的值。
    """
    options = tuple(dict.fromkeys(skill_names + ["无"]))  # 去重且保序
    item_model = create_model(
        "GapItem",
        __base__=GapItem,
        supported_by=(
            Literal[options],
            Field(description="只能从给定的学生技能名里选，或填'无'"),
        ),
    )
    return create_model("GapAnalysis", gaps=(list[item_model], Field(description="逐项比对结果")))


def judge_gaps(user_skills: list, job_requirements: list, analysis, job_evidence: list | None = None) -> list[dict]:
    """把模型回答整理成逐项的 skill_gaps —— **纯代码判定，不调模型、不做 IO**。

    单独抽出来的原因：阶段五"重新评估"要拿**同一批模型回答**，对更新前后的两套
    user_skills 各算一次缺口，用来对比能力提升的效果。判定逻辑与抽出来之前一字未改。

    参数
    ----
    user_skills : 学生能力表 [{skill, level, evidence, evidence_kind}]，水平与证据只从这里查
    job_requirements : 岗位要求，遍历以它为准，保证不漏项
    analysis : structured_invoke 的原始返回，只用到它的 ``gaps`` 列表
    job_evidence : 岗位要求原文逐句记录，**只用来标来源，不参与任何判定**。
        传 ``None``（成长前后对比那条路径拿不到证据表）时**不写** ``requirement_source``
        —— 拿不到依据就不留一个可能被误读的标记，而不是留一个错的。

    每条结果同时给出两套判定：

    - ``status``：纯等级比较（已满足 / 部分满足 / 明显不足）；
    - ``display_status``：叠加证据后的对外结论。等级已满足但支撑技能只有自述
      或只是跨技能语义关联时，降为「未验证」/「疑似具备」，并相应提高优先级。
      ``verification`` / ``evidence_kind`` / ``evidence_ref`` 记录依据来源。
    """
    # 学生水平一律以 Profile Agent 的原数据为准
    student_level = {_norm_name(s["skill"]): s["level"] for s in user_skills}
    student_entry = {_norm_name(s["skill"]): s for s in user_skills}
    # 名字完全一致的，代码直接认定，不给模型出错的机会
    exact_match = {_norm_name(s["skill"]): s["skill"] for s in user_skills}
    requirement_names = {_norm_name(r["skill"]) for r in job_requirements}
    # 名字不同但能唯一归一到同一个规范技能的（"用友财务软件"↔"财务软件"），
    # 也由代码直接认定；歧义或词表没有的名字不猜。
    vocabulary = get_vocabulary()
    user_canonical = _unique_user_canonicals(user_skills)

    # 整理模型回答，同名只取第一条
    answers = {}
    invented = []
    for item in analysis.gaps:
        key = _norm_name(item.skill)
        if key not in requirement_names:
            invented.append(item.skill)  # 模型造了岗位要求里没有的技能，丢弃
        elif key not in answers:
            answers[key] = item

    # 用【岗位要求】来遍历而不是用模型的输出，保证一一对应：
    # 模型漏答的项也会出现在结果里，不会静默丢失
    gaps = []
    unanswered = []
    for req in job_requirements:
        key = _norm_name(req["skill"])
        item = answers.get(key)
        required_level = req["level"]
        importance = req["importance"]
        req_canonical = resolve_canonical(req["skill"], vocabulary)
        canonical_user = user_canonical.get(req_canonical) if req_canonical else None

        # support_kind：same=同一技能（同名或唯一规范名），related=跨技能语义支撑
        support_kind = "none"
        if key in exact_match:
            # 技能名和岗位要求完全一致，代码直接认定，不依赖模型判断
            supported_by = exact_match[key]
            support_kind = "same"
        elif canonical_user:
            # 名字写法不同，但词表能唯一归一到同一个规范技能
            supported_by = canonical_user
            support_kind = "same"
        elif item is not None:
            supported_by = item.supported_by.strip()
            if supported_by and supported_by != "无":
                support_kind = "related"
        else:
            # 模型漏答，按"未掌握"保守处理——宁可多报缺口，也不要漏报
            unanswered.append(req["skill"])
            supported_by = "无"

        # 指认的技能必须在学生技能表里存在，否则算"无"——防止凭空抬高学生水平
        current_level = student_level.get(_norm_name(supported_by), "无")
        if current_level == "无":
            # 模型可能写的是规范名/别名（如"财务软件"），学生自述是"用友财务软件"。
            # 只有在词表能唯一对上时，才换成学生的原名再查等级。
            model_canonical = resolve_canonical(supported_by, vocabulary)
            student_name = user_canonical.get(model_canonical) if model_canonical else None
            if student_name:
                supported_by = student_name
                current_level = student_level.get(_norm_name(student_name), "无")
                support_kind = "related"
            else:
                supported_by = "无"
                support_kind = "none"

        status = _judge_status(current_level, required_level)

        # 证据只从**支撑技能**的原条目里取，不采信模型对证据的描述
        entry = student_entry.get(_norm_name(supported_by), {}) if support_kind != "none" else {}
        evidence_kind = str(entry.get("evidence_kind") or "").strip()
        if support_kind == "none":
            evidence_kind = "none"
        elif not evidence_kind:
            evidence_kind = "unknown"
        verification = _judge_verification(status, support_kind, evidence_kind)
        display_status = _display_status(status, verification)

        evidence_note = str(entry.get("evidence") or "").strip()
        evidence_ref = str(entry.get("evidence_ref") or "").strip()
        if not evidence_ref and evidence_kind == "project":
            evidence_ref = evidence_note

        evidence_gap = _evidence_gap_text(
            req["skill"], required_level, supported_by, display_status
        )
        if evidence_gap:
            gap_text = evidence_gap
        elif item is not None:
            gap_text = _clean_gap_text(item.gap, status, current_level, required_level)
        elif key in exact_match:
            gap_text = _clean_gap_text("", status, current_level, required_level)
        else:
            gap_text = f"模型未给出判断，按未掌握保守处理；岗位要求达到「{required_level}」"

        gap = {
            "skill": req["skill"],
            "status": status,
            "display_status": display_status,
            "verification": verification,
            "supported_by": supported_by,
            "current_level": current_level,
            "required_level": required_level,
            "importance": importance,
            "priority": _judge_priority(status, importance, display_status),
            "gap": gap_text,
            "evidence_kind": evidence_kind,
            "evidence_ref": evidence_ref,
            "evidence_note": evidence_note,
        }
        if job_evidence is not None:
            # 这条要求本身是从哪来的（原文/公开资料/画像/模型推断）——
            # 只透传，不改变上面任何判定；下游学习与项目规划据此对推断项降权。
            gap["requirement_source"] = requirement_source(req, job_evidence)
        gaps.append(gap)

    if invented:
        print(f"[Assessment Agent] 警告：模型捏造了岗位要求里没有的技能，已丢弃 {invented}", flush=True)
    if unanswered:
        print(f"[Assessment Agent] 警告：模型漏答 {len(unanswered)} 项，已按未掌握补全 {unanswered}", flush=True)

    # 优先级降序 → 岗位重要度降序 → 技能名，保证结果稳定可复现
    priority_rank = {"高": 0, "中": 1, "低": 2}
    gaps.sort(key=lambda g: (priority_rank[g["priority"]], -g["importance"], g["skill"]))
    return gaps


def extract_answers(analysis) -> list[dict]:
    """把模型的**原始回答**转成可序列化的最小结构，只保留三个字段。

    为什么单独抽一个函数、而不塞进 ``judge_gaps``：``growth_agent`` 也在调用
    ``judge_gaps``，改它的签名或返回值会波及那边。这里只在 assessment 节点里
    顺手存一份原始回答，判定链路一行不动。

    口径（三条都重要）：

    1. **在清洗之前取** —— 拿到的是模型原话，不是 ``_clean_gap_text`` 回退后的文本；
    2. **只取 skill / supported_by / gap** —— 其余字段 ``judge_gaps`` 从不读取；
    3. **原样保存，不做任何改写** —— ``supported_by`` 缺失或为空就留 ``None``/``""``，
       **绝不在这里写成"无"**（"无"是 judge_gaps 的判定结果，不是模型的回答）。
    """
    answers: list[dict] = []
    for item in getattr(analysis, "gaps", None) or []:
        answers.append({
            "skill": _raw_field(item, "skill"),
            "supported_by": _raw_field(item, "supported_by"),
            "gap": _raw_field(item, "gap"),
        })
    return answers


def _raw_field(item, name):
    """按原值取字段：缺失或 None 保持 None，其余转成字符串，不做"补默认值"。"""
    value = getattr(item, name, None)
    return None if value is None else str(value)


def _zero_baseline_gaps(job_requirements: list, job_evidence: list | None = None) -> list[dict]:
    """零基础用户的确定性缺口：每一项都按「明显不足」处理，不调模型。

    走的是与 :func:`judge_gaps` 完全相同的字段口径（status / display_status /
    verification / priority / gap 文本），下游学习与项目规划无需区分来源。
    """
    gaps = [{
        "skill": req["skill"],
        "status": INSUFFICIENT_STATUS,
        "display_status": INSUFFICIENT_STATUS,
        "verification": VERIFICATION_UNVERIFIED,
        "supported_by": "无",
        "current_level": "无",
        "required_level": req["level"],
        "importance": req["importance"],
        "priority": _judge_priority(INSUFFICIENT_STATUS, req["importance"]),
        "gap": f"当前完全没有这项能力；岗位要求达到「{req['level']}」",
        "evidence_kind": "none",
        "evidence_ref": "",
        "evidence_note": "",
    } for req in job_requirements]
    if job_evidence is not None:
        for gap, req in zip(gaps, job_requirements):
            gap["requirement_source"] = requirement_source(req, job_evidence)
    priority_rank = {"高": 0, "中": 1, "低": 2}
    gaps.sort(key=lambda g: (priority_rank[g["priority"]], -g["importance"], g["skill"]))
    return gaps


def assessment_node(state: CareerState) -> dict:
    """LangGraph 节点：读取 user_skills + job_requirements，写入 skill_gaps。

    模型只负责"语义对应"（本节唯一的一次 LLM 调用），
    等级 / 状态 / 优先级全部交给 judge_gaps() 里的代码判定。
    """
    user_skills = state.get("user_skills") or []
    job_requirements = state.get("job_requirements") or []
    if not job_requirements:
        raise ValueError("state 中缺少 job_requirements，无法做能力差距分析")
    # 证据表只用于给每条缺口标"这条要求从哪来"，不参与等级/状态判定。
    # 用 is None 判而不是"有没有内容"：空表是"确实一句原文都没有"，与"这一路没带证据"是两回事。
    job_evidence = state.get("job_evidence")
    # 零基础是有意义的状态，不是错误：所有要求都缺，照实列出来即可。
    if not user_skills:
        log("Assessment Agent")
        return check_update(
            {
                "skill_gaps": _zero_baseline_gaps(job_requirements, job_evidence),
                "assessment_answers": [],
            },
            "Assessment Agent",
        )

    skill_names = [s["skill"] for s in user_skills]
    prompt = PROMPT.format(
        user_skills=_format_skills(user_skills),
        job_requirements=_format_requirements(job_requirements),
        skill_options="、".join(skill_names),
    )
    analysis = structured_invoke(prompt, _build_gap_schema(skill_names))

    # 先留一份模型的原始回答（清洗/过滤/回退之前），供阶段五做成长前后对比用；
    # 这份留档不参与任何判定，skill_gaps 的计算结果与以往完全一致。
    answers = extract_answers(analysis)

    gaps = judge_gaps(user_skills, job_requirements, analysis, job_evidence)

    log("Assessment Agent")
    return check_update(
        {"skill_gaps": gaps, "assessment_answers": answers}, "Assessment Agent"
    )
