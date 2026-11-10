"""LangGraph 工作流：把各 Agent 串成一条分析流水线。

START ──┬─→ profile → growth ──┐
        └─→ retrieval ─────────┴─→ job → assessment ─┬─→ learning → project → END
                                                      └─→ END

- retrieval 只依赖用户粘贴的招聘原文（没有原文时交出空证据并如实说明），
  不依赖学生能力，所以和 profile 同时从 START 扇出：岗位依据整理不必排在对
  学生画像的模型调用后面。两条分支在 job 前汇合，job 看到的仍是"新能力 +
  完整依据"。
- growth 节点必须紧跟 profile：能力必须先按本轮成长事件更新完，后面的能力评估、
  学习与项目规划才会按**新能力**重算。没有成长事件时它不升级任何等级，
  能力表等于"合并历史后的基线"（历史为空时就是 profile 的原样输出）。
- assessment 之后的条件分支保持原样：所有岗位要求都已有直接证据时提前收尾，
  不再生成没有缺口可挂的任务。
"""

from types import SimpleNamespace

from langgraph.graph import END, START, StateGraph

from agents.assessment_agent import assessment_node
from agents.goal import (
    GOAL_ACHIEVED,
    GOAL_BASIS_INSUFFICIENT,
    evaluate_goal,
    is_goal_closed,
)
from agents.growth_agent import compare_growth_gaps, growth_node
from agents.jd_match import compute_jd_match
from agents.job_agent import job_node
from agents.learning_agent import learning_node
from agents.profile_agent import profile_node
from agents.project_agent import project_node
from agents.retrieval_agent import retrieval_node
from agents.skill_dict import SkillVocabulary
from graph.state import CareerState

# 节点名 → 处理函数
NODES = {
    "profile": profile_node,
    "growth": growth_node,
    "retrieval": retrieval_node,
    "job": job_node,
    "assessment": assessment_node,
    "learning": learning_node,
    "project": project_node,
}

#: 节点名的展示/汇报顺序（不是执行顺序 —— retrieval 与 profile 并行）。
#: 进度回报按这个集合过滤事件、给没走到的节点补 ``skipped``。
ORDER = ["profile", "growth", "retrieval", "job", "assessment", "learning", "project"]


def build_workflow():
    """构建并编译流水线。

    唯一的分叉是 START 同时启动 profile 与 retrieval；唯一的条件分支在
    assessment 之后：所有岗位要求都已有直接证据时，再往下跑 learning/project
    只会产出空计划（或直接报错）。此时提前收尾，把"目标已达成 / 依据不足无法
    确认"如实带回汇总层。
    """
    builder = StateGraph(CareerState)

    for name, node in NODES.items():
        builder.add_node(name, node)

    # 两条起点分支：能力画像（含成长更新）与岗位依据整理互不依赖。
    builder.add_edge(START, "profile")
    builder.add_edge(START, "retrieval")
    builder.add_edge("profile", "growth")
    # job 需要"新能力"和"完整依据"两样都到齐。必须用列表形式一次汇合：
    # 两个单独的 add_edge("growth","job") / add_edge("retrieval","job") 会让
    # LangGraph 每条边各触发一次，job 及其下游会跑两遍（第一遍还看不到另一条分支）。
    builder.add_edge(["growth", "retrieval"], "job")
    builder.add_edge("job", "assessment")
    builder.add_conditional_edges(
        "assessment",
        _route_after_assessment,
        {"learning": "learning", "done": END},
    )
    builder.add_edge("learning", "project")
    builder.add_edge("project", END)

    return builder.compile()


def _route_after_assessment(state: dict) -> str:
    """assessment → learning，或直接收尾。

    依据不足但确实已全部达标也走 ``done``：不是宣布达成，而是**不再生成
    没有缺口可挂的任务**，由 ``build_final_plan`` 明确回报"无法确认"。
    """
    if is_goal_closed(evaluate_goal(state)):
        return "done"
    return "learning"


# 模块级编译一次，main.py 直接 import 使用
workflow = build_workflow()


def _load_vocabulary() -> SkillVocabulary | None:
    """取技能词表用于缺口配对。不可用时返回 None，配对退回"小写去空格"的兜底键。

    汇总阶段不该因为读不到知识库文件就报错 —— 配对键两侧同源（都来自 job_requirements
    的 skill 原文），兜底键一样能对上，只是少了别名归并。
    """
    try:
        return SkillVocabulary()
    except Exception:  # noqa: BLE001 - 汇总阶段的降级路径
        return None


def _rebuild_analysis(answers: list):
    """把 ``assessment_answers`` 重建成本模块所需的最小 analysis 对象。

    只回填 ``judge_gaps`` 真正读取的三个字段。``supported_by`` 缺失或为空时补成**空字符串**
    —— 空串会走"学生表里查不到 → 无"的既有路径，与"模型没给出支撑"语义一致；
    这里**不写 "无"**（"无"是 judge_gaps 的判定结果，不是模型的原始回答）。
    """
    return SimpleNamespace(gaps=[
        SimpleNamespace(
            skill=item.get("skill"),
            supported_by=str(item.get("supported_by") or ""),
            gap=item.get("gap"),
        )
        for item in answers
    ])


def _growth_block(state: dict):
    """把成长前后的岗位缺口对比并进 ``state`` 里已有的 ``growth_diff``。

    没有成长上下文（阶段一~四那种不跑 growth 节点的运行）时返回 ``None``：
    那样 ``final_plan`` 一个键都不多，旧输出逐字段不变。

    对比要求：**同一批岗位要求 + 同一批模型原始回答**，只有技能快照不同。
    拿不到原始回答时交给 compare_growth_gaps 明确报"不可比较"，
    **不拿空回答硬算**（那会退化成"只比同名技能"，看起来像结论其实不是）。
    """
    if not (state.get("growth_diff") or state.get("user_skills_before")):
        return None

    raw_answers = state.get("assessment_answers") or []
    comparison = compare_growth_gaps(
        state.get("user_skills_before") or [],
        state.get("user_skills") or [],
        state.get("job_requirements") or [],
        _rebuild_analysis(raw_answers) if raw_answers else None,
        _load_vocabulary(),
    )

    block = dict(state.get("growth_diff") or {})
    block["gap_comparison"] = comparison
    block["gap_changes"] = comparison.get("changes", [])
    block["newly_satisfied"] = comparison.get("newly_satisfied", [])
    block["newly_appeared"] = comparison.get("newly_appeared", [])
    block["resolved_gaps"] = comparison.get("resolved_gaps", [])
    return block


def build_final_plan(state: dict) -> dict:
    """把各 Agent 的产出汇总成最终方案。

    图在 project 之后就结束了（没有额外的汇总节点），
    所以这里用确定性的代码汇总，不再多调一次 LLM。
    """
    gaps = state.get("skill_gaps") or []
    stages = state.get("learning_plan") or []
    projects = state.get("project_plan") or []
    goal = evaluate_goal(state)

    def shown_status(gap: dict) -> str:
        return str(gap.get("display_status") or gap.get("status") or "")

    # 岗位匹配结论（确定性汇总，复用 job_evidence / skill_gaps）
    jd_match = compute_jd_match(
        skill_gaps=gaps,
        job_requirements=state.get("job_requirements") or [],
        evidence=state.get("job_evidence") or [],
    )

    if goal["status"] == GOAL_ACHIEVED:
        next_action = "目标已达成，可以收尾；如需继续，换一个目标岗位或重新粘贴招聘要求再分析。"
    elif goal["status"] == GOAL_BASIS_INSUFFICIENT:
        next_action = "能力已达标，但岗位要求依据不足；请粘贴真实招聘要求后重新分析，才能确认达成。"
    elif stages:
        next_action = f"从「{stages[0]['topic']}」开始，完成后按完成标准自测"
    else:
        next_action = "先补齐岗位要求中最关键的技能"

    plan = {
        "target_job": state.get("target_job", ""),
        # 只有带项目/成长证据的才算已满足；等级达标但证据不足的单独列出
        "already_satisfied": [g["skill"] for g in gaps if shown_status(g) == "已满足"],
        "needs_verification": [
            g["skill"] for g in gaps if shown_status(g) in ("疑似具备", "未验证")
        ],
        "top_gaps": [g["skill"] for g in gaps if g["priority"] == "高"][:3],
        "learning_stage_count": len(stages),
        "recommended_projects": [p["name"] for p in projects],
        "goal_status": goal["status"],
        "goal": goal,
        "next_action": next_action,
        "jd_match": jd_match,
    }

    # 阶段五：有成长上下文时补上"成长前后缺口变化"。没有成长上下文就不加这个键 ——
    # 阶段一~四的运行输出因此逐字段保持原样。
    growth = _growth_block(state)
    if growth is not None:
        plan["growth_diff"] = growth

    return plan