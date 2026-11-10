"""成长记录的更正：作废一条已入账成果，并重算能力表。

设计约束
--------
1. **不删改原始记录**。作废只追加一条 ``kind="adjustment"`` 的调整记录，
   原条目、原事件、原证据都留在文件里，之后还能看出"哪条被作废、为什么"。
2. **重算不调模型**。从最早一条正常记录的 ``user_skills_before`` 开始，按轮次
   重放所有**未被作废**的事件，复用 ``growth_rules.plan_upgrades`` 与
   ``apply_upgrades`` —— 与 ``growth_node`` 同一套等级、上限、门禁、去重规则。
3. **失败不写盘**。词表/知识库不可用、历史结构无法识别、目标已作废等情况
   一律抛 :class:`GrowthAdjustError`，由服务层转成可读错误；磁盘保持原样。
"""

from __future__ import annotations

from agents.growth_rules import (
    apply_upgrades,
    event_fingerprint,
    level_rank,
    normalize_skills,
    pair_key,
    plan_upgrades,
)
from agents.growth_store import (
    ENTRY_KIND_ADJUSTMENT,
    KB_STATUS_LOAD_FAILED,
    KB_STATUS_OK,
    WRITE_OK,
    GrowthStore,
    load_skill_base,
    voided_index,
)
from agents.skill_dict import SkillVocabulary


class GrowthAdjustError(ValueError):
    """用户输入或历史结构导致无法安全作废；调用方应原样展示原因。"""


def _load_vocabulary() -> SkillVocabulary:
    try:
        return SkillVocabulary()
    except Exception as exc:  # noqa: BLE001 - 归一失败时不能假装技能不存在
        raise GrowthAdjustError(
            f"技能词表加载失败，无法安全重算能力表：{type(exc).__name__}: {exc}"
        ) from exc


def _event_fingerprint(event: dict, vocabulary) -> str | None:
    stored = str(event.get("fingerprint") or "").strip()
    if stored:
        return stored
    raw = event.get("skills")
    if not isinstance(raw, list):
        return None
    canonical, _ = normalize_skills(raw, vocabulary)
    if not canonical:
        return None
    return event_fingerprint(event.get("type"), event.get("title"), canonical)


def _is_adjustment(entry) -> bool:
    return isinstance(entry, dict) and entry.get("kind") == ENTRY_KIND_ADJUSTMENT


def _first_active_index(history: list) -> int | None:
    for index, entry in enumerate(history):
        if not _is_adjustment(entry):
            return index
    return None


def _skills_by_key(skills, vocabulary) -> dict[str, dict]:
    indexed: dict[str, dict] = {}
    for item in skills or []:
        if not isinstance(item, dict):
            continue
        key = pair_key(item.get("skill"), vocabulary)
        if not key:
            continue
        current = indexed.get(key)
        if current is None or level_rank(item.get("level")) > level_rank(current.get("level")):
            indexed[key] = dict(item)
    return indexed


def _external_delta(previous_after, current_before, vocabulary) -> list[dict]:
    """轮次之间由画像更新带来、但没有形成成长事件的能力变化。

    只取"当前 before 比上一轮 after 更高或新增"的项。作废事件造成的升级
    已经同时存在于上一轮 after 与当前 before 里，不会被当成外部变化复活。
    """
    base = _skills_by_key(previous_after, vocabulary)
    target = _skills_by_key(current_before, vocabulary)
    delta: list[dict] = []
    for key, item in target.items():
        if level_rank(item.get("level")) > level_rank((base.get(key) or {}).get("level")):
            delta.append(item)
    return delta


def _merge_higher(state: list[dict], extra: list[dict], vocabulary) -> list[dict]:
    merged = {key: dict(item) for key, item in _skills_by_key(state, vocabulary).items()}
    for item in extra:
        key = pair_key(item.get("skill"), vocabulary)
        if not key:
            continue
        current = merged.get(key)
        if current is None:
            merged[key] = dict(item)
        elif level_rank(item.get("level")) > level_rank(current.get("level")):
            current["level"] = item.get("level")
            if item.get("evidence"):
                current["evidence"] = item.get("evidence")
    return list(merged.values())


def _survivors(entry: dict, entry_index: int, voided_refs) -> list[dict]:
    events = entry.get("events") if isinstance(entry.get("events"), list) else []
    return [
        event for index, event in enumerate(events)
        if (entry_index, index) not in voided_refs
    ]


def _replay(history: list, voided_refs, vocabulary, skill_base, fallback_baseline) -> tuple[list[dict], dict]:
    start = _first_active_index(history)
    if start is None:
        raise GrowthAdjustError("成长历史里没有可重算的正常记录，无法作废")

    first = history[start]
    raw_before = first.get("user_skills_before")
    if not isinstance(raw_before, list):
        raw_before = fallback_baseline or []
    state = [dict(item) for item in raw_before if isinstance(item, dict)]
    # 被作废的事件指纹仍然算"已处理过"：否则下一轮会自动把同一条成果重新入账，
    # 作废就等于没做。要重新提交，应当换一条成果（标题/技能不同）。
    _, voided_fingerprints = voided_index(history)
    seen = set(voided_fingerprints)

    previous_after = first.get("user_skills_after") or []
    events_replayed = 0
    entries_replayed = 0

    for index in range(start, len(history)):
        entry = history[index]
        if _is_adjustment(entry):
            continue
        if not isinstance(entry.get("user_skills_before"), list) or not isinstance(
            entry.get("user_skills_after"), list
        ):
            raise GrowthAdjustError(
                f"第 {index + 1} 条记录缺少能力表快照，无法安全重算"
            )

        if index > start:
            state = _merge_higher(
                state,
                _external_delta(previous_after, entry.get("user_skills_before"), vocabulary),
                vocabulary,
            )

        kb_status = entry.get("knowledge_base_status") or (
            KB_STATUS_OK if skill_base is not None else KB_STATUS_LOAD_FAILED
        )
        if kb_status not in (KB_STATUS_OK, KB_STATUS_LOAD_FAILED):
            raise GrowthAdjustError(
                f"第 {index + 1} 条记录的知识库状态无法识别（{kb_status!r}），"
                f"无法安全重算"
            )

        survivors = _survivors(entry, index, voided_refs)
        if survivors:
            plan = plan_upgrades(
                survivors,
                state,
                vocabulary,
                skill_base=skill_base,
                seen_fingerprints=seen,
                knowledge_base_status=kb_status,
            )
            state = apply_upgrades(state, plan["updates"])
            upgraded_titles = {str(item.get("source") or "") for item in plan["updates"]}
            for event in plan["events"]:
                if str(event.get("title") or "") not in upgraded_titles:
                    continue
                fingerprint = _event_fingerprint(event, vocabulary)
                if fingerprint:
                    seen.add(fingerprint)
            events_replayed += len(survivors)

        entries_replayed += 1
        previous_after = entry.get("user_skills_after") or []

    return state, {
        "entries_replayed": entries_replayed,
        "events_replayed": events_replayed,
    }


def _level_changes(old_skills, new_skills, vocabulary) -> list[dict]:
    old = _skills_by_key(old_skills, vocabulary)
    new = _skills_by_key(new_skills, vocabulary)
    changes: list[dict] = []
    for key in sorted(set(old) | set(new)):
        before = old.get(key) or {}
        after = new.get(key) or {}
        before_level = before.get("level")
        after_level = after.get("level")
        if level_rank(before_level) == level_rank(after_level):
            continue
        changes.append({
            "skill": after.get("skill") or before.get("skill") or key,
            "from": before_level,
            "to": after_level,
            "reason": "作废记录后按剩余成果重算",
            "source": "作废调整",
        })
    return changes


def void_event(
    store: GrowthStore,
    entry_index: int,
    event_index: int,
    reason: str,
    now=None,
) -> dict:
    """作废一条已入账成果。返回可 JSON 序列化的结果；失败抛 GrowthAdjustError。"""
    if isinstance(entry_index, bool) or not isinstance(entry_index, int) or entry_index < 0:
        raise GrowthAdjustError("entry_index 必须是非负整数")
    if isinstance(event_index, bool) or not isinstance(event_index, int) or event_index < 0:
        raise GrowthAdjustError("event_index 必须是非负整数")
    reason = str(reason or "").strip()
    if not reason:
        raise GrowthAdjustError("请填写作废原因")

    loaded = store.load()
    if not loaded.writable:
        raise GrowthAdjustError(loaded.warning or "成长记录当前不可安全写入")

    history = loaded.data.get("history") or []
    if entry_index >= len(history):
        raise GrowthAdjustError(f"成长历史里没有第 {entry_index + 1} 条记录")
    entry = history[entry_index]
    if _is_adjustment(entry):
        raise GrowthAdjustError("调整记录本身不能再作废")
    events = entry.get("events") if isinstance(entry.get("events"), list) else []
    if event_index >= len(events):
        raise GrowthAdjustError(f"第 {entry_index + 1} 条记录里没有第 {event_index + 1} 条成果")
    event = events[event_index]
    if not isinstance(event, dict):
        raise GrowthAdjustError("这条成果的结构不合法，无法作废")

    voided_refs, _ = voided_index(history)
    if (entry_index, event_index) in voided_refs:
        raise GrowthAdjustError("这条成果已经作废过，不能重复作废")

    vocabulary = _load_vocabulary()
    kb = load_skill_base()
    stored_statuses = [
        item.get("knowledge_base_status")
        for item in history
        if not _is_adjustment(item)
    ]
    if not kb.gate_available and any(
        status != KB_STATUS_LOAD_FAILED for status in stored_statuses
    ):
        raise GrowthAdjustError(
            f"技能知识库当前不可用（{kb.warning or kb.status}），无法保证重算结果与原记录一致；"
            f"本次没有改动任何记录"
        )

    state, replay = _replay(
        history,
        voided_refs | {(entry_index, event_index)},
        vocabulary,
        kb.base if kb.gate_available else None,
        (loaded.data.get("baseline") or {}).get("user_skills") or [],
    )
    old_latest = store.latest_skills(loaded.data)
    changes = _level_changes(old_latest, state, vocabulary)
    fingerprint = _event_fingerprint(event, vocabulary)

    adjustment = {
        "kind": ENTRY_KIND_ADJUSTMENT,
        "action": "void",
        "voided": [{
            "entry_index": entry_index,
            "event_index": event_index,
            "fingerprint": fingerprint,
            "type": event.get("type"),
            "title": event.get("title"),
            "reason": reason,
        }],
        "reason": reason,
        "user_skills_before": [dict(item) for item in old_latest if isinstance(item, dict)],
        "user_skills_after": state,
        "level_changes": changes,
        "events": [],
        "blocked_upgrades": [],
        "skipped": [],
        "invalid_events": [],
        "knowledge_base_status": kb.status,
        "target_job": entry.get("target_job"),
        "recomputed": replay,
    }

    written = store.append(adjustment, now=now)
    if written.get("status") != WRITE_OK:
        raise GrowthAdjustError(written.get("warning") or "作废记录未能写入")

    return {
        "voided": True,
        "entry_index": entry_index,
        "event_index": event_index,
        "fingerprint": fingerprint,
        "title": event.get("title"),
        "reason": reason,
        "level_changes": changes,
        "latest_skills": state,
        "history_length": written.get("history_length"),
        "recomputed": replay,
    }
