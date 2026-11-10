"""目标结局判定：由代码看能力表与证据，不听模型总结。

三档结局：

- ``achieved``           岗位要求全部「已满足 + 直接证据」，且要求本身有可靠依据；
- ``in_progress``        还有未满足 / 未验证的要求（含依据不足但仍有缺口的情况）；
- ``basis_insufficient`` 能力表已全部达标，但岗位要求没有可靠依据，**不得宣布达成**。

任务勾选进度不参与判定：任务只是手段，达成与否只看岗位要求有没有被直接证据覆盖。
"""

GOAL_ACHIEVED = "achieved"
GOAL_IN_PROGRESS = "in_progress"
GOAL_BASIS_INSUFFICIENT = "basis_insufficient"

BASIS_USER_PASTED = "user_pasted"
BASIS_KNOWLEDGE_BASE = "knowledge_base"
BASIS_MODEL_ONLY = "model_only"

_VERIFICATION_DIRECT = "direct"
_CLOSED_STATUSES = (GOAL_ACHIEVED, GOAL_BASIS_INSUFFICIENT)


def _shown_status(gap: dict) -> str:
    return str(gap.get("display_status") or gap.get("status") or "")


def _is_satisfied(gap: dict) -> bool:
    """「已满足」必须同时带证据；旧数据缺 verification 字段时按 display_status 为准。"""
    if _shown_status(gap) != "已满足":
        return False
    verification = str(gap.get("verification") or "")
    return verification in ("", _VERIFICATION_DIRECT)


def _names(items: list) -> list[str]:
    return [str(item.get("skill") or "").strip() for item in items if item.get("skill")]


def evaluate_goal(state: dict) -> dict:
    """返回可直接给前端展示的结局快照。**纯函数，不调模型、不做 IO。**"""
    gaps = [gap for gap in (state.get("skill_gaps") or []) if isinstance(gap, dict)]
    requirements = [req for req in (state.get("job_requirements") or []) if isinstance(req, dict)]
    basis = str(state.get("job_requirements_basis") or BASIS_MODEL_ONLY)
    grounded = bool(state.get("job_requirements_grounded"))

    satisfied = [gap for gap in gaps if _is_satisfied(gap)]
    remaining = [gap for gap in gaps if not _is_satisfied(gap)]
    needs_verification = [
        gap for gap in remaining if _shown_status(gap) in ("疑似具备", "未验证")
    ]
    unmet = [
        gap for gap in remaining if _shown_status(gap) not in ("疑似具备", "未验证")
    ]

    # 只有用户原文路径会把"模型补充项"留在要求表里；知识库路径的补全项由画像担保。
    ungrounded = [
        str(req.get("skill") or "").strip()
        for req in requirements
        if basis == BASIS_USER_PASTED
        and req.get("evidence_status") == "model_supplement"
        and req.get("skill")
    ]

    if not gaps:
        status = GOAL_IN_PROGRESS
        message = "本次没有可判定的岗位要求，无法给出目标结局。"
    elif remaining:
        status = GOAL_IN_PROGRESS
        message = f"还有 {len(remaining)} 项岗位要求未满足或未验证，目标尚未达成。"
        if not grounded:
            message += "岗位要求本身缺少可靠依据，结论只能当参考。"
    elif grounded and not ungrounded:
        status = GOAL_ACHIEVED
        message = (
            f"岗位要求的 {len(gaps)} 项能力均已满足，"
            "并且都有项目经历或成长记录作为直接证据。"
        )
    else:
        status = GOAL_BASIS_INSUFFICIENT
        if ungrounded:
            names = "、".join(ungrounded[:3])
            suffix = " 等" if len(ungrounded) > 3 else ""
            message = (
                f"能力表已达到全部要求，但其中 {len(ungrounded)} 项要求是模型补充的"
                f"（{names}{suffix}）、招聘原文里没有，无法确认目标达成。"
            )
        else:
            message = (
                "能力表已达到全部要求，但岗位要求既没有招聘原文、也不在内置岗位画像里，"
                "属于模型推断，无法确认目标达成。"
            )

    return {
        "status": status,
        "basis": basis,
        "grounded": grounded and not ungrounded,
        "required_count": len(gaps),
        "satisfied_count": len(satisfied),
        "remaining_count": len(remaining),
        "remaining": _names(remaining),
        "remaining_high": [
            str(gap.get("skill") or "").strip()
            for gap in remaining
            if gap.get("priority") == "高" and gap.get("skill")
        ],
        "needs_verification": _names(needs_verification),
        "unmet": _names(unmet),
        "ungrounded_requirements": ungrounded,
        "message": message,
    }


def is_goal_closed(goal: dict) -> bool:
    """是否已经到了"不该再出任务"的结局（达成或依据不足无法确认）。"""
    return goal.get("status") in _CLOSED_STATUSES
