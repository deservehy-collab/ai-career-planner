"""Retrieval Agent：把用户粘贴的招聘要求原文整理成逐句依据。

定位
----
本节点只负责"取事实"，不做任何判定，也不写 ``user_skills``：

- 输入：state 里的 ``jd_text``（用户当场复制的招聘要求原文）；
- 输出：``job_evidence``（一句一条的依据记录）、``retrieval_status``、
  ``retrieval_diagnostics``。

切句是纯文本操作，**不连接任何检索后端**：Ollama 只负责后面那几次推理，
这一步的等待时间为零。

没有原文时先看内置资料，最后才如实交出空证据
--------------------------------------------
``jd_text`` 为空时，本节点按顺序退让：

1. 目标岗位在内置岗位目录里、且画像登记了资料出处 → 交出那些**可回查的整理稿条目**
   （``provenance = "official_digest"``），状态是 ``official_digest``；
2. 认不出岗位（如"护士"）或画像没登记 evidence → 返回空 ``job_evidence`` 并写清原因，
   **不**假装"有依据"。下游 job 节点会把每条要求如实标成 ``model_supplement``，
   前端整栏标灰、不给匹配度百分比。

原文里一句可用的都没切出来（比如只粘了个标题）时同样交出空证据，但原因写的是
``user_jd_empty`` —— "没粘"和"粘了却没内容"是两件事，得分开说。

宁可明说"这是同类岗位的整理稿"或"这是模型推断"，也不拿含糊的措辞冒充原话。

证据 ≠ 学生能力
---------------
``job_evidence`` 描述的是**招聘方要什么**，``user_skills`` 描述的是
**学生已经掌握什么**，两者性质不同，不得互相写入 —— 尤其不能因为
岗位要求里出现某技能，就把它当成学生已具备的技能。
"""

from __future__ import annotations

from agents.external_sources import OFFICIAL_PROVENANCE, build_official_evidence
from agents.user_jd import USER_JD_PROVENANCE, USER_JD_SOURCE_ID, chunk_user_jd
from agents.utils import log
from graph.state import CareerState, check_update

#: 依据来自用户当场粘贴的原文（最硬的一档：逐条能反查到原话）。
STATUS_USER_PASTED = "user_pasted"

#: 本次没有原文，改用内置岗位画像登记的公开资料整理稿 —— 能回查同一句话，
#: 但它是**同类岗位的整理稿**，不代表这家公司真实写过这些要求。
STATUS_OFFICIAL_DIGEST = "official_digest"

#: 本次没有任何可用依据 —— 岗位要求将全部由模型推断。
STATUS_NO_SOURCE = "no_source"

_SOURCE_NOTE = "招聘方要求的原文，一句一条"

#: 三档状态在 diagnostics.coverage 里的键，前端据此说话，不自己猜。
_EMPTY_COVERAGE = {"user_jd": 0, "official": 0, "source_ids": []}


def _coverage_of(records: list[dict]) -> dict:
    """按 provenance 数一遍本轮证据 —— 覆盖数从记录本身算，不靠调用方报数。"""
    user = sum(1 for item in records if item.get("provenance") == USER_JD_PROVENANCE)
    official = sum(1 for item in records if item.get("provenance") == OFFICIAL_PROVENANCE)
    source_ids = sorted(
        {
            str(item.get("source_id") or "").strip()
            for item in records
            if str(item.get("source_id") or "").strip() not in ("", USER_JD_SOURCE_ID)
        }
    )
    return {"user_jd": user, "official": official, "source_ids": source_ids}


def _status_of(records: list[dict]) -> str:
    """旧快照没存状态时，按证据本身推断状态，而不是默认"用户粘过原文"。

    有原文就算原文；否则只要还有可回查的记录，就按内置资料对待；全没有才算没依据。
    """
    if not records:
        return STATUS_NO_SOURCE
    if any(item.get("provenance") == USER_JD_PROVENANCE for item in records):
        return STATUS_USER_PASTED
    return STATUS_OFFICIAL_DIGEST


def _empty_update(reason: str, note: str) -> dict:
    """交出空证据 + 如实说明原因，让下游把结论标成"模型推断"。"""
    print(f"[Retrieval Agent] 警告：{note}，岗位要求将退化为模型推断（已如实标注）", flush=True)
    return check_update(
        {
            "job_evidence": [],
            "retrieval_status": STATUS_NO_SOURCE,
            "retrieval_diagnostics": {
                "reason": reason,
                "source_id": USER_JD_SOURCE_ID,
                "note": note,
                "coverage": dict(_EMPTY_COVERAGE),
            },
        },
        "Retrieval Agent",
    )


def _resolve_profile(target_job: str) -> dict | None:
    """查内置岗位目录；读不出来或认不出都返回 None（宁可不给资料，也不猜岗位）。

    目录单例在 ``agents.job_agent``，延迟导入避免与本节点的导入顺序形成环。
    """
    from agents.job_agent import get_job_catalog

    catalog = get_job_catalog()
    if catalog is None:
        return None
    try:
        return catalog.resolve_profile(target_job)
    except Exception as exc:  # noqa: BLE001 - 目录问题只降级，不拖垮分析
        print(f"[Retrieval Agent] 警告：岗位目录查询失败（{type(exc).__name__}: {exc}）", flush=True)
        return None


def _digest_update(target_job: str) -> dict | None:
    """没粘原文时的退让：改用内置画像登记的资料摘要；给不出就返回 None。"""
    profile = _resolve_profile(target_job)
    if profile is None:
        return None
    records, stats = build_official_evidence(profile)
    if not records:
        return _empty_update(
            "profile_without_evidence",
            f"内置岗位画像「{stats['job_name'] or target_job}」没有登记可回查的资料出处",
        )

    print(
        f"[Retrieval Agent] 未粘贴招聘原文，改用内置资料摘要 {len(records)} 条"
        f"（岗位画像：{stats['job_name']}，覆盖 {stats['skills_with_evidence']}/"
        f"{stats['skills_total']} 项技能）",
        flush=True,
    )
    return check_update(
        {
            "job_evidence": records,
            "retrieval_status": STATUS_OFFICIAL_DIGEST,
            "retrieval_diagnostics": {
                "job_id": stats["job_id"],
                "source_id": stats["job_id"],
                "match_kind": "official_digest",
                "scope_count": len(records),
                "dropped_entries": stats["entries_dropped"],
                "reason": (
                    f"本次没有粘贴招聘要求原文，改用内置岗位画像登记的资料摘要"
                    f"（岗位：{stats['job_name']}）"
                ),
                "note": "同类岗位公开资料的整理稿，一句一条，可回查但不是这家公司的原话",
                "coverage": {
                    **_coverage_of(records),
                    "skills_covered": stats["skills_with_evidence"],
                    "skills_total": stats["skills_total"],
                },
            },
        },
        "Retrieval Agent",
    )


def retrieval_node(state: CareerState) -> dict:
    """LangGraph 节点：把粘贴的招聘要求原文切成逐句依据写进 state。"""
    target_job = (state.get("target_job") or "").strip()
    if not target_job:
        raise ValueError("state 中缺少 target_job，无法确认这份原文对应哪个岗位")

    # 下一轮沿用上一轮快照：原文在保存那一轮时就已切好，这里原样交回。
    # 重新切一遍不会更准，反而可能因为前端没再传原文而把依据清空。
    if "snapshot_job_evidence" in state:
        evidence = [
            dict(item)
            for item in (state.get("snapshot_job_evidence") or [])
            if isinstance(item, dict)
        ]
        status = str(state.get("snapshot_retrieval_status") or _status_of(evidence))
        diagnostics = dict(state.get("snapshot_retrieval_diagnostics") or {})
        diagnostics["reused_from_snapshot"] = True
        # 旧快照里没有 coverage：按证据本身补齐，前端就不必为"字段缺失"编措辞。
        diagnostics.setdefault("coverage", _coverage_of(evidence))
        print("[Retrieval Agent] 本轮沿用上一轮保存的岗位依据快照，未重新切句", flush=True)
        log("Retrieval Agent")
        return check_update(
            {
                "job_evidence": evidence,
                "retrieval_status": status,
                "retrieval_diagnostics": diagnostics,
            },
            "Retrieval Agent",
        )

    jd_text = str(state.get("jd_text") or "").strip()
    if not jd_text:
        # 没粘原文不等于没依据可说：内置岗位画像登记过资料出处的岗位，先据实交出整理稿。
        # 认不出岗位时这里返回 None，退回"空证据 + 全模型推断"的老口径。
        return _digest_update(target_job) or _empty_update(
            "no_jd_text", "本次没有粘贴招聘要求原文"
        )

    records, dropped = chunk_user_jd(jd_text)
    if not records:
        return _empty_update(
            "user_jd_empty", "粘贴的原文过短或全是标题序号，没切出可用句子"
        )

    log("Retrieval Agent")
    return check_update(
        {
            "job_evidence": records,
            "retrieval_status": STATUS_USER_PASTED,
            "retrieval_diagnostics": {
                "job_id": USER_JD_SOURCE_ID,
                "source_id": USER_JD_SOURCE_ID,
                "match_kind": "user_pasted",
                "scope_count": len(records),
                "dropped_sentences": dropped,
                "reason": _SOURCE_NOTE,
                "coverage": _coverage_of(records),
            },
        },
        "Retrieval Agent",
    )
