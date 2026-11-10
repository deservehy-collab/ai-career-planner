"""内置资料摘要：把 `knowledge/jobs/*.yaml` 里人工登记的 evidence 变成可回查的证据条目。

复用而不是新建
--------------
每份岗位画像的 ``skills[].evidence`` 早就写好了「这条要求出自哪份公开资料、原句是哪句」
（见 knowledge/jobs/accountant.yaml 头部的来源说明），但此前**没有任何运行时代码读它**：
用户没粘招聘原文时，岗位要求直接退化成模型推断。本模块把那批已存在的登记接进
``job_evidence``，让"没粘原文"也有能回查的出处 —— 不必再造一套资料格式。

三条边界
--------
1. **只在用户没粘原文时用**（调用方是 retrieval 节点）：粘了就用这家公司真实写过的话，
   整理稿不配盖过原文。
2. **认不出岗位就一条都不产出**：拿别的岗位的整理稿凑数，等于把猜测包装成出处。
3. **只转格式**：不调模型、不改写文字、不做语义匹配。条目带
   ``provenance = "official_digest"``，下游据此把它和"你粘贴的原文"分开。

刻意不冒充的事
--------------
资料条目里**不写** ``company_slug``：整理稿多是多家合并或公开标准，写不出具体公司；
留空比猜一个诚实。``importance`` / ``level`` 是画像里人工标注的判断，不是招聘广告里的数字，
所以它们只会把要求标成"画像标注"，不会伪装成原文措辞。
"""

from __future__ import annotations

__all__ = [
    "MAX_EVIDENCE_PER_SKILL", "MAX_OFFICIAL_EVIDENCE", "OFFICIAL_PROVENANCE",
    "build_official_evidence", "evidence_inventory",
]

#: 内置资料条目在 job_evidence 里的来源标记。与 ``user_pasted`` 互斥，
#: 前端与 source_attribution 都按它把「你粘的原文」和「仓库里的整理稿」分开。
OFFICIAL_PROVENANCE = "official_digest"

#: 单个技能最多取几条。同一条要求挂十句近义重复的整理稿，只会挤掉别的技能。
MAX_EVIDENCE_PER_SKILL = 2

#: 一轮最多带出多少条资料摘要。它们要整段进提示词，AI 岗画像有 45 条，
#: 全塞进去会把 3B 模型的上下文吃掉一大块；超出部分如实计入 dropped。
MAX_OFFICIAL_EVIDENCE = 24

#: 硬性要求优先于"优先/加分"，同一技能内按这个顺序取舍。
_TYPE_ORDER = ("required", "preferred", "bonus")


def _clamp_importance(value) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return max(1, min(5, number))


def _type_rank(entry: dict) -> int:
    name = str(entry.get("requirement_type") or "").strip().lower()
    return _TYPE_ORDER.index(name) if name in _TYPE_ORDER else len(_TYPE_ORDER)


def _usable_skills(profile: dict) -> list[dict]:
    """画像里的技能表，按人工标注的重要度从高到低排（同分保持 YAML 原序）。

    排序是为了让"上限截断"先砍最不重要的技能，而不是砍 YAML 末尾那个。
    """
    skills = [item for item in ((profile or {}).get("skills") or []) if isinstance(item, dict)]
    return sorted(skills, key=lambda item: -(_clamp_importance(item.get("importance")) or 0))


def _clean_entries(item: dict) -> list[dict]:
    """一个技能的 evidence 列表 → 可用条目（缺 source_id 或原句的直接丢掉）。"""
    result = []
    for entry in item.get("evidence") or []:
        if not isinstance(entry, dict):
            continue
        source_id = str(entry.get("source_id") or "").strip()
        note = str(entry.get("note") or "").strip()
        if not source_id or not note:
            continue
        result.append((source_id, note, entry))
    result.sort(key=lambda triple: _type_rank(triple[2]))
    return result


def build_official_evidence(profile) -> tuple[list[dict], dict]:
    """岗位画像 → ``(证据条目, 统计)``；画像为 None 或没登记 evidence 时返回空表。

    条目字段与 :func:`agents.user_jd.chunk_user_jd` 同构，因此 ``job_agent``
    的编号回查、提示词拼装与匹配度计算都不用改就能复用。
    """
    if not isinstance(profile, dict):
        return [], _stats(None, [], 0, 0, 0)

    skills = _usable_skills(profile)
    records: list[dict] = []
    total = 0
    covered = 0
    dropped = 0

    for item in skills:
        name = str(item.get("skill") or "").strip()
        entries = _clean_entries(item)
        total += len(entries)
        if name and entries:
            covered += 1

        kept = entries[:MAX_EVIDENCE_PER_SKILL]
        dropped += len(entries) - len(kept)

        room = MAX_OFFICIAL_EVIDENCE - len(records)
        if room <= 0:
            dropped += len(kept)
            continue
        if len(kept) > room:
            dropped += len(kept) - room
            kept = kept[:room]

        importance = _clamp_importance(item.get("importance"))
        level = str(item.get("level") or "").strip() or None
        job_id = str(profile.get("job_id") or "").strip()
        for source_id, note, entry in kept:
            records.append({
                "job_id": job_id,
                "source_id": source_id,
                "company_slug": None,
                "skill": name or None,
                "requirement_type": str(entry.get("requirement_type") or "").strip() or None,
                "context": str(entry.get("context") or "").strip() or None,
                "importance": importance,
                "level": level,
                "provenance": OFFICIAL_PROVENANCE,
                "note": note,
            })

    return records, _stats(profile, records, total, covered, dropped)


def evidence_inventory(profile) -> dict:
    """画像里**登记了多少**资料条目（不受上限截断影响），供状态页与校验脚本读数。

    与 :func:`build_official_evidence` 用同一套"可用"判据，两处不会数出两个数。
    """
    entries = [triple for item in _usable_skills(profile) for triple in _clean_entries(item)]
    return {
        "entries": len(entries),
        "source_ids": sorted({triple[0] for triple in entries}),
    }


def _stats(profile, records: list[dict], total: int, covered: int, dropped: int) -> dict:
    """如实说明这批摘要覆盖了画像的多少技能、丢了多少条 —— 不做美化。"""
    return {
        "job_id": str((profile or {}).get("job_id") or "").strip(),
        "job_name": str((profile or {}).get("job_name") or "").strip(),
        "skills_total": len((profile or {}).get("skills") or []),
        "skills_with_evidence": covered,
        "entries_total": total,
        "entries_kept": len(records),
        "entries_dropped": dropped,
        "source_ids": sorted({str(r.get("source_id")) for r in records if r.get("source_id")}),
    }
