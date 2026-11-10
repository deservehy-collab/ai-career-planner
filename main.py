"""AI 大学生成长 / 就业智能规划系统 —— 第六阶段：成长闭环（--loop）"""

import json
import sys

from agents.growth_agent import (
    OUTCOME_ALL_DUPLICATES,
    OUTCOME_BLOCKED,
    OUTCOME_GATE_UNAVAILABLE,
    OUTCOME_INVALID_EVENTS,
    OUTCOME_NO_EVENTS,
    OUTCOME_NO_UPGRADE,
    OUTCOME_STORE_UNREADABLE,
    OUTCOME_UPGRADED,
    get_store as get_growth_store,
)
from agents.growth_loop import (
    LOOP_COMPLETED_NO_EVENT,
    LOOP_COMPLETED_NO_UPGRADE,
    LOOP_COMPLETED_UPGRADED,
    LOOP_COMPLETION_FAILED,
    get_loop_store,
    pending_growth_events,
    process_task_completion,
)
from agents.growth_rules import (
    GATE_BLOCKED,
    GATE_NOT_COVERED,
    GATE_PASSED,
    GATE_UNAVAILABLE,
    KB_STATUS_LOAD_FAILED,
    validate_event,
)
from agents.growth_store import ENTRY_KIND_ADJUSTMENT, STATUS_EMPTY, WRITE_REFUSED
from agents.loop_store import (
    COMPLETE_ALREADY,
    COMPLETE_OK,
    KIND_LEARNING,
    KIND_PROJECT,
    TASK_COMPLETED,
    TASK_PENDING,
    WRITE_OK as LOOP_WRITE_OK,
)
from graph.workflow import build_final_plan, workflow

# Windows 控制台默认可能是 GBK，强制 UTF-8 避免中文乱码
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

user_profile = {
    "major": "计算机科学与技术",
    "grade": "大三",
    "skills": ["Python", "LangChain", "LangGraph", "RAG", "Docker"],
    "projects": ["RAG知识库问答系统"],
}

target_job = "AI应用开发工程师"

# 招聘要求原文：把招聘页上那段「任职要求」原样粘进来，本次岗位要求就以它为依据。
# 留空则没有原文 —— 岗位要求全部由模型推断，前端会如实标灰、不给匹配度百分比。
jd_text = ""


def show(title: str, data) -> None:
    print(f"\n{'=' * 10} {title} {'=' * 10}")
    print(json.dumps(data, ensure_ascii=False, indent=2))


# 终端展示用的学习阶段字段：只挑原有业务字段
LEARNING_STAGE_FIELDS = (
    "stage",
    "topic",
    "goal",
    "covers",
    "prerequisite",
    "completion_criteria",
    "estimated_weeks",
)


def public_learning_plan(stages: list) -> list:
    """把学习路线投影成**只含原有业务字段**的展示形态。

    阶段字典里还有 ``skill_mapping`` / ``unmapped_covers`` /
    ``dropped_low_priority`` 等内部追踪字段 —— 它们在 state 里**继续保留**
    （下游与排查都要用），只是在终端展示这一层被过滤掉，避免把内部口径
    混进给用户看的输出。这里**只做投影，不删任何内部数据**。

    缺失字段一律按 None 处理：``dropped_low_priority`` 本来就是"非空才写"，
    空路线也不能让展示炸掉。
    """
    return [
        {key: stage.get(key) for key in LEARNING_STAGE_FIELDS}
        for stage in (stages or [])
        if isinstance(stage, dict)
    ]


def show_jd_match(match: dict) -> None:
    """展示岗位匹配结论（纯展示，复用 jd_match 结果）。"""
    print(f"\n{'=' * 10} 岗位匹配 {'=' * 10}")
    if not match:
        print("（没有匹配结论）")
        return

    print(f"总体结论：{match.get('overall', 'unknown')}")
    for line in match.get("jd_source_warning") or []:
        print(f"依据说明：{line}")

    # 计数口径分三层，不能把"有证据归属数"说成"可判定数"：
    # 有证据归属 = 归属该目标 + 能回查到证据；可判定 = 其中还能对上能力差距的。
    judged = match.get("requirement_judged", 0)
    print(f"有证据归属的岗位要求：{match.get('requirement_total', 0)} 项")
    print(f"  可判定（计入统计）：{judged} 项")
    print(f"    - 已满足：{match.get('requirement_satisfied', 0)} 项")
    print(f"    - 存在差距：{match.get('requirement_shortfall', 0)} 项")
    print(f"  证据不足（不计入统计，也不进分母）：{match.get('requirement_orphan', 0)} 项")

    # 满足率的分母只能是可判定要求数；分母为 0 时不出百分比
    rate = match.get("match_rate")
    if rate is None:
        print("满足率：无可判定要求，本次不出满足率")
    else:
        print(f"满足率：{rate:.0%}（分母 = 可判定要求 {judged} 项）")

    hard_blocks = match.get("hard_blocks") or []
    if hard_blocks:
        print(
            f"硬性缺口候选（{len(hard_blocks)} 项）"
            f"——supported = 技能名与证据完全同名；partially_supported = 仅别名/语义相关，"
            f"证据强度弱于前者，不等于完全确认："
        )
        for block in hard_blocks:
            print(
                f"  - {block.get('skill')}"
                f"（重要性 {block.get('importance')}，"
                f"要求等级 {block.get('required_level')}，"
                f"证据强度 {block.get('evidence_status') or '未知'}）"
            )

    key_gaps = match.get("key_gaps") or []
    if key_gaps:
        print("关键差距（按优先级）：")
        for gap in key_gaps:
            print(
                f"  - {gap.get('skill')}"
                f"（重要性 {gap.get('importance')}，优先级 {gap.get('priority')}）"
            )

    warning = match.get("jd_source_warning")
    if warning:
        print(f"来源提示：{warning}")


def show_retrieval(result: dict) -> None:
    """展示岗位依据（用户粘贴的招聘要求原文，一句一条；纯展示，不参与规划计算）。"""
    print(f"\n{'=' * 10} 岗位依据原文 {'=' * 10}")

    evidence = result.get("job_evidence") or []
    diagnostics = result.get("retrieval_diagnostics") or {}

    print(f"依据状态：{result.get('retrieval_status', '未执行')}")
    print(f"原话句数：{len(evidence)} 句")
    dropped = diagnostics.get("dropped_sentences")
    if dropped:
        print(f"超出句数上限被丢弃：{dropped} 句（后半段原文没有参与分析）")
    if diagnostics.get("reason"):
        print(f"说明：{diagnostics['reason']}")

    if not evidence:
        print("（没有原文可依：岗位要求将由模型推断，并逐条标成「模型补充」）")
        return

    for index, item in enumerate(evidence, start=1):
        print(f"  E{index:02d}. {item.get('note')}")


# ── 阶段五：动态能力成长的输入与展示 ────────────────────────────────────
# outcome → 给人看的一句话（键取自 agents/growth_agent.py 的常量，避免两处写法漂移）
OUTCOME_LABELS = {
    OUTCOME_NO_EVENTS: "本轮没有成长事件",
    OUTCOME_UPGRADED: "有技能等级提升",
    OUTCOME_NO_UPGRADE: "有事件，但没有产生等级变化",
    OUTCOME_ALL_DUPLICATES: "事件都是历史重复，未重复升级",
    OUTCOME_BLOCKED: "升级被前置技能门禁拦下",
    OUTCOME_GATE_UNAVAILABLE: "知识库不可用，前置门禁未执行",
    OUTCOME_INVALID_EVENTS: "事件不合法，已跳过",
    OUTCOME_STORE_UNREADABLE: "成长历史无法读取（已按 profile 基线继续）",
}

# 门禁结论 → 给人看的一句话
GATE_LABELS = {
    GATE_PASSED: "已通过（技能已收录且前置齐备）",
    GATE_NOT_COVERED: "已跳过（该技能未收录等级标准）",
    GATE_UNAVAILABLE: "未执行（知识库加载失败）",
    GATE_BLOCKED: "被拦截（前置技能不足）",
}

_GROWTH_TYPE_CHOICES = {"1": "learning", "2": "project"}
_GROWTH_TYPE_LABELS = {"learning": "学习成果", "project": "项目成果"}
_YES_WORDS = ("y", "yes", "是")
_SKIP_WORDS = ("", "n", "no", "否", "跳过", "s")


def _ask(input_fn, prompt: str) -> str:
    """读一行输入并去掉首尾空白。input_fn 可注入，便于自动化测试。"""
    return str(input_fn(prompt) or "").strip()


def _ask_growth_event(input_fn, index: int):
    """问一条成长成果。返回事件 dict；用户放弃时返回 ``None``。

    只收集 ``validate_event()`` 要求的字段（type / title / skills / evidence），
    **不提供"把技能升到几级"的输入** —— 等级一律由 agents/growth_rules.py 按
    「一档封顶 + 证据分级 + 前置技能门禁」判定，用户只负责提供"做了什么"这类事实。

    每个字段**各自重问**：某一项填错只重问那一项，不会把整份表单推倒重来。
    """
    print(f"\n-- 第 {index} 条成果（类型处直接回车即跳过）--")
    print("  1 = 学习成果（本轮最高只能升到 初级）")
    print("  2 = 项目成果（本轮最高只能升到 中级）")

    while True:
        kind = _ask(input_fn, "  类型 [1/2，回车跳过]：").lower()
        if kind in _SKIP_WORDS:
            return None
        if kind in _GROWTH_TYPE_CHOICES:
            break
        print(f"  ✗ 类型只能填 1 或 2，收到 {kind!r}，请重新输入")

    while True:
        title = _ask(input_fn, "  成果标题（如：完成 RAG 知识库问答系统）：")
        if title:
            break
        print("  ✗ 标题不能为空，请重新输入")

    while True:
        raw_skills = _ask(input_fn, "  涉及技能（顿号或逗号分隔，如：RAG、Embedding）：")
        skills = [
            part.strip()
            for part in raw_skills.replace("，", "、").replace(",", "、").split("、")
            if part.strip()
        ]
        if skills:
            break
        print("  ✗ 至少要填一项技能，请重新输入")

    while True:
        evidence = _ask(input_fn, "  证据（具体做了什么、有什么产出）：")
        if evidence:
            break
        print("  ✗ 证据不能为空 —— 只有标题不会带来任何升级")

    event = {
        "type": _GROWTH_TYPE_CHOICES[kind],
        "title": title,
        "skills": skills,
        "evidence": evidence,
    }
    # 四个字段已逐个把关，这里正常不会再失败；留着是为了万一口径变了能明确报出来
    _, reason = validate_event(event)
    if reason:
        print(f"  ✗ 这条成果不合法：{reason}（已跳过）")
        return None

    print(
        f"  ✓ 已记录：{_GROWTH_TYPE_LABELS[event['type']]}「{title}」"
        f"（{'、'.join(skills)}）"
    )
    return event


def collect_growth_events(input_fn=input) -> list:
    """交互式收集本轮新增的学习 / 项目成果。直接回车即跳过，不记录任何成长。

    返回值直接塞进 ``workflow.invoke(...)`` 的 ``growth_events`` 字段；
    跳过时返回空列表，工作流行为与不带成长事件时一致。
    """
    print(f"\n{'=' * 10} 记录成长成果 {'=' * 10}")
    print("可以登记本轮新完成的学习或项目成果，系统会据此重新评估能力与路线。")
    answer = _ask(input_fn, "现在要记录新的成果吗？[y/N]：").lower()
    if answer not in _YES_WORDS:
        print("（已跳过：本轮不记录成长事件，能力维持现状）")
        return []

    events: list = []
    while True:
        event = _ask_growth_event(input_fn, len(events) + 1)
        if event is None:
            break
        events.append(event)
        if _ask(input_fn, "还要再记一条吗？[y/N]：").lower() not in _YES_WORDS:
            break

    if not events:
        print("（已跳过：本轮不记录成长事件，能力维持现状）")
    else:
        print(f"\n共记录 {len(events)} 条成长成果，将交给成长节点判定等级。")
    return events


def show_growth_result(growth: dict) -> None:
    """展示动态能力成长结果（纯展示，复用 growth_diff 的真实字段）。"""
    print(f"\n{'=' * 10} 动态能力成长 {'=' * 10}")
    if not growth:
        print("（本轮没有成长记录）")
        return

    outcome = growth.get("outcome")
    print(f"本轮结果：{OUTCOME_LABELS.get(outcome, outcome or '未知')}")
    print(f"收到成果事件：{growth.get('events_received', 0)} 条")
    if growth.get("knowledge_base_status") == KB_STATUS_LOAD_FAILED:
        print("⚠ 技能知识库加载失败：本次**没有执行**前置技能门禁，升级结果可能偏宽松")

    # 1) 技能等级变化
    updates = growth.get("level_changes") or []
    if updates:
        print(f"\n技能等级变化（{len(updates)} 项）：")
        for item in updates:
            print(f"  - {item.get('skill')}：{item.get('from') or '无'} → {item.get('to')}")
            print(f"      依据：{item.get('reason')}")
            print(f"      来源成果：{item.get('source')}")
            print(f"      前置门禁：{GATE_LABELS.get(item.get('gate_status'), item.get('gate_status') or '未知')}")
            if item.get("gate_unavailable"):
                print("      ⚠ 该升级没有经过前置技能校验，不能当成已通过门禁")
            for note in item.get("notes") or []:
                print(f"      提示：{note}")
    else:
        print("\n技能等级变化：无 —— 本轮没有任何技能的等级被提升（原因见下面的分类）")

    # 2) 被门禁拦下的升级
    blocked = growth.get("blocked_upgrades") or []
    if blocked:
        print(f"\n被前置门禁拦下的升级（{len(blocked)} 项）：")
        for item in blocked:
            missing = "、".join(item.get("missing_prerequisites") or []) or "（未给出）"
            print(
                f"  - {item.get('skill')}：{item.get('current_level') or '无'}"
                f" → 本可升到 {item.get('candidate_level')}"
            )
            print(f"      缺前置：{missing}")
            print(f"      原因：{item.get('reason')}")

    # 3) 重复 / 非法 / 未映射 / 未升级的候选
    duplicates = growth.get("duplicates") or []
    if duplicates:
        print(f"\n重复成果（{len(duplicates)} 条，已跳过以免重复升级）：")
        for item in duplicates:
            print(f"  - {item.get('title')}（指纹 {item.get('fingerprint')}）")

    invalid = growth.get("invalid_events") or []
    if invalid:
        print(f"\n不合法成果（{len(invalid)} 条，已跳过）：")
        for item in invalid:
            print(f"  - 第 {item.get('index')} 条：{item.get('reason')}")

    unmapped = growth.get("unmapped_skills") or []
    if unmapped:
        print(f"\n无法归一的技能名（{len(unmapped)} 处，未参与升级）：")
        for item in unmapped:
            print(f"  - 来自「{item.get('title')}」：{'、'.join(item.get('skills') or [])}")

    skipped = growth.get("skipped") or []
    if skipped:
        print(f"\n未升级的候选（{len(skipped)} 项）：")
        for item in skipped:
            print(f"  - {item.get('skill')}（{item.get('title')}）：{item.get('reason')}")

    # 4) 历史写入状态：True / False / None / 字段缺失 四种情况分开说，
    #    绝不把"没有发生写入"说成写入成功
    print("\n成长历史：")
    written = growth.get("history_written")
    status = growth.get("history_write_status")
    if written is True:
        print(f"  已写入 runs/growth.json（写入状态：{status}）")
    elif written is False and status == WRITE_REFUSED:
        print("  **未写入** —— 存储层拒绝了本次写入，原文件很可能已损坏且未被覆盖")
    elif written is False and status is None:
        print("  本轮无需写入（没有产生需要记录的候选成果）")
    elif written is False:
        print(f"  **未写入**（写入状态：{status}）")
    else:
        print("  写入状态未知（本条记录里没有 history_written 字段）")

    for warning in growth.get("warnings") or []:
        print(f"  提示：{warning}")


def show_gap_change(comparison: dict, level_changes=None) -> None:
    """展示成长前后的岗位缺口变化。

    **必须**传 ``final_plan["growth_diff"]["gap_comparison"]``：成长节点自己写的那个
    ``gap_comparison`` 永远是"不可比"的（它跑在 job 节点之前，拿不到岗位要求），
    真正的对比是 ``build_final_plan()`` 在 assessment 之后算出来的。
    """
    print(f"\n{'=' * 10} 岗位缺口变化 {'=' * 10}")
    if not comparison:
        print("（没有缺口对比结果）")
        return
    if comparison.get("status") != "computed":
        print(f"本次无法比较成长前后的岗位缺口：{comparison.get('reason') or '原因未知'}")
        return

    counts = comparison.get("gap_count") or {}
    print(
        f"参与对比的岗位要求：成长前 {counts.get('before', '?')} 条"
        f" → 成长后 {counts.get('after', '?')} 条"
    )

    newly = comparison.get("newly_satisfied") or []
    appeared = comparison.get("newly_appeared") or []
    resolved = comparison.get("resolved_gaps") or []
    print(f"新满足的缺口：{len(newly)} 项" + (f"（{'、'.join(newly)}）" if newly else ""))
    print(f"新出现的缺口：{len(appeared)} 项" + (f"（{'、'.join(appeared)}）" if appeared else ""))
    print(f"已消除的缺口：{len(resolved)} 项" + (f"（{'、'.join(resolved)}）" if resolved else ""))

    changes = comparison.get("changes") or []
    if changes:
        print("具体变化：")
        for item in changes:
            print(
                f"  - {item.get('skill')}：{item.get('status_before')} → {item.get('status_after')}"
                f"（优先级 {item.get('priority_before')} → {item.get('priority_after')}）"
            )
    else:
        print("具体变化：无 —— 各缺口的状态与优先级都与成长前相同")
        if level_changes:
            names = "、".join(str(u.get("skill")) for u in level_changes)
            print(
                f"  说明：技能等级**确实提升了**（{names}），但还没跨过岗位要求的门槛，"
                f"所以缺口状态未变；技能成长见上面的「动态能力成长」一节。"
            )


def public_final_plan(plan: dict) -> dict:
    """最终方案的展示投影：``growth_diff`` 由专门函数展示，这里不再整份原样输出。

    只做投影，不改动 ``final_plan`` 本身。
    """
    return {key: value for key, value in (plan or {}).items() if key != "growth_diff"}


def run_pipeline(growth_events=None, announce=False) -> tuple:
    """跑一次完整工作流并展示阶段一~五的全部输出。返回 ``(result, final_plan)``。

    这一段就是从原来的 ``__main__`` 原样搬过来的 —— 展示顺序、调用的函数、
    打印的内容一个字都没改，只是包成函数，好让闭环模式也能复用同一条路径。
    """
    if announce:
        print("开始执行 LangGraph 工作流...")

    result = workflow.invoke(
        {
            "user_profile": user_profile,
            "target_job": target_job,
            "growth_events": list(growth_events or []),
            "jd_text": jd_text,
        }
    )

    show("用户能力", result.get("user_skills", []))
    show_retrieval(result)
    show("岗位要求", result.get("job_requirements", []))
    show("能力差距", result.get("skill_gaps", []))
    show("学习路线", public_learning_plan(result.get("learning_plan", [])))
    show("项目路线", result.get("project_plan", []))

    # 汇总只算一次：原计划、JD 匹配与成长差异都来自这一份结果
    final_plan = build_final_plan(result)
    growth = final_plan.get("growth_diff") or {}

    show("最终方案", public_final_plan(final_plan))
    show_jd_match(final_plan.get("jd_match"))
    show_growth_result(growth)
    show_gap_change(growth.get("gap_comparison"), growth.get("level_changes"))

    return result, final_plan


# ══════════════════════════════════════════════════════════════════════════
# 阶段六：成长闭环（python main.py --loop）
#
# 闭环怎么合上（读完这段再看下面的代码会顺很多）
# ------------------------------------------------
#   第 N 轮：工作流生成学习/项目计划 → 存进 runs/loop.json（本轮任务清单）
#            ↓
#   学生按任务 ID 提交完成证据 → process_task_completion() 把完成记录**写进
#            runs/loop.json**，并按阶段五规则算一份升级计划 —— 但**不落盘**，
#            因为成长历史的唯一正式写入方是 growth_node（agents/growth_agent.py）。
#            ↓
#   学生请求「下一轮计划」→ 把本次新产生的成长成果当 growth_events **再跑一次
#            工作流**：growth_node 走正式路径写 runs/growth.json 并升级能力，
#            assessment 用**升级后的能力**重算缺口，learning/project 据此出新计划
#            → 存成第 N+1 轮
#
# 所以「任务完成」「成长事件被接受」「技能实际升级」是三件不同的事，
# 分别发生在 loop.json、阶段五判定、和下一轮的 growth_node 里 —— 界面分开报告。
# ══════════════════════════════════════════════════════════════════════════

# 任务类型 → 展示用中文
_TASK_KIND_LABELS = {KIND_LEARNING: "学习", KIND_PROJECT: "项目"}

# process_task_completion 的整体状态 → 展示用中文
LOOP_STATUS_LABELS = {
    LOOP_COMPLETED_UPGRADED: "任务已完成；阶段五算出了升级计划（尚未落盘）",
    LOOP_COMPLETED_NO_UPGRADE: "任务已完成；阶段五本次不产生升级",
    LOOP_COMPLETED_NO_EVENT: "任务已完成；但构不成长成果",
    LOOP_COMPLETION_FAILED: "任务完成**未记录**",
}

_LOOP_MENU = (
    "\n请选择："
    "\n  [1] 提交任务完成证据"
    "\n  [2] 生成下一轮计划（会调用模型重算，并正式写入成长历史）"
    "\n  [3] 查看当前进度"
    "\n  [q] 退出"
    "\n选择："
)


def show_tasks(entry: dict) -> None:
    """展示某一轮的任务清单：任务 ID / 类型 / 标题 / 关联技能 / 完成状态。"""
    print(f"\n{'=' * 10} 第 {entry.get('round')} 轮任务 {'=' * 10}")
    print(f"目标岗位：{entry.get('target_job')}")
    print(f"生成时间：{entry.get('created_at')}")

    tasks = entry.get("tasks") or []
    if not tasks:
        print("（本轮没有任何任务 —— 学习与项目计划都是空的）")
        return

    done = sum(1 for task in tasks if task.get("status") == TASK_COMPLETED)
    print(f"共 {len(tasks)} 项，已完成 {done} 项：")
    for task in tasks:
        kind = _TASK_KIND_LABELS.get(task.get("kind"), task.get("kind"))
        skills = "、".join(task.get("skills") or []) or "（未关联技能）"
        if task.get("status") == TASK_COMPLETED:
            print(f"  ✔ {task.get('task_id')}  [{kind}] {task.get('title')}  ← {skills}")
            print(f"      完成时间：{task.get('completed_at')}｜证据：{task.get('evidence')!r}")
        else:
            print(f"  ○ {task.get('task_id')}  [{kind}] {task.get('title')}  ← {skills}  （待完成）")


def show_completion_result(result: dict) -> None:
    """分别报告「任务完成」「成长事件」「技能升级」三件事，绝不混为一谈。

    最关键的一条：``process_task_completion`` 算出的升级是**只读预览**，
    它没有写进 runs/growth.json。真正落盘发生在请求「下一轮计划」时，
    由 growth_node 走正式写入路径完成 —— 这里必须说清楚，否则用户会以为
    技能已经升上去了。
    """
    print(f"\n{'=' * 10} 任务 {result.get('task_id')} 的完成结果 {'=' * 10}")
    status = result.get("status")
    print(f"整体状态：{LOOP_STATUS_LABELS.get(status, status)}")

    # ① 任务完成 —— 唯一真正落盘的一步
    print("\n① 任务完成记录（runs/loop.json）：")
    completion = result.get("completion_status")
    if completion == COMPLETE_OK:
        print(f"  已写入（状态 {completion}）")
    elif completion == COMPLETE_ALREADY:
        print(f"  该任务此前已完成，本次**未改动磁盘**（{completion}）")
    else:
        print(f"  **未记录**（{completion}）：{result.get('reason')}")
    task = result.get("task") or {}
    if task:
        print(f"  完成时间：{task.get('completed_at')}｜证据：{task.get('evidence')!r}")

    # ② 成长事件 —— 阶段五是否接受这条成果
    print("\n② 成长成果（任务 → 阶段五事件）：")
    event = result.get("growth_event")
    if event is None:
        print(f"  未构造成果：{result.get('reason')}")
    else:
        unmapped = result.get("unmapped_skills") or []
        print(f"  已构造：{_GROWTH_TYPE_LABELS.get(event.get('type'), event.get('type'))}"
              f"「{event.get('title')}」（{'、'.join(event.get('skills') or [])}）")
        print(f"  证据（原样保留）：{event.get('evidence')!r}")
        if unmapped:
            print(f"  ⚠ 未识别的技能名（不会自动映射到相似技能）：{'、'.join(unmapped)}")
        invalid = result.get("invalid_events") or []
        duplicates = result.get("duplicate_events") or []
        if invalid:
            print(f"  阶段五校验**未通过**：{invalid[0].get('reason')}")
        elif duplicates:
            print(f"  阶段五判定为**历史重复**（指纹 {duplicates[0].get('fingerprint')}）"
                  f"—— 不会重复升级")
        else:
            print("  阶段五校验通过，且不是历史重复")

    # ③ 技能升级 —— 只读预览，明确标注未落盘
    print("\n③ 技能升级：")
    if result.get("baseline_source") == "history_only":
        print("  ⚠ 本次会话还没跑过工作流：预览以「成长历史」为基线，可能没算上 user_profile")
        print("    里的技能，比真实判定更严格。真正的升级以「下一轮计划」时 growth_node 为准。")
    if not result.get("growth_evaluated"):
        outcome = result.get("growth_outcome")
        print(f"  本次没有评估升级"
              + (f"（阶段五结论：{OUTCOME_LABELS.get(outcome, outcome)}）" if outcome else ""))
        return

    outcome = result.get("growth_outcome")
    print(f"  阶段五结论：{OUTCOME_LABELS.get(outcome, outcome)}")

    updates = result.get("updates") or []
    if updates:
        print("  ⚠ 以下是**只读预览，尚未写入 runs/growth.json**：")
        for item in updates:
            gate = GATE_LABELS.get(item.get("gate_status"), item.get("gate_status") or "未知")
            print(f"    - {item.get('skill')}：{item.get('from') or '无'} → {item.get('to')}"
                  f"（前置门禁：{gate}）")
        print("  正式落盘时机：请求「下一轮计划」时，由 growth_node 走阶段五正式写入路径完成。")
    else:
        print("  本次没有技能升级。")

    for blocked in result.get("blocked") or []:
        missing = "、".join(blocked.get("missing_prerequisites") or []) or "（未给出）"
        print(f"  被前置门禁拦下：{blocked.get('skill')}"
              f"（缺 {missing}）—— {blocked.get('reason')}")
    for skipped in result.get("skipped") or []:
        print(f"  未升级：{skipped.get('skill')} —— {skipped.get('reason')}")

    warnings = result.get("warnings") or []
    for warning in warnings:
        print(f"  提示：{warning}")


def show_progress(store) -> None:
    """展示当前进度：历轮任务与完成情况 + 真正落盘的成长历史。"""
    print(f"\n{'=' * 10} 当前进度 {'=' * 10}")

    rounds = store.rounds()
    if not rounds:
        print("（还没有任何轮次记录）")
    else:
        print(f"共 {len(rounds)} 轮，最新第 {rounds[-1].get('round')} 轮：")
        for entry in rounds:
            tasks = entry.get("tasks") or []
            done = sum(1 for task in tasks if task.get("status") == TASK_COMPLETED)
            print(f"  第 {entry.get('round')} 轮（{entry.get('created_at')}）："
                  f"{done}/{len(tasks)} 项完成")
            for task in tasks:
                if task.get("status") == TASK_COMPLETED:
                    print(f"     ✔ {task.get('task_id')} {task.get('title')}"
                          f"  证据：{task.get('evidence')!r}")

    # 成长历史是**真正落盘**的那一份，和上面任务完成情况不是一回事
    growth_store = get_growth_store()
    loaded = growth_store.load()
    print("\n成长历史（runs/growth.json）：")
    if loaded.status == STATUS_EMPTY:
        print("  还没有成长记录 —— 当前能力全部来自 user_profile")
    elif not loaded.writable:
        print(f"  ⚠ 不可读（{loaded.status}）：{loaded.warning}")
    else:
        history = loaded.data.get("history") or []
        confirmed = sum(1 for e in history if e.get("kind") != ENTRY_KIND_ADJUSTMENT)
        adjustments = len(history) - confirmed
        suffix = f"（另有 {adjustments} 条作废调整）" if adjustments else ""
        print(f"  已确认 {confirmed} 次成长记录{suffix}")
        skills = growth_store.latest_skills(loaded.data)
        if skills:
            print("  最近一次确认的能力："
                  + "、".join(f"{s.get('skill')}={s.get('level') or '无'}" for s in skills))


def persist_round(store, result: dict) -> dict | None:
    """把本轮的学习计划、项目计划与目标岗位写进 LoopStore。

    模型写的计划**原样存**，不重写、不裁剪。写不成时明确报错并返回 None ——
    **绝不假装保存成功**。计划整体为空时也不建轮次（避免占掉一个轮次号）。
    """
    learning = result.get("learning_plan") or []
    projects = result.get("project_plan") or []
    if not learning and not projects:
        print("\n⚠ 本轮没有生成任何学习/项目计划，**未创建轮次**（不记录空轮次）")
        return None

    written = store.append_round(target_job, learning, projects)
    if written.get("status") != LOOP_WRITE_OK:
        print(f"\n⚠ **本轮计划未能保存**（{written.get('status')}）：{written.get('warning')}")
        print("  任务清单不会出现在 runs/loop.json 里，本轮无法登记完成情况。")
        return None

    print(f"\n已把本轮计划保存为第 {written['round']} 轮"
          f"（{len(written.get('tasks') or [])} 项任务）")
    return store.get_round(written["round"])


def _loop_submit_completion(store, pending: list, input_fn, vocabulary=None,
                            profile_skills=None) -> None:
    """提交一个任务的完成证据：先落盘完成记录，再由阶段五评估成长。

    ``profile_skills`` 是最近一次工作流跑出来的 ``user_skills`` —— 传下去，升级预览才会
    用**和 growth_node 相同的基线**（历史 ∪ profile）。不传就退回历史基线，
    阅读时会更严格（见 :func:`show_completion_result` 的提示）。
    """
    entry = store.latest_round()
    if not entry:
        print("\n（还没有任何轮次，请先跑一次生成第一轮计划）")
        return

    show_tasks(entry)
    open_ids = [t["task_id"] for t in (entry.get("tasks") or [])
                if t.get("status") == TASK_PENDING]
    if not open_ids:
        print("\n本轮所有任务都已完成，没有可提交的任务。")
        return
    print(f"\n可提交的任务：{'、'.join(open_ids)}")

    while True:
        task_id = _ask(input_fn, "任务 ID（回车取消）：")
        if not task_id:
            print("（已取消）")
            return
        if task_id in open_ids:
            break
        print(f"  ✗ {task_id} 不在本轮待完成任务里，请重新输入")

    evidence = _ask(input_fn, "完成证据（具体做了什么、有什么产出）：")
    if not evidence:
        print("  ⚠ 没有填证据：任务仍会被记为完成，但**不会**产生任何成长成果"
              "（阶段五规定「仅凭标题不得升级」，这里也不替你编证据）。")

    result = process_task_completion(task_id, evidence, loop_store=store,
                                     vocabulary=vocabulary,
                                     profile_skills=profile_skills)
    show_completion_result(result)

    if result.get("growth_event") and not result.get("duplicate_events"):
        pending.append(result["growth_event"])
        print(f"\n已排队 {len(pending)} 条成果 —— 请求「下一轮计划」时会交给成长节点正式写入。")


def _loop_next_round(store, pending: list, input_fn) -> dict | None:
    """用最新能力与最新缺口生成下一轮计划，并正式写入成长历史。

    返回本轮工作流的 ``state``（供上层更新"最新能力表"）；用户取消时返回 ``None``。
    """
    if pending:
        print(f"\n将把 {len(pending)} 条新成果交给成长节点，由它**正式写入**成长历史后重算路线。")
    else:
        print("\n⚠ 本次会话里还没有新的完成记录 —— 生成下一轮**不会改变任何能力**，")
        print("  学习与项目路线只会基于当前能力重算。")
        show_progress(store)
        if _ask(input_fn, "仍要生成下一轮计划吗？[y/N]：").lower() not in _YES_WORDS:
            print("（已取消：不生成新轮次，也不伪造成长）")
            return None

    result, _ = run_pipeline(list(pending))
    # 工作流已经跑完：成长历史该写的都写完了，队列随即清空
    # （即使下面保存轮次失败，这些成果也已经在成长历史里，不该再喂一次）
    handed_over = len(pending)
    pending.clear()

    entry = persist_round(store, result)
    if entry is None and handed_over:
        print(f"  注意：{handed_over} 条成果**已经**由成长节点写入成长历史，"
              f"只是本轮计划没能存下来。")
    if entry:
        show_tasks(entry)
    return result


def run_growth_loop(input_fn=input, vocabulary=None) -> int:
    """成长闭环主流程：生成计划 → 展示任务 → 提交证据 → 下一轮。返回退出码。

    每一轮都由用户显式触发（菜单里的 [2]），**不会自动循环** —— 避免意外重复调用模型。
    """
    print("开始执行 LangGraph 工作流（成长闭环模式）...")
    store = get_loop_store()

    loaded = store.load()
    if not loaded.writable:
        print(f"\n⚠ 闭环记录当前不可安全写入（{loaded.status}）：{loaded.warning}")
        print("  为免覆盖或删除现有数据，本次不进入闭环。请先人工处理该文件后再试。")
        return 1

    # 新完成、还没交给成长节点的成果。
    # 启动时从 loop.json + 成长历史的指纹**重建**一次 —— 上一次会话里提交完就退出、
    # 没来得及点「下一轮计划」的成果，靠这一步才不会丢（判据见 pending_growth_events）。
    restored = pending_growth_events(store, vocabulary=vocabulary)
    for warning in restored["warnings"]:
        print(f"  提示：{warning}")
    pending: list = restored["events"]
    if pending:
        names = "、".join(t["task_id"] for t in restored["tasks"])
        print(f"\n（恢复 {len(pending)} 条「已完成但还没交给成长节点」的成果：{names}）")

    # 最近一次工作流跑出来的能力表 —— 升级预览拿它当 profile 侧基线，
    # 保证预览和下一轮 growth_node 用的是同一套能力
    session_skills = None

    rounds = loaded.data.get("rounds") or []
    if not rounds:
        print("\n（还没有任何轮次记录，先生成第一轮计划）")
        events = collect_growth_events(input_fn)
        result, _ = run_pipeline(events)
        session_skills = result.get("user_skills")
        entry = persist_round(store, result)
    else:
        latest = store.latest_round()
        print(f"\n已有 {len(rounds)} 轮记录，从第 {latest.get('round')} 轮继续。")
        entry = latest

    if entry:
        show_tasks(entry)

    while True:
        choice = _ask(input_fn, _LOOP_MENU).lower()
        if choice in ("q", "quit", "exit", "退出"):
            print("（已退出闭环：已完成的记录都在 runs/loop.json 与 runs/growth.json 里）")
            return 0
        if choice == "1":
            _loop_submit_completion(store, pending, input_fn, vocabulary, session_skills)
        elif choice == "2":
            result = _loop_next_round(store, pending, input_fn)
            if result is not None:
                session_skills = result.get("user_skills")
        elif choice == "3":
            show_progress(store)
        else:
            print(f"  ✗ 认不出的选项：{choice!r}，请填 1 / 2 / 3 / q")


if __name__ == "__main__":
    # 阶段六：闭环是**显式触发**的 —— 不带 --loop 时，下面的行为与阶段一~五完全一致。
    if "--loop" in sys.argv[1:]:
        raise SystemExit(run_growth_loop())

    print("开始执行 LangGraph 工作流...")

    # 阶段五：先收集本轮新完成的学习 / 项目成果（直接回车即跳过）
    growth_events = collect_growth_events()

    run_pipeline(growth_events)
