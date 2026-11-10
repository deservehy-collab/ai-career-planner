"""来源归因：把散在 9 套枚举里的「这条岗位要求是从哪来的」收敛成一个口径。

为什么是派生而不是新字段
------------------------
一条 `job_requirements[i]` 已经带着 `evidence_status` / `importance_basis` /
`level_basis.origin` / `evidence_refs` / `source_notes` 五个来源线索，
`job_evidence[j]` 又带着 `provenance` / `source_id`。再往 state 里塞一套
`source_type` 只会多出第四套互相打架的词表，而且 `graph/state.py` 的
`check_update` 白名单会直接拦下新键。

所以这里只做**只读派生**：不写进 CareerState、不写进任何 JSON 存储，
旧的分析结果与新结果用同一个函数解析，也就不触碰任何 `schema_version`。
对外出口是 `build_source_report(state)`，由 `backend/analyze.py` 投影成结果里的
`source_report` —— 前端只读这份结论，不再自己重算一遍真值表。

口径只有一条：**回查不到证据的，一律算模型推断。**
宁可少认一个来源，也不能把模型编的东西标成"有出处"。
"""

from __future__ import annotations

import re

from agents.external_sources import OFFICIAL_PROVENANCE

# ---- 来源口径（唯一对外暴露的四值）-------------------------------------
SOURCE_USER_JD = "user_jd"
SOURCE_OFFICIAL_DATA = "official_data"
SOURCE_KNOWLEDGE_BASE = "knowledge_base"
SOURCE_MODEL = "model_inference"

SOURCE_LABELS = {
    SOURCE_USER_JD: "用户粘贴的招聘原文",
    SOURCE_OFFICIAL_DATA: "公开资料摘要",
    SOURCE_KNOWLEDGE_BASE: "内置岗位画像",
    SOURCE_MODEL: "模型推断",
}

#: 来源硬度排序：原文 > 公开资料 > 人工画像 > 模型。一条要求同时命中多档时取最硬的。
SOURCE_PRIORITY = (
    SOURCE_USER_JD,
    SOURCE_OFFICIAL_DATA,
    SOURCE_KNOWLEDGE_BASE,
    SOURCE_MODEL,
)

#: 外部资料条目的 source_id 前缀（knowledge/jobs/*.yaml 里 evidence.source_id 的
#: 一种命名法，如 source_accounting_std_001）。内置摘要条目现在直接带
#: ``provenance = official_digest``，前缀只作为**未带 provenance 的历史条目**的兜底。
OFFICIAL_SOURCE_ID_PREFIX = "source_"

#: 用户当场粘贴的原文在 job_evidence 里的 provenance 值。这个字同时被
#: job_agent._attach_evidence、jd_match 的分母和 job_profile 的编号校验依赖，
#: 本模块只读不改。
USER_PASTED_PROVENANCE = "user_pasted"

# 画像级依据（人工整理，不是招聘原文）
PROFILE_IMPORTANCE_BASIS = "profile_skill_importance"
PROFILE_LEVEL_ORIGIN = "profile_level"

_REF_PATTERN = re.compile(r"^E(\d{1,2})$")


def ref_of(index: int) -> str:
    """job_evidence 下标 → 证据编号。

    与 `agents/job_agent.evidence_ref` 必须逐字一致；由
    `tests/test_source_attribution.py` 里的一条对账用例锁死，避免两处各自漂移。
    """
    return f"E{index + 1:02d}"


def resolve_refs(refs, evidence) -> list[tuple[str, dict]]:
    """把 `["E01", ...]` 换回证据条目；越界与非法编号直接忽略，不猜。

    返回 `[(编号, 证据条目)]`，保留编号是为了前端要能原样回显芯片。
    """
    evidence = list(evidence or [])
    by_ref = {ref_of(i): i for i in range(len(evidence))}
    resolved: list[tuple[str, dict]] = []
    seen: set[str] = set()
    for raw in refs or []:
        token = str(raw or "").strip().upper()
        index = by_ref.get(token)
        if index is None or token in seen:
            continue
        seen.add(token)
        item = evidence[index]
        if isinstance(item, dict):
            resolved.append((token, item))
    return resolved


def evidence_source(entry: dict) -> str | None:
    """一条证据属于哪个来源；判不出来返回 None（调用方按"无出处"处理）。"""
    if not isinstance(entry, dict):
        return None
    provenance = str(entry.get("provenance") or "")
    if provenance == USER_PASTED_PROVENANCE:
        return SOURCE_USER_JD
    if provenance == OFFICIAL_PROVENANCE:
        return SOURCE_OFFICIAL_DATA
    source_id = str(entry.get("source_id") or "").strip()
    if source_id.startswith(OFFICIAL_SOURCE_ID_PREFIX):
        return SOURCE_OFFICIAL_DATA
    return None


def requirement_source(requirement: dict, evidence=()) -> str:
    """一条岗位要求的来源，四值之一。"""
    if not isinstance(requirement, dict):
        return SOURCE_MODEL

    buckets = {evidence_source(entry) for _, entry in resolve_refs(
        requirement.get("evidence_refs"), evidence)}
    buckets.discard(None)
    for candidate in (SOURCE_USER_JD, SOURCE_OFFICIAL_DATA):
        if candidate in buckets:
            return candidate

    # 没有可回查的证据时，才看画像级依据：它们由代码回填，代表"人工整理的
    # 岗位画像担保过这一条"，但仍然不是某一句招聘原文。
    if str(requirement.get("importance_basis") or "") == PROFILE_IMPORTANCE_BASIS:
        return SOURCE_KNOWLEDGE_BASE
    level_basis = requirement.get("level_basis")
    if isinstance(level_basis, dict) and str(level_basis.get("origin") or "") == PROFILE_LEVEL_ORIGIN:
        return SOURCE_KNOWLEDGE_BASE

    return SOURCE_MODEL


def requirement_citation(requirement: dict, evidence=()) -> dict:
    """给前端用的最小引用视图：来源 + 能不能回查 + 原话。

    只带已存在的事实，不做任何"覆盖率""置信度"一类的加工。
    """
    if not isinstance(requirement, dict):
        requirement = {}
    source = requirement_source(requirement, evidence)
    pairs = resolve_refs(requirement.get("evidence_refs"), evidence)
    quotes = [
        str(entry.get("note") or "").strip()
        for _, entry in pairs
        if str(entry.get("note") or "").strip()
    ]
    # source_notes 是代码回填的原文句子，与 evidence_refs 是两套通道，
    # 都可能在，按内容去重后合并，前端只看到一份"原话"。
    for note in requirement.get("source_notes") or []:
        text = str(note or "").strip()
        if text and text not in quotes:
            quotes.append(text)

    level_basis = requirement.get("level_basis")
    level_origin = ""
    if isinstance(level_basis, dict):
        level_origin = str(level_basis.get("origin") or "")

    return {
        "skill": str(requirement.get("skill") or ""),
        "source_type": source,
        "source_label": SOURCE_LABELS[source],
        "traceable": bool(pairs),
        "evidence_refs": [token for token, _ in pairs],
        "quotes": quotes,
        "evidence_status": str(requirement.get("evidence_status") or ""),
        "importance_basis": str(requirement.get("importance_basis") or ""),
        "level_basis_origin": level_origin,
    }


def group_by_source(requirements, evidence=()) -> dict[str, list[dict]]:
    """按来源分组，用于"本次用了哪些资料"与推断项降权。"""
    grouped: dict[str, list[dict]] = {key: [] for key in SOURCE_PRIORITY}
    for req in requirements or []:
        grouped[requirement_source(req, evidence)].append(req)
    return grouped


def count_by_source(requirements, evidence=()) -> dict[str, int]:
    counts = {key: len(items) for key, items in group_by_source(requirements, evidence).items()}
    counts["total"] = sum(counts[key] for key in SOURCE_PRIORITY)
    return counts


def is_model_inferred(requirement: dict, evidence=()) -> bool:
    """这条要求是否没有任何可回查依据（含画像担保）。"""
    return requirement_source(requirement, evidence) == SOURCE_MODEL


def gap_source_hint(gap: dict) -> str:
    """给提示词里的一条缺口补一句来路；只给可能被当成硬门槛的那两档补，其余不啰嗦。

    用途是**降权不删除**：模型推断出来的要求仍然出现在路线里（它可能是对的），
    内置摘要也是同类岗位的整理稿而不是这家公司的原话，但都得让模型自己知道来路，
    别把它们当成硬门槛去堆料。没有 ``requirement_source`` 的缺口
    （拿不到证据表那几条路径）原样不标。
    """
    source = str(gap.get("requirement_source") or "")
    if source == SOURCE_MODEL:
        return "｜要求来源：模型推断（招聘原文没写这句，可按常识参考，别当硬门槛）"
    if source == SOURCE_OFFICIAL_DATA:
        return "｜要求来源：内置资料摘要（同类岗位的整理稿，不是这家公司的原话）"
    return ""


def evidence_by_source(evidence) -> dict[str, int]:
    """本轮证据按来源数一遍；判不出来源的进 ``unattributed``，不悄悄算进任何一档。"""
    counts = {key: 0 for key in SOURCE_PRIORITY}
    counts["unattributed"] = 0
    for item in evidence or []:
        source = evidence_source(item)
        counts[source if source in counts else "unattributed"] += 1
    return counts


def distinct_official_sources(requirements, evidence=()) -> list[str]:
    """本次真正用到的公开资料 source_id，排序去重，供来源清单直接列给人看。"""
    ids: set[str] = set()
    for req in requirements or []:
        for _, entry in resolve_refs(req.get("evidence_refs"), evidence):
            if evidence_source(entry) == SOURCE_OFFICIAL_DATA:
                ids.add(str(entry.get("source_id") or "").strip())
    return sorted(value for value in ids if value)


def catalog_profile_matched(target_job) -> bool | None:
    """目标岗位在不在内置目录里。目录不可用时返回 None —— 读不出来时不许谎称"不在"。

    这是"陌生岗位提示"的唯一依据：岗位不在目录、又没粘原文时，全部要求都是模型推断，
    前端要把这句话讲清楚。

    复用 ``job_agent.get_job_catalog()`` 的单例，避免每次投影都重读一遍 yaml。
    """
    text = str(target_job or "").strip()
    if not text:
        return False
    from agents.job_agent import get_job_catalog

    catalog = get_job_catalog()
    if catalog is None:
        return None
    return catalog.resolve_profile(text) is not None


def build_source_report(state: dict, requirement_basis: str = "") -> dict:
    """把"这次分析到底用了哪些资料、每句话从哪来"打包给前端。

    只读派生，不写 state、不写盘。放这里算而不是让前端各算各的，是为了
    **真值表只有一份**：前端拿到什么就显示什么，不再自己判断来源。

    `citations` 与 `state["job_requirements"]` **逐条同序**，前端按下标配对即可，
    不要按技能名找（同名条目在脏数据里是可能出现的）。
    """
    requirements = state.get("job_requirements") or []
    evidence = state.get("job_evidence") or []
    counts = count_by_source(requirements, evidence)

    return {
        "target_job": str(state.get("target_job") or "").strip(),
        "requirement_basis": str(requirement_basis or ""),
        "counts": counts,
        "citations": [requirement_citation(req, evidence) for req in requirements],
        "model_only_skills": [
            str(req.get("skill") or "").strip()
            for req in group_by_source(requirements, evidence)[SOURCE_MODEL]
        ],
        "evidence_sentence_count": len(list(evidence)),
        # 原话与整理稿不是一回事：前端要说"你粘的 X 句 / 内置资料 Y 条"，靠这个拆。
        "evidence_by_source": evidence_by_source(evidence),
        "official_source_ids": distinct_official_sources(requirements, evidence),
        "catalog_profile_matched": catalog_profile_matched(state.get("target_job")),
        "job_profile_origin": str(state.get("job_profile_origin") or ""),
    }
