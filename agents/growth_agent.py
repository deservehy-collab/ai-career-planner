"""阶段五：动态能力成长节点（``growth``）。

位置与职责
----------
工作流里排在 ``profile`` 之后、``retrieval`` 之前：能力必须先更新，下游的
job → assessment → learning → project 才会按新能力重算。

本节点**不调模型**：技能名归一、等级判定、证据上限、前置门禁、去重、差异计算
全部复用 growth_rules / growth_store / assessment_agent 的纯函数。

一次运行做六件事
----------------
1. 读历史（``GrowthStore.load()``），缺文件/损坏/不可读/版本不符都如实反映；
2. 把历史已确认的能力与本次 profile 提取结果**合并成基线**（取高者 + 记录分歧）；
3. 用 ``plan_upgrades()`` 判等级（本轮**最多升一档**，只升不降，同轮不叠加）；
4. 把升级写回能力表，产出 ``user_skills_before`` / ``skill_updates`` / ``growth_diff``；
5. 记录本轮历史（``GrowthStore.append()``），损坏文件一律拒绝写入；
6. 尽量做成长前后的缺口对比（数据不齐时如实标为 unavailable，不硬凑）。

结果分类（``growth_diff["outcome"]``）
--------------------------------------
不把不同情况压成同一个"成功"：``store_unreadable`` / ``no_events`` / ``upgraded`` /
``all_duplicates`` / ``blocked_by_prerequisites`` / ``gate_unavailable`` /
``invalid_events`` / ``no_upgrade``。多种情况同时成立时，``outcome`` 取信息量最大的
那个，``signals`` 里列出**全部**成立的标签。
"""

from __future__ import annotations

from agents.assessment_agent import judge_gaps
from agents.growth_rules import (
    apply_upgrades,
    compute_growth_diff,
    gap_change_report,
    plan_upgrades,
)
from agents.growth_store import (
    KB_STATUS_LOAD_FAILED,
    STATUS_EMPTY,
    STATUS_OK,
    WRITE_OK,
    GrowthStore,
    load_skill_base,
)
from agents.skill_dict import SkillVocabulary
from agents.utils import log
from graph.state import CareerState, check_update

# 只有这两种状态下的历史文件是可以安全追加的
_STORE_USABLE = (STATUS_OK, STATUS_EMPTY)

# 结果标签
OUTCOME_STORE_UNREADABLE = "store_unreadable"
OUTCOME_NO_EVENTS = "no_events"
OUTCOME_UPGRADED = "upgraded"
OUTCOME_ALL_DUPLICATES = "all_duplicates"
OUTCOME_BLOCKED = "blocked_by_prerequisites"
OUTCOME_GATE_UNAVAILABLE = "gate_unavailable"
OUTCOME_INVALID_EVENTS = "invalid_events"
OUTCOME_NO_UPGRADE = "no_upgrade"

# 判定优先级：越靠前越"值得先说"
_OUTCOME_PRIORITY = (
    OUTCOME_STORE_UNREADABLE,
    OUTCOME_NO_EVENTS,
    OUTCOME_UPGRADED,
    OUTCOME_ALL_DUPLICATES,
    OUTCOME_BLOCKED,
    OUTCOME_GATE_UNAVAILABLE,
    OUTCOME_INVALID_EVENTS,
    OUTCOME_NO_UPGRADE,
)


def _load_vocabulary() -> SkillVocabulary | None:
    """取技能词表。不可用时返回 None，调用方降级为"不做技能名归一"。"""
    try:
        return SkillVocabulary()
    except Exception as exc:  # noqa: BLE001 - 降级路径
        log("Growth Agent", f"技能词表不可用，跳过技能名归一：{exc}")
        return None


# 可替换的成长记录存储：默认指向项目下的 runs/。
# 和下面 _vocabulary() 的注入方式同款 —— 测试必须能把目录换成临时目录，
# 否则跑一次用例就会在项目里写出 runs/growth.json。
_store: GrowthStore | None = None


def set_store(store: GrowthStore | None) -> None:
    """注入成长记录存储（测试用）。传 None 表示下次调用时重建默认存储。"""
    global _store
    _store = store


def get_store() -> GrowthStore:
    """取成长记录存储；默认是项目下 runs/growth.json 的读写入口。"""
    global _store
    if _store is None:
        _store = GrowthStore()
    return _store


def compare_growth_gaps(
    before_skills: list,
    after_skills: list,
    job_requirements: list,
    analysis,
    vocabulary=None,
) -> dict:
    """成长**前后**的能力缺口对比。纯计算，不调模型。

    口径（三条都来自已确认的设计）：

    1. 两次比较用**同一批岗位要求**和**同一批模型回答**（``analysis`` 原样传两次），
       这样差异只可能来自能力变化，不会混进模型随机性；
    2. 走 ``judge_gaps()`` 的既有接口，不重写判定逻辑；
    3. 对比按**规范技能名**配对（``gap_change_report``），**不使用列表下标 zip**
       —— ``judge_gaps`` 会按"优先级 → 重要度 → 技能名"重排，两侧顺序不同是常态。

    ``analysis`` 缺失（拿不到那批回答）或岗位要求为空时，返回
    ``status="unavailable"`` 并说明原因，**不硬凑**一个看起来像结论的对比。
    """
    requirements = list(job_requirements or [])
    if not requirements:
        return {"status": "unavailable",
                "reason": "本轮状态下没有岗位要求（job 节点尚未运行），无法比较缺口"}
    if analysis is None:
        return {"status": "unavailable",
                "reason": "拿不到 assessment 那次调用的模型回答，无法保证前后用同一批答案；"
                          "不做「只用同名匹配硬凑」的对比，以免被当成真实的缺口变化"}
    if not (before_skills or after_skills):
        return {"status": "unavailable", "reason": "成长前后的能力表都为空"}

    gaps_before = judge_gaps(list(before_skills or []), requirements, analysis)
    gaps_after = judge_gaps(list(after_skills or []), requirements, analysis)
    report = gap_change_report(gaps_before, gaps_after, vocabulary)

    return {
        "status": "computed",
        "reason": None,
        "method": "judge_gaps(同一批岗位要求 + 同一批模型回答) → gap_change_report(按规范技能名配对)",
        "gap_count": {"before": len(gaps_before), "after": len(gaps_after)},
        "changes": report["changes"],
        "newly_satisfied": report["newly_satisfied"],
        "newly_appeared": report["newly_appeared"],
        "resolved_gaps": report["resolved"],
    }


def _events_for_history(plan: dict) -> list[dict]:
    """决定历史里每条事件要不要带指纹。

    **只有真正产生了升级的事件才消耗指纹**。被前置拦下、被上限拦下、或归一失败的事件
    照常记入历史（保留"试过但没成"的记录），但不写 fingerprint ——
    否则它们的指纹会进 ``seen_fingerprints``，下一轮再提交同一条成果就会被当成重复直接
    跳过，**哪怕前置技能已经补齐了也永远升不上去**。
    """
    upgraded_titles = {str(u.get("source") or "") for u in (plan.get("updates") or [])}
    recorded: list[dict] = []
    for event in plan.get("events") or []:
        if event.get("title") in upgraded_titles:
            recorded.append(dict(event))
        else:
            recorded.append({k: v for k, v in event.items() if k != "fingerprint"})
    return recorded


def _classify(events, plan, kb_status, store_status) -> list[str]:
    """列出本轮**全部**成立的结果标签（不只一个）。"""
    signals: list[str] = []
    if store_status not in _STORE_USABLE:
        signals.append(OUTCOME_STORE_UNREADABLE)
    if not events:
        signals.append(OUTCOME_NO_EVENTS)
    if plan["updates"]:
        signals.append(OUTCOME_UPGRADED)
    if plan["duplicates"] and not plan["updates"] and not plan["blocked"] and not plan["skipped"]:
        signals.append(OUTCOME_ALL_DUPLICATES)
    if plan["blocked"]:
        signals.append(OUTCOME_BLOCKED)
    if kb_status == KB_STATUS_LOAD_FAILED:
        signals.append(OUTCOME_GATE_UNAVAILABLE)
    if plan["invalid"]:
        signals.append(OUTCOME_INVALID_EVENTS)
    if not signals:
        signals.append(OUTCOME_NO_UPGRADE)
    return signals


def growth_node(state: CareerState) -> dict:
    """LangGraph 节点：读取 growth_events + user_skills，更新能力表并记录成长差异。

    只写 ``CareerState`` 里已登记的四个字段：``user_skills`` / ``user_skills_before``
    / ``skill_updates`` / ``growth_diff``。

    没有 ``growth_events`` 时**不推断成果、不升级任何等级**；能力表等于"合并历史后的
    基线"—— 历史为空时与 profile 的输出逐字段相同，对旧流程完全兼容。
    """
    profile_skills = list(state.get("user_skills") or [])
    events = list(state.get("growth_events") or [])

    warnings: list[str] = []
    vocabulary = _load_vocabulary()

    # ── 1) 读历史 ───────────────────────────────────────────────────────
    store = get_store()
    loaded = store.load()
    if loaded.warning:
        warnings.append(loaded.warning)
        print(f"[Growth Agent] 警告：{loaded.warning}", flush=True)

    # ── 2) 合并历史与本次 profile（取高者 + 记录分歧）────────────────────
    # 传入 data 用同一份快照，避免同一轮里读两次文件；但**状态与告警以 loaded 为准**
    # （注入 data 时 MergeResult 的状态会被固定成 ok，不适合用来判断文件健康度）。
    merge = store.merge_with_profile(profile_skills, vocabulary, data=loaded.data)

    # ── 3) 知识库状态：原样传给规则模块（绝不把 None + 默认 ok 混过去）──
    kb = load_skill_base()
    if not kb.gate_available:
        warnings.append(kb.warning)
        print(f"[Growth Agent] 警告：{kb.warning}", flush=True)

    # ── 4) 等级判定（全部复用规则模块，本文件不重实现）──────────────────
    plan = plan_upgrades(
        events,
        merge.user_skills,
        vocabulary,
        skill_base=kb.base,
        seen_fingerprints=store.seen_fingerprints(loaded.data),
        knowledge_base_status=kb.status,
    )
    for note in plan["notes"]:
        warnings.append(note)
        print(f"[Growth Agent] 提示：{note}", flush=True)

    # ── 5) 应用升级 ─────────────────────────────────────────────────────
    after_skills = apply_upgrades(merge.user_skills, plan["updates"])

    # ── 6) 差异（缺口部分数据不齐时如实标 unavailable）────────────────
    gap_comparison = compare_growth_gaps(
        merge.user_skills,
        after_skills,
        state.get("job_requirements") or [],
        # growth 节点按设计排在 job / assessment **之前**，拿不到那批模型回答
        analysis=None,
        vocabulary=vocabulary,
    )
    diff = compute_growth_diff(
        merge.user_skills,
        after_skills,
        gaps_before=None,
        gaps_after=None,
        vocabulary=vocabulary,
        updates=plan["updates"],
        blocked=plan["blocked"],
    )
    diff.update({
        "outcome": None,                       # 下面按优先级填
        "signals": [],
        "events_received": len(events),
        "store_status": loaded.status,
        "knowledge_base_status": kb.status,
        "restored_from_history": merge.restored,
        "level_disagreements": merge.disagreements,
        "duplicates": plan["duplicates"],
        "skipped": plan["skipped"],
        "invalid_events": plan["invalid"],
        "unmapped_skills": plan["unmapped"],
        "warnings": warnings,
        "gap_comparison": gap_comparison,
        "gap_changes": gap_comparison.get("changes", []),
        "newly_satisfied": gap_comparison.get("newly_satisfied", []),
        "newly_appeared": gap_comparison.get("newly_appeared", []),
        "resolved_gaps": gap_comparison.get("resolved_gaps", []),
        "history_written": False,
        "history_write_status": None,
    })

    signals = _classify(events, plan, kb.status, loaded.status)
    diff["signals"] = signals
    diff["outcome"] = next(label for label in _OUTCOME_PRIORITY if label in signals)

    # ── 7) 记录历史 ─────────────────────────────────────────────────────
    # 只在"本轮确实处理了候选"时追加：没有任何事件、或事件全是历史重复时，
    # 追加只会让文件无限增长而没有新信息。
    should_append = bool(plan["updates"] or plan["blocked"] or plan["skipped"] or plan["invalid"])
    if should_append:
        written = store.append(
            {
                "events": _events_for_history(plan),
                "user_skills_before": merge.user_skills,
                "user_skills_after": after_skills,
                "level_changes": plan["updates"],
                "blocked_upgrades": plan["blocked"],
                "skipped": plan["skipped"],
                "invalid_events": plan["invalid"],
                "knowledge_base_status": kb.status,
                "target_job": state.get("target_job"),
            },
            baseline_skills=merge.user_skills,
        )
        diff["history_write_status"] = written["status"]
        diff["history_written"] = written["status"] == WRITE_OK
        if written["status"] != WRITE_OK:
            # 存储层拒绝写入（文件损坏/版本不符）—— 必须让上层看见，不能当成成功
            warnings.append(written.get("warning") or "成长历史未能写入")
            print(f"[Growth Agent] 警告：成长历史未写入 —— {written.get('warning')}", flush=True)

    # ── 8) 汇总日志 ─────────────────────────────────────────────────────
    print(
        f"[Growth Agent] 本轮结果：{diff['outcome']}"
        f"（事件 {len(events)}，升级 {len(plan['updates'])}，"
        f"被前置拦截 {len(plan['blocked'])}，重复 {len(plan['duplicates'])}，"
        f"非法 {len(plan['invalid'])}，知识库 {kb.status}）",
        flush=True,
    )
    if merge.disagreements:
        names = "、".join(d["skill"] for d in merge.disagreements)
        print(
            f"[Growth Agent] 提示：{names} 本次 profile 提取低于历史已确认值，已按历史保留"
            f"（只升不降）",
            flush=True,
        )
    for entry in plan["updates"]:
        if entry.get("gate_unavailable"):
            print(
                f"[Growth Agent] 提示：{entry['skill']} 的升级未经过前置门禁"
                f"（{entry.get('gate_reason')}）",
                flush=True,
            )

    log("Growth Agent")
    return check_update(
        {
            "user_skills": after_skills,
            "user_skills_before": merge.user_skills,
            "skill_updates": plan["updates"],
            "growth_diff": diff,
        },
        "Growth Agent",
    )
