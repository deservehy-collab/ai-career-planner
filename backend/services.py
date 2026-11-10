"""业务编排层：把 HTTP 请求翻译成对 ``agents/`` 既有函数的调用。

本模块**不重新实现**任何六阶段规则，全部委托：

============================================  ==========================================
接口需求                                      复用的既有函数
============================================  ==========================================
任务完成 + 证据                               ``agents.growth_loop.process_task_completion``
待处理成长成果                                ``agents.growth_loop.pending_growth_events``
下一轮成长规划                                ``pending_growth_events`` + ``graph.workflow.workflow``
任务 / 轮次查询                               ``agents.loop_store.LoopStore``
成长历史查询                                  ``agents.growth_store.GrowthStore``
============================================  ==========================================

三条不可动摇的边界（对应 tests/ 里的四个 API 测试）：

1. **分析只读** —— 由 ``backend.analyze._isolated_growth_store`` 保证。
2. **任务完成只写 loop.json** —— 升级只是"只读预览"，正式落盘只发生在
   下一轮规划时由 ``growth_node`` 走正式路径写入。
3. **查询不改动磁盘** —— 待办成果查询前后，两个 store 文件逐字节不变。
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime

from agents.growth_adjust import GrowthAdjustError, void_event
from agents.goal import (
    GOAL_ACHIEVED,
    GOAL_BASIS_INSUFFICIENT,
)
from agents.growth_loop import (
    build_growth_event,
    event_fingerprint,
    load_vocabulary,
    normalize_skills,
    pending_growth_events,
    process_task_completion,
)
from agents.growth_store import (
    ENTRY_KIND_ADJUSTMENT,
    ENTRY_KIND_VERIFICATION,
    GrowthStore,
    voided_index,
)
from agents.external_sources import evidence_inventory
from agents.job_catalog import JobCatalog
from agents.job_profile import JobProfileStore
from agents.llm import (
    CHANNEL_CLOUD, active_model_name, begin_warning_capture, cloud_auth_failed,
    cloud_configured, cloud_settings, get_channel, take_warnings,
)
from agents.loop_store import (
    ATTACH_ALREADY,
    ATTACH_OK,
    COMPLETE_ALREADY,
    COMPLETE_INVALID_TASK_ID,
    COMPLETE_OK,
    COMPLETE_UNKNOWN_TASK,
    KIND_LEARNING,
    KIND_PROJECT,
    REOPEN_OK,
    STATUS_CORRUPT as LOOP_STATUS_CORRUPT,
    STATUS_EMPTY as LOOP_STATUS_EMPTY,
    STATUS_OK as LOOP_STATUS_OK,
    STATUS_SCHEMA_MISMATCH as LOOP_STATUS_SCHEMA_MISMATCH,
    STATUS_UNREADABLE as LOOP_STATUS_UNREADABLE,
    TASK_COMPLETED,
    TASK_PENDING,
    WRITE_OK,
    LoopStore,
    LoopStoreError,
)
from agents.project_knowledge import ProjectKnowledgeBase
from agents.skill_knowledge import SkillKnowledgeBase
from backend import config
from backend.analyze import jsonable, use_growth_store, workflow
from backend.jobs import current_run_canceled
from graph.workflow import build_final_plan

__all__ = [
    "Stores", "complete_task", "list_pending", "run_next_round", "list_tasks",
    "growth_history", "health", "_run_next_round", "add_manual_event",
    "run_analyze_and_save", "withdraw_pending_event", "void_growth_event",
]

#: 断线原因（供 warning 文案使用）
_NO_EVENTS_WARNING = (
    "本次没有「已完成但还没交给成长节点」的成果，未生成新一轮计划"
    "（不伪造轮次，也不伪造成长记录）"
)


@dataclass
class Stores:
    """一次请求会用到的两个存储入口（由 ``backend.deps.create_stores`` 构造）。

    两个 store 都是**无内存状态**的：每次调用重新读盘、改副本、原子写，
    所以"每个请求新建一个实例"是安全的。
    """

    growth: GrowthStore
    loop: LoopStore
    #: 档案级存储时非空；旧账号级 JSON 路径为 None。
    plan_id: str | None = None
    #: 创建档案时冻结的自述资料；下一轮用它，而不是当前可编辑资料。
    profile_snapshot: dict | None = None
    #: 档案绑定的目标岗位与招聘要求；分析/下一轮都不接受请求临时改写。
    target_job: str | None = None
    jd_text: str | None = None


# ── 任务完成（只写 loop.json）────────────────────────────────────────────

# 进程内互斥锁：保护 ``loop.json`` 的**全部**读—改—写。
#
# 为什么需要
# ----------
# ``LoopStore`` 的每次写入都是「``load()`` 读全量 → 深拷贝改副本 → ``_write()`` 原子
# 替换」。``_write`` 的"原子"只保证**文件替换**不半途而废，**不保证两次读—改—写之间
# 不互相覆盖**：两个线程各自读到同一份基线，后写的那个会把先写的整条丢掉（丢失更新）。
# 存储层自身没有任何锁（``agents/loop_store.py`` 不 import threading），所以在**服务层**
# 补这把锁。
#
# 保护范围：按"写入方"划，不按"接口名"划
# ----------------------------------------
# 会写 ``loop.json`` 的路径有四条，**四条都在锁内**，缺一条就前功尽弃：
#
# 1. :func:`complete_task` —— ``mark_completed``（HTTP 请求线程，会真正并发）
# 2. :func:`add_manual_event` —— ``append_round`` + ``mark_completed`` 两段
# 3. :func:`_run_next_round` —— 下一轮规划末尾的 ``append_round``（后台工作线程）
# 4. :func:`run_analyze_and_save` —— 首轮任务的 ``append_round``（后台工作线程）
#
# 临界区只包住**读写本身**（毫秒级），绝不把 ``workflow.invoke`` 那几分钟的模型推理
# 放进去 —— 否则一次「生成下一轮」会把同时提交的任务完成堵死。临界区里只做
# 纯规则计算与文件读写：升级预览读 YAML 词表与技能库，**不调模型**。
#
# 边界（不要误用）
# ----------------
# - **不是跨进程锁**：只对本进程内的线程有效。服务仍必须 ``--workers 1``，
#   多 worker / 同时跑 ``main.py`` CLI 时这把锁形同虚设（根治要靠业务数据进数据库）。
# - **不保护"读到的基线有多新"**：``_run_next_round`` 开头的 ``pending_growth_events``
#   与推理之间可能有新完成的任务落盘，那条成果**不会**丢 —— 指纹没进成长历史，
#   下一次「生成下一轮」仍会排队（见 agents/growth_loop.py 的判据）。
# - 用 ``RLock`` 而非 ``Lock``：同一线程重入不会自锁死，互斥性不受影响。
_STORE_WRITE_LOCK = threading.RLock()

# 成长历史的写锁：正常记账（next-round 里的 growth_node）与用户作废（history/void）
# 都会读—改—写 growth.json，必须串行。它不保护 loop.json，所以不会把任务完成
# 堵在一次几分钟的模型推理后面。
_GROWTH_WRITE_LOCK = threading.RLock()


def _persist_job_profile(result: dict, store: JobProfileStore, target_job: str,
                         jd_text: str | None) -> str | None:
    """保存本次**新生成**的岗位画像；缓存命中的不重复写。

    返回一条警告文案（保存失败时），成功返回 ``None``。画像只是分析用的
    中间成果，保存失败不该让整轮计划失败，所以这里只报告、不抛出。
    """
    profile = result.get("job_profile")
    if not profile or result.get("job_profile_origin") != "generated":
        return None
    try:
        store.save(
            profile,
            target_job=target_job,
            jd_text=jd_text,
            warnings=result.get("job_profile_warnings") or [],
        )
    except Exception as exc:  # noqa: BLE001 - 缓存失败不阻断业务保存
        return f"岗位画像缓存未保存（{type(exc).__name__}: {exc}）"
    return None


def _load_job_profile(state: dict, store: JobProfileStore, target_job: str,
                      jd_text: str | None) -> None:
    """把当前用户缓存里的画像注入 state；读不到就留给 job_node 生成。"""
    cached = store.load(target_job, jd_text)
    if cached.found:
        state["job_profile"] = cached.profile
        state["job_profile_warnings"] = list(cached.warnings)
    elif cached.warning:
        state["job_profile_warnings"] = [cached.warning]


def _snapshot_payload(result: dict, goal: dict, jd_text: str | None) -> dict:
    """把一轮的岗位依据与结局固化成快照，随轮次一起写进 ``loop.json``。

    下一轮只认这份快照：目标、原文、岗位要求与依据强度都不再让模型重新推断。
    没有原文时 ``jd_text`` 是空串，读取方据此区分"这一轮本来就没粘原文"。
    """
    return {
        "jd_text": str(jd_text or "").strip(),
        "job_evidence": list(result.get("job_evidence") or []),
        "retrieval_status": result.get("retrieval_status"),
        "retrieval_diagnostics": dict(result.get("retrieval_diagnostics") or {}),
        "job_requirements": list(result.get("job_requirements") or []),
        "job_requirements_basis": (
            result.get("job_requirements_basis")
            or result.get("requirement_basis")
            or "model_only"
        ),
        "job_requirements_grounded": bool(result.get("job_requirements_grounded")),
        "goal": dict(goal or {}),
        "goal_status": (goal or {}).get("status"),
        "goal_evaluated_at": datetime.now().isoformat(timespec="seconds"),
    }


def _apply_round_snapshot(state: dict, latest: dict) -> dict | None:
    """把上一轮快照注入下一轮 state；没有快照时返回 ``None``（退回旧口径）。"""
    snapshot = latest.get("snapshot")
    if not isinstance(snapshot, dict):
        return None

    requirements = snapshot.get("job_requirements")
    if isinstance(requirements, list) and requirements:
        state["snapshot_job_requirements"] = [
            dict(item) for item in requirements if isinstance(item, dict)
        ]
        state["snapshot_job_requirements_basis"] = str(
            snapshot.get("job_requirements_basis") or ""
        )
        state["snapshot_job_requirements_grounded"] = bool(
            snapshot.get("job_requirements_grounded")
        )

    evidence = snapshot.get("job_evidence")
    if isinstance(evidence, list):
        state["snapshot_job_evidence"] = [
            dict(item) for item in evidence if isinstance(item, dict)
        ]
        state["snapshot_retrieval_status"] = snapshot.get("retrieval_status")
        state["snapshot_retrieval_diagnostics"] = dict(
            snapshot.get("retrieval_diagnostics") or {}
        )

    bound_jd = str(snapshot.get("jd_text") or "").strip()
    if bound_jd:
        state["jd_text"] = bound_jd
    return snapshot


def complete_task(
    stores: Stores,
    task_id: str,
    evidence: str | None,
    profile_skills: list[dict] | None = None,
    note: str | None = None,
) -> dict:
    """登记任务完成并给出一份**不落盘**的升级预览。

    委托 :func:`agents.growth_loop.process_task_completion`，签名逐字对应。
    返回体里 ``completion_status`` 用于映射 HTTP 状态码：

    - ``completed`` / ``already_completed`` / 存储写失败 → 200（用 body 说明真相）
    - ``unknown_task`` → 404
    - ``invalid_task_id`` → 422

    整段调用在 :data:`_STORE_WRITE_LOCK` 临界区内执行：并发提交**不同**任务时
    逐个串行落盘，不会互相覆盖；重复提交**同一**任务仍由 ``mark_completed``
    返回 ``already_completed``，首次证据不被覆盖。

    ``evidence`` 为空表示自述完成，``note`` 另行保存 —— 两者都不并入对方。
    """
    with _STORE_WRITE_LOCK:
        return process_task_completion(
            task_id,
            evidence=evidence or "",
            note=note,
            loop_store=stores.loop,
            growth_store=stores.growth,
            profile_skills=profile_skills,
        )


def completion_http_status(result: dict) -> int:
    """把 ``process_task_completion`` 的状态映射成 HTTP 状态码。

    ``completion_failed``（存储损坏/写盘失败）**仍返回 200** —— 接口本身跑通了，
    是业务结果是失败态；tests/test_growth_loop_api.py 明确要求此时
    ``completion_written is False`` 而不是伪装成 5xx 或 200 成功。
    """
    status = result.get("completion_status")
    if status == COMPLETE_UNKNOWN_TASK:
        return 404
    if status == COMPLETE_INVALID_TASK_ID:
        return 422
    return 200


#: :func:`clear_tasks` 的三种结果：已清空 / 需要用户确认 / 存储拒绝。
CLEAR_OK = "cleared"
CLEAR_NEEDS_CONFIRM = "needs_confirmation"
CLEAR_REFUSED = "refused"


def clear_tasks(stores: Stores, drop_pending_events: bool = False) -> dict:
    """清空**当前用户**的任务清单：先备份原文件，再重置成空。只动 ``loop.json``。

    为什么先问一句"未记账成果怎么办"
    --------------------------------
    打卡攒下的成长成果是先躺在 ``loop.json`` 的完成记录里的，要等用户点
    「生成下一轮」才正式进成长历史（见 :func:`agents.growth_loop.pending_growth_events`）。
    直接清空会把这些**还没记账的成果**一起删掉 —— 那是真实进度，不该被一次
    "把界面清干净"顺手冲掉。所以默认拒绝，只有前端带着明确确认才丢。

    返回 ``{status, rounds_removed, tasks_removed, backup, pending_events, warning}``；
    ``backup`` 是备份文件名，用户随时可以照着它把清单还原。
    """
    pending = pending_growth_events(loop_store=stores.loop, growth_store=stores.growth)
    events = pending.get("events") or []

    # 成长历史读不出来时，判不了"哪些成果已经记账" —— 此时清空可能把已记账的
    # 记录连成无源升级，也可能顺手删掉未记账的真实成果，所以一律拒绝。
    if pending.get("restored") is False:
        return {
            "status": CLEAR_REFUSED,
            "rounds_removed": 0,
            "tasks_removed": 0,
            "backup": None,
            "pending_events": 0,
            "warning": (
                "成长历史当前不可读，无法判断任务里的成果是否已经记账；"
                "为避免误删，本次未清空任务清单。请先修复成长历史文件再试。"
            ),
        }

    if events and not drop_pending_events:
        return {
            "status": CLEAR_NEEDS_CONFIRM,
            "rounds_removed": 0,
            "tasks_removed": 0,
            "backup": None,
            "pending_events": len(events),
            "warning": "有成果还没进成长历史，清空会一起丢掉，需要明确确认",
        }

    with _STORE_WRITE_LOCK:
        result = stores.loop.clear()

    status = result.get("status")
    return {
        "status": CLEAR_OK if status == "written" else CLEAR_REFUSED,
        "rounds_removed": result.get("rounds_removed", 0),
        "tasks_removed": result.get("tasks_removed", 0),
        "backup": result.get("backup"),
        "pending_events": len(events) if drop_pending_events else 0,
        "warning": result.get("warning"),
    }


# ── 完成后补凭证（只翻证据类型，不新增升级）──────────────────────────────
def attach_task_evidence(
    stores: Stores,
    task_id: str,
    evidence: str,
) -> dict:
    """给一条已完成任务补凭证，并把已入账的自述升级翻成有凭证。

    - 任务还没完成 → ``status=not_completed``（路由映射 409）。
    - 相同凭证已经记过 → 不重复写任务，也不重复追加验证记录。
    - 成长历史里已有这条成果 → 追加一条 ``verification`` 记录，原记录一条不改；
      没有历史（还没跑「生成下一轮」）→ 只翻任务证据类型，等下一轮正常入账。
    """
    with _STORE_WRITE_LOCK:
        attached = stores.loop.attach_evidence(task_id, evidence=evidence)

    status = attached.get("status")
    result = {
        "task_id": task_id,
        "status": status,
        "evidence_kind": None,
        "verified": False,
        "verification_written": False,
        "pending_next_round": False,
        "task": attached.get("task"),
        "reason": attached.get("warning"),
    }
    if status not in (ATTACH_OK, ATTACH_ALREADY):
        return jsonable(result)

    task = attached.get("task") or {}
    result["evidence_kind"] = str(task.get("evidence_kind") or "growth")

    with _GROWTH_WRITE_LOCK:
        loaded = stores.growth.load()
        if not loaded.writable:
            result["pending_next_round"] = True
            result["reason"] = (
                loaded.warning or "成长记录当前不可安全写入；凭证已记在任务上，下一轮再入账"
            )
            return jsonable(result)

        existing = stores.growth.find_verification(task_id, loaded.data)
        if existing is not None:
            result["verified"] = True
            result["reason"] = "这条任务此前已经补过凭证"
            return jsonable(result)

        entry = stores.growth.build_verification_entry(
            loaded.data,
            task_id=task_id,
            title=str(task.get("title") or ""),
            skills=list(task.get("skills") or []),
            evidence=str(evidence or "").strip(),
            target_job=str(task.get("target_job") or ""),
        )
        if entry is None:
            result["pending_next_round"] = True
            result["reason"] = (
                "成长历史里还没有这条成果；凭证已记在任务上，"
                "点「生成下一轮」时会按有凭证入账"
            )
            return jsonable(result)
        if not entry.get("changed_skills"):
            result["verified"] = True
            return jsonable(result)

        written = stores.growth.append(entry)
        if written.get("status") != WRITE_OK:
            result["pending_next_round"] = True
            result["reason"] = written.get("warning") or "验证记录未写入成长历史"
            return jsonable(result)

    result["verified"] = True
    result["verification_written"] = True
    return jsonable(result)



def add_manual_event(
    stores: Stores,
    kind: str,
    title: str,
    skills: list[str] | None,
    evidence: str | None,
    target_job: str | None = None,
) -> dict:
    """手动补录一条成长成果：写成一个"已完成任务"进 ``loop.json``。

    目的
    ----
    新用户空系统时没有任何轮次/任务，也就无法通过任务提交产生待处理成果，
    更无法点「生成下一轮」。本函数让用户可以**直接录入**一条自己学过/做过的
    成果：把它实时写成一个``已完成``的任务挂在 ``loop.json`` 上，随后
    ``pending_growth_events`` 就能把它重建为待处理成果，从而打破死锁。

    实现方式
    --------
    复用 ``LoopStore.append_round`` 新开一轮（只含这一条任务），再
    ``LoopStore.mark_completed`` 把它标成完成并写入证据 —— 与正常任务完成
    走的是同一套落盘路径，之后升级判定、去重逻辑全部照常。

    ``target_job`` 缺省时沿用最近一轮的 ``target_job``（都拿不到则为空字符串）。

    在 :data:`_STORE_WRITE_LOCK` 临界区内执行：append + complete 是两段
    独立的读—改—写，必须串行，否则并发请求会互相覆盖。
    """
    if kind not in (KIND_LEARNING, KIND_PROJECT):
        return {"added": False, "reason": f"未知成果类型：{kind!r}（可选 {KIND_LEARNING}/{KIND_PROJECT}）"}

    title = (title or "").strip()
    if not title:
        return {"added": False, "reason": "成果标题不能为空"}
    evidence = (evidence or "").strip()
    skills = [str(s).strip() for s in (skills or []) if str(s or "").strip()]

    target = (target_job or "").strip()
    if not target:
        latest = stores.loop.latest_round() or {}
        target = str(latest.get("target_job") or "")

    # 按类型构造只含这一条任务的计划：learning 用 topic/covers，project 用 name/covers_gaps
    if kind == KIND_LEARNING:
        learning_plan = [{"topic": title, "covers": skills}]
        project_plan = []
    else:
        learning_plan = []
        project_plan = [{"name": title, "covers_gaps": skills}]

    with _STORE_WRITE_LOCK:
        appended = stores.loop.append_round(target, learning_plan, project_plan)
        if appended.get("status") != WRITE_OK:
            return {
                "added": False,
                "reason": appended.get("warning") or "未能写入任务记录",
                "store_status": appended.get("status"),
            }
        tasks = appended.get("tasks") or []
        if not tasks:
            return {"added": False, "reason": "任务创建后未返回任务条目"}
        task = tasks[0]
        done = stores.loop.mark_completed(task["task_id"], evidence=evidence)
        if done.get("status") != COMPLETE_OK:
            return {
                "added": False,
                "reason": done.get("warning") or "任务标完成失败",
                "task_id": task["task_id"],
            }

    return {
        "added": True,
        "round": appended.get("round"),
        "task_id": task["task_id"],
        "task": jsonable(task),
    }


# ── 分析并保存第一轮任务（根治冷启动死锁）────────────────────────────────
def _canceled_analysis_result(
    target: str, latest_before: dict | None, analysis: dict, goal: dict, *,
    new_plan: bool,
) -> dict:
    """被用户停止的分析：把已算出的内容交回去，但明确说不落盘。"""
    return {
        "ok": True,
        "canceled": True,
        "new_plan": new_plan,
        "round_created": False,
        "round_no": None,
        "existing_round_no": latest_before.get("round") if latest_before else None,
        "target_job": target,
        "learning_plan": list(analysis.get("learning_plan") or []),
        "project_plan": list(analysis.get("project_plan") or []),
        "analysis": analysis,
        "goal": goal,
        "goal_status": goal.get("status"),
        "warning": "分析已停止，本次结果没有保存。",
    }


def run_analyze_and_save(stores: Stores, payload) -> dict:
    """跑一次分析；首次分析或显式开始新计划时，把计划存成新的一轮任务。

    两种模式
    --------
    - **预览**（默认）：已有轮次时只返回分析结果，**不新建轮次、不改任务清单**，
      并明确告诉前端"这次只是预览；要另存为新计划请点「开始新计划」"。
    - **新计划**（``payload.new_plan=True``）：无论是否已有轮次，都把本次产出的
      学习/项目计划追加成新的一轮；旧轮次与旧任务原样保留，不清空任何东西。

    每轮都会写入 ``snapshot``（目标、JD 原文、岗位要求、依据强度、结局），
    下一轮据此沿用同一份岗位依据。顶层 ``latest_goal`` 同步更新，刷新页面后
    结局仍然可见。

    注意
    ----
    这里不写 ``growth.json``（成长历史仍只由「生成下一轮」的 ``growth_node``
    写），只是把分析产出的**学习计划 / 项目计划**落成任务轮次。若已有轮次而
    未显式要求新计划，则**不**追加，避免污染既有轮次。

    返回 ``{ok, new_plan, round_created, round_no, existing_round_no, target_job,
    learning_plan, project_plan, analysis (只读分析结果), goal, goal_status, warning}``。

    分析基线是**当前用户**的 ``stores.growth``（见 ``backend.analyze.run_workflow``
    的 ``baseline_store``）—— 隔离复制看到的历史与这位用户真实一致，而不是
    数据根目录下的旧全局文件。
    """
    from backend.analyze import run_workflow

    loop = stores.loop
    new_plan = bool(getattr(payload, "new_plan", False))

    if stores.plan_id:
        # 档案快照是这次分析的唯一资料/岗位依据：请求体里的可编辑画像和
        # 目标不能悄悄改动旧档案。新建档案时前端已经把这些值写成快照。
        if stores.profile_snapshot is not None:
            payload.user_profile = type(payload.user_profile).model_validate(
                stores.profile_snapshot
            )
        if stores.target_job:
            payload.target_job = stores.target_job
        payload.jd_text = stores.jd_text or None
    latest_before = loop.latest_round()

    if latest_before is not None and not new_plan:
        # 已有轮次：保持只读分析语义，不重复建轮次。这里也不生成新画像，
        # 只允许读已有缓存，避免"本来不写盘"的分支偷偷多一次模型调用。
        analysis = run_workflow(
            payload, baseline_store=stores.growth, enable_job_profile=False
        )
        goal = (analysis.get("final_plan") or {}).get("goal") or {}
        round_no = latest_before.get("round")
        return {
            "ok": True,
            "new_plan": False,
            "round_created": False,
            "round_no": None,
            "existing_round_no": round_no,
            "target_job": getattr(payload, "target_job", ""),
            "learning_plan": analysis.get("learning_plan") or [],
            "project_plan": analysis.get("project_plan") or [],
            "analysis": analysis,
            "goal": goal,
            "goal_status": goal.get("status"),
            "warning": (
                f"当前已有第 {round_no} 轮计划，本次分析只做预览，任务清单没有改动。"
                "如果想把这一版分析另存为新的一轮计划，请点「开始新计划」。"
            ),
        }

    target = str(getattr(payload, "target_job", "") or "").strip()

    # 明确开始新计划 = 用户已经放弃上一轮未保存的恢复机会；先清掉恢复标记，
    # 免得新计划落盘后又被旧标记补存出一条重复轮次。
    pending_clear_warning = None
    if new_plan:
        with _STORE_WRITE_LOCK:
            cleared = loop.clear_pending_next_round()
        if cleared.get("status") not in ("written", "not_needed"):
            pending_clear_warning = cleared.get("warning")

    analysis = run_workflow(payload, baseline_store=stores.growth)
    goal = (analysis.get("final_plan") or {}).get("goal") or {}
    if current_run_canceled():
        # 用户在这次推理途中按了「只停止分析」：结果只能丢，档案一个字节都不动。
        return _canceled_analysis_result(
            target, latest_before, analysis, goal, new_plan=new_plan,
        )
    profile_warning = _persist_job_profile(
        analysis,
        JobProfileStore(stores.growth.directory),
        target,
        getattr(payload, "jd_text", None),
    )
    learning_plan = list(analysis.get("learning_plan") or [])
    project_plan = list(analysis.get("project_plan") or [])

    round_created = False
    round_no = None
    warning = pending_clear_warning

    if learning_plan or project_plan:
        snapshot = _snapshot_payload(analysis, goal, getattr(payload, "jd_text", None))
        with _STORE_WRITE_LOCK:
            # 锁内**重读**再决定：上面那几分钟推理里，别的写入方可能已经建了轮次
            # （用户同时提交手动补录、或另一条分析恰好落盘）。预览分支用的
            # latest_round 是推理**之前**的快照，不重读就会多建一轮重复任务。
            # 显式新计划不受这条限制 —— 它就是要另起一轮。
            if not new_plan and loop.latest_round() is not None:
                warning = "分析期间已存在轮次，本次未重复创建任务（任务已在「学习与项目」页）。"
            elif current_run_canceled():
                # 推理结束到进锁之间被停止：同样不许落盘。
                warning = "分析已停止，本次结果没有保存。"
            else:
                written = loop.append_round(
                    target,
                    learning_plan,
                    project_plan,
                    snapshot=snapshot,
                    source="new_plan" if new_plan else "analyze",
                    latest_goal=goal if goal.get("status") else None,
                    clear_pending=new_plan,
                )
                if written.get("status") == WRITE_OK:
                    round_created = True
                    round_no = written.get("round")
                else:
                    warning = written.get("warning") or "计划未能保存"
    else:
        # 结局分支不是失败：目标已达成 / 依据不足无法确认，都不该生成空任务。
        if goal.get("status") == GOAL_ACHIEVED:
            warning = None
        elif goal.get("status") == GOAL_BASIS_INSUFFICIENT:
            warning = goal.get("message")
        else:
            warning = "本次分析没有生成学习/项目计划，未创建任务轮次。"

    if profile_warning:
        warning = f"{warning}；{profile_warning}" if warning else profile_warning

    return {
        "ok": True,
        "new_plan": new_plan,
        "round_created": round_created,
        "round_no": round_no,
        "existing_round_no": latest_before.get("round") if latest_before else None,
        "target_job": target,
        "learning_plan": learning_plan,
        "project_plan": project_plan,
        "analysis": analysis,
        "goal": goal,
        "goal_status": goal.get("status"),
        "warning": warning,
    }


# ── 待处理成长成果（纯只读）──────────────────────────────────────────────
def list_pending(stores: Stores) -> dict:
    """列出"已完成、但还没由 ``growth_node`` 处理过"的成果。**不写任何文件。**"""
    return jsonable(
        pending_growth_events(loop_store=stores.loop, growth_store=stores.growth)
    )


def _task_fingerprint(task: dict) -> str | None:
    """按阶段五同一口径算任务的成果指纹；构不成成果时返回 None。"""
    built = build_growth_event(task)
    if built.get("event") is None:
        return None
    vocabulary, _ = load_vocabulary()
    canonical, _ = normalize_skills(built["event"].get("skills") or [], vocabulary)
    if not canonical:
        return None
    return event_fingerprint(
        built["event"].get("type"), built["event"].get("title"), canonical
    )


def withdraw_pending_event(stores: Stores, task_id: str, round_no: int | None = None) -> dict:
    """撤回一条**尚未计入成长历史**的完成记录，把任务退回待完成。

    只允许撤回仍在待处理列表里的成果。已入账的成果必须走
    :func:`void_growth_event`（追加作废调整并重算），否则历史里会留下一条
    找不到来源的升级。
    """
    with _STORE_WRITE_LOCK:
        loaded = stores.loop.load()
        task = stores.loop.find_task(task_id, data=loaded.data)
        if task is None:
            return {"withdrawn": False, "reason": f"没有找到任务 {task_id}"}
        if task.get("status") != TASK_COMPLETED:
            return {"withdrawn": False, "reason": "这条任务还没完成，不在待处理成果里"}

        pending = pending_growth_events(
            loop_store=stores.loop, growth_store=stores.growth
        )
        if not pending.get("restored", False):
            return {
                "withdrawn": False,
                "reason": pending.get("warnings", ["成长历史不可读，无法确认这条成果是否已入账"])[-1],
            }

        if not any(item.get("task_id") == task_id for item in pending.get("tasks") or []):
            fingerprint = _task_fingerprint(task)
            if fingerprint and fingerprint in stores.growth.seen_fingerprints():
                reason = "这条成果已经记入成长历史，不能从待处理里撤回；请在成长历史里作废"
            elif fingerprint and any(
                item.get("fingerprint") == fingerprint for item in pending.get("tasks") or []
            ):
                reason = "同一成果已由另一条任务排队，撤回那条即可"
            else:
                skipped = next(
                    (item for item in pending.get("skipped") or []
                     if item.get("task_id") == task_id),
                    None,
                )
                reason = (
                    f"这条任务构不成可提交的成果：{skipped.get('reason')}"
                    if skipped else "这条任务当前不在待处理成果里"
                )
            return {"withdrawn": False, "reason": reason}

        reopened = stores.loop.reopen(task_id, round_no=round_no)
        if reopened.get("status") != REOPEN_OK:
            return {
                "withdrawn": False,
                "reason": reopened.get("warning") or "未能撤回这条完成记录",
                "store_status": reopened.get("status"),
            }
        return {
            "withdrawn": True,
            "task_id": task_id,
            "round": reopened.get("round"),
            "task": reopened.get("task"),
        }


def void_growth_event(
    stores: Stores, entry_index: int, event_index: int, reason: str
) -> dict:
    """作废一条已入账成果：追加调整记录并重算能力表。原记录一个字段都不改。"""
    with _GROWTH_WRITE_LOCK:
        try:
            return jsonable(void_event(stores.growth, entry_index, event_index, reason))
        except GrowthAdjustError as exc:
            return {"voided": False, "reason": str(exc)}


# ── 下一轮成长规划（正常记账入口）──────────────────────────────────────────
def _next_round_blocked(warning: str, outcome: str, target: str,
                        event_count: int, warnings: list[str]) -> dict:
    """下一轮在跑模型之前就被挡下（目标/原文与当前计划不一致）时的返回体。"""
    return {
        "ok": False,
        "round_created": False,
        "round_no": None,
        "history_written": False,
        "history_write_status": None,
        "growth_outcome": outcome,
        "growth_upgraded": False,
        "events_handed_over": 0,
        "goal": None,
        "goal_status": None,
        "final_plan": None,
        "target_job": target,
        "learning_plan": [],
        "project_plan": [],
        "user_skills": [],
        "skill_updates": [],
        "growth_diff": {},
        "pending_events": event_count,
        "warning": warning,
        "warnings": list(warnings),
        "recovered": False,
        "plan_snapshot_round": None,
        "requirements_reused": False,
    }


def _run_next_round(
    stores: Stores,
    user_profile: dict | None = None,
    target_job: str | None = None,
    jd_text: str | None = None,
) -> dict:
    """用最新能力与最新缺口生成下一轮计划，并**正式写入成长历史**。

    这是正常记账路径；用户作废走 :func:`void_growth_event`（追加调整记录）。

    计划快照
    --------
    每一轮落盘时都会保存 ``snapshot``（目标、JD 原文、岗位要求、依据强度、结局）。
    下一轮**只认这份快照**：岗位要求不再让模型重新拆解，避免同一目标下门槛
    悄悄漂移；请求里带来的 target_job / jd_text 只有与快照一致才接受，不一致
    直接拒绝 —— 换岗位或换原文必须先到分析页「开始新计划」。

    断点恢复
    --------
    工作流开始前先写 ``pending_next_round``；算完后再把计划补进标记。若轮次落盘
    失败，下次调用会**直接补存**这份已算好的计划（不重复跑模型，也不重复记账），
    之后才清标记。没有算完计划的中断则按标记里的事件重放一次。

    流程（与 ``main.py --loop`` 的菜单 [2] 同一条业务路径）::

        pending_growth_events 重建待办
          → 空：直接返回，**不跑模型、不建轮次**（不伪造成长）
          → 非空：把待办当 growth_events 重跑工作流
                   growth_node 走正式路径写 runs/growth.json 并升级能力
                   → 用新计划写一轮 runs/loop.json（带快照与结局）

    返回字段与 tests/test_growth_next_round.py 逐字段对应：
    ``ok`` / ``round_created`` / ``round_no`` / ``history_written`` /
    ``history_write_status`` / ``growth_outcome`` / ``growth_upgraded`` / ``warning``。

    ``user_profile`` 缺省时退化为空画像 —— ``profile_node`` 会明确报错，
    该次 run 被 TaskManager 标成 failed，**不会伪造出一轮计划**。
    """
    loop = stores.loop
    growth = stores.growth

    # 档案级存储：下一轮必须用创建时冻结的自述资料，不能拿现在可编辑的资料
    # 去回灌旧档案。旧账号级路径没有快照，沿用调用方传入值。
    if stores.profile_snapshot is not None:
        user_profile = dict(stores.profile_snapshot)

    restored = pending_growth_events(loop_store=loop, growth_store=growth)
    events = list(restored.get("events") or [])
    warnings = [str(item) for item in (restored.get("warnings") or [])]

    latest = loop.latest_round() or {}
    latest_no = latest.get("round")
    snapshot = latest.get("snapshot") if isinstance(latest.get("snapshot"), dict) else None
    plan_target = str(latest.get("target_job") or "").strip()
    requested_target = str(target_job or "").strip()
    requested_jd = str(jd_text or "").strip()

    if plan_target and requested_target and requested_target != plan_target:
        return _next_round_blocked(
            f"当前计划绑定的是「{plan_target}」，与请求里的「{requested_target}」不一致。"
            "同一轮不能换目标岗位；请先在「岗位分析」页用新岗位开始新计划。",
            "target_mismatch", plan_target, len(events), warnings,
        )

    if snapshot is not None:
        bound_jd = str(snapshot.get("jd_text") or "").strip()
        if requested_jd and requested_jd != bound_jd:
            return _next_round_blocked(
                "这轮计划已经绑定了保存时的招聘要求原文，请求里的原文与它不一致；"
                "换原文请先在「岗位分析」页开始新计划。",
                "jd_mismatch", plan_target or requested_target, len(events), warnings,
            )
        resolved_target = plan_target or requested_target
    else:
        bound_jd = requested_jd
        resolved_target = requested_target or plan_target

    # ── 上次没存下来的计划：直接补存，不重跑模型 ─────────────────────────
    recovery = loop.pending_next_round()
    if recovery:
        plans = recovery.get("plans") if isinstance(recovery.get("plans"), dict) else None
        if plans:
            rec_target = str(recovery.get("target_job") or resolved_target or "").strip()
            rec_goal = plans.get("goal") if isinstance(plans.get("goal"), dict) else {}
            rec_learning = list(plans.get("learning_plan") or [])
            rec_project = list(plans.get("project_plan") or [])
            rec_snapshot = plans.get("snapshot") if isinstance(plans.get("snapshot"), dict) else None
            with _STORE_WRITE_LOCK:
                written = loop.append_round(
                    rec_target,
                    rec_learning,
                    rec_project,
                    snapshot=rec_snapshot,
                    source="next_round",
                    latest_goal=rec_goal or None,
                    clear_pending=True,
                )
            recovered_fields = {
                "history_written": bool(plans.get("history_written")),
                "history_write_status": plans.get("history_write_status"),
                "growth_outcome": plans.get("growth_outcome") or "recovered",
                "growth_upgraded": bool(plans.get("growth_upgraded")),
                "events_handed_over": len(recovery.get("events") or []),
                "goal": rec_goal,
                "goal_status": rec_goal.get("status"),
                "final_plan": plans.get("final_plan"),
                "target_job": rec_target,
                "learning_plan": rec_learning,
                "project_plan": rec_project,
                "recovered": True,
            }
            if written.get("status") == WRITE_OK:
                warnings.append(
                    f"上次未保存的第 {written.get('round')} 轮计划已补存成功（未重复跑模型）。"
                )
                return {
                    "ok": True,
                    "round_created": True,
                    "round_no": written.get("round"),
                    **recovered_fields,
                    "user_skills": [],
                    "skill_updates": [],
                    "growth_diff": {},
                    "job_profile": None,
                    "job_profile_origin": None,
                    "job_profile_warnings": [],
                    "warning": None,
                    "warnings": warnings,
                    "pending_next_round": False,
                }
            return {
                "ok": False,
                "round_created": False,
                "round_no": None,
                **recovered_fields,
                "warning": (
                    "上次未保存的计划这次仍未存下，恢复标记保留着，可以稍后重试："
                    f"{written.get('warning') or written.get('status')}"
                ),
                "warnings": warnings,
                "pending_next_round": True,
            }

        # 工作流在算完之前就中断：按标记里的事件重试；已入账的成果会被指纹去重。
        if not events:
            events = [
                item for item in (recovery.get("events") or []) if isinstance(item, dict)
            ]
            if events:
                warnings.append(
                    "上次生成下一轮在保存前中断，本次按当时的事件重试；"
                    "已入账的成果不会重复升级。"
                )

    # 没有待办成果：本轮不改变任何能力，也就没有必要跑模型或新建轮次。
    if not events:
        # 成长历史读不出来时"没有待办"只是判不了，不能说成"确实没有成果"。
        warning = _NO_EVENTS_WARNING
        if restored.get("restored") is False:
            warning = (
                "成长历史当前不可读，无法判断哪些成果已经记账；"
                "本次没有生成下一轮，也没有改动任何记录。"
            )
        return {
            "ok": True,
            "round_created": False,
            "round_no": None,
            "history_written": False,
            "history_write_status": None,
            "growth_outcome": "no_events",
            "growth_upgraded": False,
            "events_handed_over": 0,
            "goal": None,
            "goal_status": None,
            "final_plan": None,
            "warning": warning,
            "warnings": warnings,
            "recovered": False,
            "pending_next_round": bool(recovery),
        }

    state = {
        "user_profile": dict(user_profile or {}),
        "target_job": resolved_target,
        "growth_events": events,
    }
    # 下一轮沿用上一轮快照：岗位要求、依据与原文都按保存时那一份，不再重新推断。
    if snapshot is not None:
        applied = _apply_round_snapshot(state, latest)
        if applied is not None:
            warnings.append(
                f"本轮沿用第 {latest_no} 轮保存的目标、招聘要求与岗位要求，未重新推断。"
            )
    if bound_jd:
        state["jd_text"] = bound_jd

    # 有缓存就沿用同一份岗位画像；没有才让 job_node 生成。无论哪种，本轮结束
    # 都会把新生成的画像写进当前用户目录（这是一条会写盘的保存路径）。
    profile_store = JobProfileStore(growth.directory)
    _load_job_profile(state, profile_store, resolved_target, bound_jd or None)
    state["enable_job_profile"] = True

    # 先落恢复标记：万一轮次写盘失败，下次还能把这次算好的计划补存回来。
    marker = {
        "target_job": resolved_target,
        "jd_text": bound_jd,
        "events": events,
        "source_round": latest_no,
        "history_written": False,
        "history_write_status": None,
        "growth_outcome": None,
        "growth_upgraded": False,
    }
    with _STORE_WRITE_LOCK:
        marked = loop.set_pending_next_round(marker)
    if marked.get("status") != WRITE_OK:
        warnings.append(
            "恢复标记未能写入"
            f"（{marked.get('warning') or marked.get('status')}）；"
            "若本轮计划保存失败，将无法一键补存。"
        )

    # 关键：把工作流内部的 growth_node 指向**注入的** store，
    # 否则它会写默认 runs/，注入就形同虚设。写锁覆盖整段推理：期间如果有
    # 用户作废记录，宁可让它排队，也不能和 growth_node 的读—改—写互相覆盖。
    begin_warning_capture()
    try:
        with _GROWTH_WRITE_LOCK:
            with use_growth_store(growth):
                result = workflow.invoke(state)
    except Exception:
        take_warnings()
        raise
    warnings.extend(take_warnings())

    profile_warning = _persist_job_profile(
        result, profile_store, resolved_target, bound_jd or None
    )
    if profile_warning:
        warnings.append(profile_warning)

    final_plan = result.get("final_plan") or build_final_plan(result)
    goal = final_plan.get("goal") or {}

    diff = result.get("growth_diff") or {}
    history_written = bool(diff.get("history_written"))
    history_write_status = diff.get("history_write_status")
    growth_outcome = diff.get("outcome")
    growth_upgraded = bool(result.get("skill_updates"))

    learning_plan = list(result.get("learning_plan") or [])
    project_plan = list(result.get("project_plan") or [])
    snapshot_payload = _snapshot_payload(result, goal, bound_jd)

    round_created = False
    round_no = None
    warning = None

    if not learning_plan and not project_plan:
        if goal.get("status") == GOAL_ACHIEVED:
            warning = None
        elif goal.get("status") == GOAL_BASIS_INSUFFICIENT:
            warning = goal.get("message")
        else:
            warning = "本轮没有生成任何学习/项目计划，未创建轮次（不记录空轮次）"
        # 结局仍要持久化：刷新页面后前端还要能显示"已达成/依据不足"。
        if latest_no is not None:
            with _STORE_WRITE_LOCK:
                saved_goal = loop.record_goal(
                    goal, round_no=latest_no, target_job=resolved_target
                )
            if saved_goal.get("status") != WRITE_OK:
                warnings.append(
                    "本轮结局未能保存到任务清单"
                    f"（{saved_goal.get('warning') or saved_goal.get('status')}）"
                )
        with _STORE_WRITE_LOCK:
            loop.clear_pending_next_round()
    else:
        # 把算好的计划补进恢复标记；轮次写入失败时，下次可直接补存。
        plans_payload = {
            "target_job": resolved_target,
            "jd_text": bound_jd,
            "events": events,
            "source_round": latest_no,
            "plans": {
                "learning_plan": learning_plan,
                "project_plan": project_plan,
                "snapshot": snapshot_payload,
                "goal": goal,
                "final_plan": final_plan,
                "history_written": history_written,
                "history_write_status": history_write_status,
                "growth_outcome": growth_outcome,
                "growth_upgraded": growth_upgraded,
            },
        }
        with _STORE_WRITE_LOCK:
            pending_written = loop.set_pending_next_round(plans_payload)
        if pending_written.get("status") != WRITE_OK:
            warnings.append(
                "本轮计划尚未落盘，恢复标记也未能写入"
                f"（{pending_written.get('warning') or pending_written.get('status')}）"
            )

        # 锁只包住落盘这一段（毫秒级），**不含**上面 workflow.invoke 的几分钟推理 ——
        # 否则一次「生成下一轮」会把同时提交的任务完成整整堵死几分钟。
        with _STORE_WRITE_LOCK:
            written = loop.append_round(
                resolved_target,
                learning_plan,
                project_plan,
                snapshot=snapshot_payload,
                source="next_round",
                latest_goal=goal,
                clear_pending=True,
            )
        if written.get("status") == WRITE_OK:
            round_created = True
            round_no = written.get("round")
        else:
            # 部分成功要如实说：成长历史可能已经写进去了，只有轮次没存下来。
            head = "成长历史已写入，但本轮计划未能保存（部分成功）" if history_written \
                else "本轮计划未能保存，成长历史也未写入"
            warning = (
                f"{head}（{written.get('status')}）：{written.get('warning')}"
                "。本次已算好的计划留在恢复标记里，再点一次「生成下一轮」会直接补存，"
                "不会重复记账。"
            )

    return {
        "ok": True,
        "round_created": round_created,
        "round_no": round_no,
        "history_written": history_written,
        "history_write_status": history_write_status,
        "growth_outcome": growth_outcome,
        "growth_upgraded": growth_upgraded,
        "events_handed_over": len(events),
        "goal": goal,
        "goal_status": goal.get("status"),
        "final_plan": final_plan,
        "target_job": resolved_target,
        "user_skills": result.get("user_skills") or [],
        "skill_updates": result.get("skill_updates") or [],
        "growth_diff": diff,
        "learning_plan": learning_plan,
        "project_plan": project_plan,
        "job_profile": result.get("job_profile"),
        "job_profile_origin": result.get("job_profile_origin"),
        "job_profile_warnings": result.get("job_profile_warnings") or [],
        "warning": warning,
        "warnings": warnings,
        "recovered": bool(recovery),
        "pending_next_round": loop.pending_next_round() is not None,
        "plan_snapshot_round": latest_no if snapshot is not None else None,
        "requirements_reused": bool(snapshot and snapshot.get("job_requirements")),
    }


def run_next_round(stores: Stores, user_profile=None, target_job=None, jd_text=None) -> dict:
    """``_run_next_round`` 的可序列化包装（HTTP 层用这个）。"""
    return jsonable(
        _run_next_round(stores, user_profile=user_profile, target_job=target_job, jd_text=jd_text)
    )


# ── 只读查询 ────────────────────────────────────────────────────────────
def list_tasks(
    stores: Stores,
    round_no: int | None = None,
    status: str | None = None,
    limit: int = 500,
    offset: int = 0,
) -> dict:
    """查询轮次与任务清单。**只读。**

    ``status`` 非法时抛 ``ValueError``（由 app 层转成 422），
    不静默返回空列表 —— 那会让"参数写错"看起来像"没有任务"。

    ``limit`` / ``offset`` 只对返回的 ``tasks`` 数组开窗；``rounds`` 与
    ``summary`` 永远是**全量口径**，另附 ``pagination`` 块说明窗口位置。
    默认 ``limit=500`` —— 正常使用远用不满，不传参数的老前端行为不变。
    """
    loaded = stores.loop.load()
    try:
        tasks = stores.loop.tasks(round_no=round_no, status=status, data=loaded.data)
    except LoopStoreError as exc:
        raise ValueError(str(exc)) from exc

    total = len(tasks)
    window = tasks[offset:offset + limit] if (offset or limit) else tasks

    rounds = []
    for entry in stores.loop.rounds(loaded.data):
        items = entry.get("tasks") or []
        snapshot = entry.get("snapshot") if isinstance(entry.get("snapshot"), dict) else {}
        rounds.append({
            "round": entry.get("round"),
            "target_job": entry.get("target_job"),
            "created_at": entry.get("created_at"),
            "source": entry.get("source"),
            "goal_status": snapshot.get("goal_status"),
            "task_count": len(items),
            "completed_count": sum(1 for t in items if t.get("status") == TASK_COMPLETED),
        })

    latest = stores.loop.latest_round(loaded.data)
    marker = stores.loop.pending_next_round(loaded.data)
    pending = None
    if marker:
        # 只回恢复所需的元信息：计划全文要么已经在轮次里，要么留在标记里给服务层用；
        # 列表接口不该把一整份计划再复制一遍。
        pending = {
            "created_at": marker.get("created_at"),
            "source_round": marker.get("source_round"),
            "target_job": marker.get("target_job"),
            "has_plans": bool(marker.get("plans")),
            "event_count": len(marker.get("events") or []),
        }
    return jsonable({
        "store_status": loaded.status,
        "writable": loaded.writable,
        "warning": loaded.warning,
        "rounds": rounds,
        "latest_round": latest,
        "latest_goal": stores.loop.latest_goal(loaded.data),
        "pending_next_round": pending,
        "tasks": window,
        "pagination": {"limit": limit, "offset": offset, "returned": len(window), "total": total},
        "summary": {
            "total": total,
            "completed": sum(1 for t in tasks if t.get("status") == TASK_COMPLETED),
            "pending": sum(1 for t in tasks if t.get("status") == TASK_PENDING),
        },
    })


def growth_history(stores: Stores) -> dict:
    """查询真正落盘的成长历史。**只读。**

    存储损坏/不可读时**不掩盖**：原样带出 ``status`` 与 ``warning``，
    并明确说明此时看到的是空表而不是"确实没有成长"。
    """
    loaded = stores.growth.load()
    payload = {
        "status": loaded.status,
        "writable": loaded.writable,
        "warning": loaded.warning,
        "path": str(loaded.path) if loaded.path else None,
        "history": [],
        "latest_skills": [],
        "baseline": None,
        "confirmed_count": 0,
    }
    if not loaded.writable:
        return jsonable(payload)

    history = loaded.data.get("history") or []
    voided_refs, _ = voided_index(history)
    annotated = []
    for entry_index, entry in enumerate(history):
        item = dict(entry)
        item["voided_event_indices"] = sorted(
            event_index for (row, event_index) in voided_refs if row == entry_index
        )
        annotated.append(item)

    payload["history"] = annotated
    payload["confirmed_count"] = sum(
        1 for entry in history
        if entry.get("kind") not in (ENTRY_KIND_ADJUSTMENT, ENTRY_KIND_VERIFICATION)
    )
    payload["voided_count"] = len(voided_refs)
    payload["latest_skills"] = stores.growth.latest_skills(loaded.data)
    payload["baseline"] = loaded.data.get("baseline")
    return jsonable(payload)


# ── 系统状态 ────────────────────────────────────────────────────────────
def _ollama_status(timeout: float = 2.0) -> dict:
    """探测 Ollama 是否可达，并列出已安装模型。不调模型、不生成内容。"""
    url = config.OLLAMA_BASE_URL.rstrip("/") + config.OLLAMA_TAGS_PATH
    info = {
        "ok": False,
        "base_url": config.OLLAMA_BASE_URL,
        "models": [],
        "chat_model": config.CHAT_MODEL,
        "chat_model_ready": False,
        "error": None,
    }
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        names = [str(item.get("name") or "") for item in (payload.get("models") or [])]
        info["models"] = names
        info["ok"] = True
        info["chat_model_ready"] = any(n.split(":")[0] == config.CHAT_MODEL.split(":")[0] for n in names)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        info["error"] = f"{type(exc).__name__}: {exc}"
    return info


def _knowledge_status() -> dict:
    """检查技能库 / 项目库 / 岗位画像能否加载（成长升级、项目推荐与资料摘要都依赖它们）。"""
    result: dict = {}
    try:
        skills = SkillKnowledgeBase(directory=config.SKILLS_DIR)
        # size 是 @property，不是方法
        result["skills"] = {"ok": True, "count": skills.size, "error": None}
    except Exception as exc:  # noqa: BLE001 - 健康检查要如实报告失败原因
        result["skills"] = {"ok": False, "count": 0, "error": f"{type(exc).__name__}: {exc}"}
    try:
        projects = ProjectKnowledgeBase(directory=config.PROJECTS_DIR)
        # projects 是 @property，不是方法
        result["projects"] = {"ok": True, "count": len(projects.projects), "error": None}
    except Exception as exc:  # noqa: BLE001
        result["projects"] = {"ok": False, "count": 0, "error": f"{type(exc).__name__}: {exc}"}
    result["jobs"] = _job_catalog_status()
    return result


def _job_catalog_status() -> dict:
    """岗位目录读数：几个岗位、登记了多少条可回查的资料出处、来自几份资料。

    这是"没粘招聘原文时还能不能给出可回查依据"的家底 —— 用户没提供原文时，
    岗位要求的可信度全靠这里数出来的那批整理稿。
    """
    try:
        catalog = JobCatalog()
        inventories = [evidence_inventory(profile) for profile in catalog.profiles]
        source_ids = {source for item in inventories for source in item["source_ids"]}
        return {
            "ok": True,
            "count": len(catalog.profiles),
            "evidence_entries": sum(item["entries"] for item in inventories),
            "source_count": len(source_ids),
            "error": None,
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "count": 0, "evidence_entries": 0, "source_count": 0,
                "error": f"{type(exc).__name__}: {exc}"}


def health(stores: Stores) -> dict:
    """系统状态：把三类外部依赖的可达性一次说清。**只读。**

    任一依赖不可用只降低整体状态（``degraded``），不视为服务故障 ——
    技能/项目知识库是**增强**而非必需：读不到时分析照常跑，只是学习路线与项目
    推荐会如实告警"无法归一为规范技能名"（见 agents/learning_agent.py）。

    模型就绪口径随通道走：云端通道只看 key 是否已配置（配置即视为就绪，
    key 无效的真相由 ``cloud_auth_failed`` 单独标出），本地通道才探测 Ollama。
    """
    ollama = _ollama_status()
    knowledge = _knowledge_status()

    channel = get_channel()
    cloud_ready = cloud_configured()
    model_ready = (
        cloud_ready
        if channel == CHANNEL_CLOUD
        else (ollama["ok"] and ollama["chat_model_ready"])
    )

    growth_loaded = stores.growth.load()
    loop_loaded = stores.loop.load()
    stores_info = {
        "growth": {
            "path": str(stores.growth.path),
            "status": growth_loaded.status,
            "writable": growth_loaded.writable,
            "exists": stores.growth.path.exists(),
            "warning": growth_loaded.warning,
        },
        "loop": {
            "path": str(stores.loop.path),
            "status": loop_loaded.status,
            "writable": loop_loaded.writable,
            "exists": stores.loop.path.exists(),
            "rounds": len(loop_loaded.data.get("rounds") or []),
            "warning": loop_loaded.warning,
        },
    }

    healthy = (
        model_ready and not cloud_auth_failed()
        and knowledge["skills"]["ok"] and knowledge["projects"]["ok"]
        and knowledge["jobs"]["ok"]
        and growth_loaded.writable and loop_loaded.writable
    )

    return jsonable({
        "status": "ok" if healthy else "degraded",
        "llm_channel": {
            "channel": channel,
            "cloud_configured": cloud_ready,
            "active_model": active_model_name(),
            "cloud_model": cloud_settings()["model"],
            "cloud_auth_failed": cloud_auth_failed(),
        },
        "ollama": ollama,
        "knowledge": knowledge,
        "stores": stores_info,
        "config": {
            "chat_model": config.CHAT_MODEL,
            "runs_dir": str(config.RUNS_DIR),
            "task_workers": config.TASK_WORKERS,
        },
    })


# 让常量在 app 层可用（避免 app 直接依赖 agents.loop_store 的常量名）
COMPLETION_OK = COMPLETE_OK
COMPLETION_ALREADY = COMPLETE_ALREADY
LOOP_STORE_STATUSES = (
    LOOP_STATUS_OK, LOOP_STATUS_EMPTY, LOOP_STATUS_CORRUPT,
    LOOP_STATUS_UNREADABLE, LOOP_STATUS_SCHEMA_MISMATCH,
)