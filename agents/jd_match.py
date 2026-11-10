"""岗位匹配结论（确定性汇总，纯函数、无 IO）。

本模块把"用户粘贴的招聘要求原文"与"学生当前能力"做一次逐条对账：

- 输入只有三份 state 数据：``skill_gaps``（Assessment Agent）、``job_requirements``
  （Job Agent）、``job_evidence``（Retrieval Agent 切出的原话句子）。
- 全部结论由代码算，**不采信模型自填的文本**：一条岗位要求算不算"有依据"，
  看它的 ``evidence_refs`` 能不能在 ``job_evidence`` 里按编号回查到真实记录。

判定口径
--------
1. 归属判据是**代码回查**：``evidence_refs`` 形如 ``["E01"]``，E01 对应
   ``job_evidence[0]``。回查不到的要求不计入任何一项统计。
2. 同技能的要求只算一次（去重键为小写去空格后的技能名）。
3. 没有可判定要求时 ``overall`` 直接返回 ``insufficient``，**不强行出满足率**。
4. ``overall`` 只用 confirmed 的规则：全满足且无未解决硬性缺口 → ``satisfied``；
   存在明确差距 → ``partial``；证据不足 → ``insufficient``。不加未经确认的阈值。

计数口径（五个数含义不同，不得混用）
------------------------------------
==========================  ==================================================
``requirement_total``       **有证据归属**的要求数（≠ 可判定数，其中可能还有
                            对不上能力差距的项）
``requirement_judged``      **可判定**要求数 = 已满足 + 存在差距，**满足率的分母**
``requirement_satisfied``   已满足数
``requirement_shortfall``   存在差距数
``requirement_orphan``      证据不足数 = total - judged（不进分子也不进分母）
==========================  ==================================================

满足率 ``match_rate`` 的分母**只能是** ``requirement_judged``；它为 0 时取 ``None``，
调用方必须不显示百分比，也不得拿 ``requirement_total`` 当分母。
"""

from __future__ import annotations

import re

from agents.user_jd import USER_JD_PROVENANCE

# 未解决硬性缺口的门槛：目标要求有证据支撑 + 当前能力为"无" + 等级重要。
_HARD_IMPORTANCE = 4

_PRIORITY_RANK = {"高": 0, "中": 1, "低": 2}
# 对外展示的"存在差距"状态。等级已经达标、但只有自述或跨技能推断的，也算差距：
# 它们缺的是可验证的项目/成长证据，不该计入满足率。
_GAP_SHORTFALL = ("部分满足", "明显不足", "疑似具备", "未验证")

# 有证据支撑的 evidence_status：
#   supported           —— 技能名与原话完全对得上
#   partially_supported —— 仅靠别名/语义相关，证据强度弱于 supported
# 两者都参与硬性缺口判定，但条目里会原样带出 ``evidence_status``，
# 供调用方区分"完全确认"与"仅弱证据"，不得把后者描述成完全确认。
_EVIDENCE_BACKED_STATUSES = ("supported", "partially_supported")

_USER_JD_WARNING = (
    "本次匹配逐条对照你粘贴的招聘要求原文得出；结论只适用于这份原文对应的岗位，"
    "不代表其他公司或同类岗位的一般门槛。"
)
_NO_BASIS_WARNING = (
    "本次没有招聘要求原文作依据，下面的岗位要求与匹配结论全部由模型按行业通识推断，"
    "不代表任何真实岗位的门槛。"
)


def _norm_skill(name) -> str:
    return str(name or "").strip().lower().replace(" ", "")


def _req_evidence(req: dict, evidence: list) -> list[dict]:
    """取一条岗位要求实际引用的证据记录（按 evidence_refs 里的编号）。

    只做只读回查，回查不到就忽略，绝不采信模型填写的其他文本。
    """
    refs = req.get("evidence_refs") or []
    picked: list[dict] = []
    for ref in refs:
        m = re.fullmatch(r"E(\d+)", str(ref or "").strip())
        if not m:
            continue
        idx = int(m.group(1)) - 1
        if 0 <= idx < len(evidence):
            picked.append(evidence[idx])
    return picked


def compute_jd_match(
    skill_gaps: list,
    job_requirements: list,
    evidence: list,
) -> dict:
    """确定性汇总：产出岗位匹配结论。纯函数，不连模型、不做 IO。

    参数：
    - ``skill_gaps``       来自 Assessment Agent（含 status / current_level / priority）
    - ``job_requirements`` 来自 Job Agent（含 evidence_refs / evidence_status / importance）
    - ``evidence``         来自 Retrieval Agent 的 ``job_evidence``
    """
    skill_gaps = list(skill_gaps or [])
    job_requirements = list(job_requirements or [])
    evidence = list(evidence or [])

    # 1) 有证据归属的要求，去重（口径：同技能只计一次）
    seen: set[str] = set()
    attached: list[dict] = []
    for req in job_requirements:
        if not _req_evidence(req, evidence):
            continue
        key = _norm_skill(req.get("skill"))
        if key in seen:
            continue
        seen.add(key)
        attached.append(req)

    # requirement_total = **有证据归属**的要求数（不是可判定数，也不是全部要求数）
    requirement_total = len(attached)

    # 2) 与 skill_gaps 按技能名对号，统计满足 / 缺口
    gap_by_skill = {_norm_skill(g.get("skill")): g for g in skill_gaps}
    satisfied: list[dict] = []
    shortfall: list[dict] = []
    hard_blocks: list[dict] = []
    for req in attached:
        gap = gap_by_skill.get(_norm_skill(req.get("skill")))
        if gap is None:
            continue  # assessment 未给出该项，按证据不足处理，不计入
        status = gap.get("status")
        display = str(gap.get("display_status") or status or "")
        if display == "已满足":
            satisfied.append(gap)
        elif display in _GAP_SHORTFALL:
            shortfall.append(gap)
        # 硬性缺口：目标要求有证据支撑 + 当前为"无" + 重要
        if (
            gap.get("current_level") == "无"
            and req.get("evidence_status") in _EVIDENCE_BACKED_STATUSES
            and int(req.get("importance") or 0) >= _HARD_IMPORTANCE
        ):
            hard_blocks.append({
                "skill": req.get("skill"),
                "required_level": gap.get("required_level"),
                "importance": req.get("importance"),
                "status": gap.get("status"),
                "evidence_status": req.get("evidence_status"),
            })

    requirement_satisfied = len(satisfied)
    requirement_shortfall = len(shortfall)
    # 可判定数 = 真正能参与比较的要求数；它才是满足率的分母
    requirement_judged = requirement_satisfied + requirement_shortfall
    # 证据不足数：有归属但对不上能力差距，既不进分子也不进分母
    requirement_orphan = requirement_total - requirement_judged
    # 分母为 0 时不出比率
    match_rate = (
        round(requirement_satisfied / requirement_judged, 4)
        if requirement_judged
        else None
    )

    # 3) overall + key_gaps 复用 skill_gaps 排序
    if requirement_judged == 0:
        overall = "insufficient"
    elif requirement_shortfall > 0 or hard_blocks:
        overall = "partial"
    else:
        overall = "satisfied"

    target_list = [g for g in skill_gaps if _norm_skill(g.get("skill")) in seen]
    target_list.sort(key=lambda g: (_PRIORITY_RANK.get(g.get("priority"), 9), -int(g.get("importance") or 0), g.get("skill") or ""))

    from_user_jd = any(item.get("provenance") == USER_JD_PROVENANCE for item in evidence)
    return {
        "overall": overall,
        "requirement_total": requirement_total,
        "requirement_judged": requirement_judged,
        "requirement_orphan": requirement_orphan,
        "requirement_satisfied": requirement_satisfied,
        "requirement_shortfall": requirement_shortfall,
        "match_rate": match_rate,
        "hard_blocks": hard_blocks,
        "key_gaps": target_list,
        "jd_source_warning": [_USER_JD_WARNING if from_user_jd else _NO_BASIS_WARNING],
    }
