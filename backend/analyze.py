"""只读分析链路：跑一次完整工作流，**绝不写真实 runs/。**

只读边界是怎么实现的
--------------------
``growth_node`` 是 ``runs/growth.json`` 的**唯一**正式写入方，而它读的是
``agents.growth_agent`` 的**模块级全局 store**。分析接口不该写成长历史，
所以进入工作流前用 :func:`_isolated_growth_store` 把那个全局换成临时目录，
退出时原样还原：

- 分析期间任何"想落盘"的动作都只落在临时目录里，真实 ``runs/`` 逐字节不变；
- 临时目录里会**只读复制**一份现有成长历史，让分析看到的基线与真实一致
  （复制是只读的，不碰原文件）。

这个手法改写的是**进程级全局**，所以它天然要求"同一时刻只有一个分析在跑"——
由 ``backend.jobs.TaskManager`` 的单工作线程保证（见 backend/config.py）。

与测试的对应关系
----------------
- ``workflow`` 必须是**模块级属性**：tests/test_readonly_boundary.py 用
  ``mock.patch.object(analyze_mod.workflow, "invoke", ...)`` 拦截模型调用。
- ``_isolated_growth_store`` 必须在此模块：同上测试 ``from backend.analyze import``。
"""

from __future__ import annotations

import contextlib
import json
import shutil
import tempfile
from pathlib import Path
from typing import Iterator

from agents import growth_agent, llm
from agents.growth_store import GrowthStore
from agents.job_profile import JobProfileStore
from agents.retrieval_agent import STATUS_USER_PASTED
from agents.source_attribution import build_source_report
from backend.jobs import (
    inside_task_run,
    report_node_finished,
    report_node_interrupted,
    report_node_skipped,
    report_node_started,
)
from graph.workflow import ORDER, build_final_plan, workflow

__all__ = [
    "workflow", "run_workflow", "use_growth_store", "_isolated_growth_store",
    "project_result", "requirement_basis",
]

#: 透出给前端的 state 字段白名单（其余是内部追踪字段，不对外）。
_PUBLIC_STATE_FIELDS = (
    "user_skills",
    "user_strengths",
    "user_weaknesses",
    "user_skills_before",
    "skill_updates",
    "job_requirements",
    "job_requirements_basis",
    "job_requirements_grounded",
    "job_profile",
    "job_profile_warnings",
    "job_profile_origin",
    "skill_gaps",
    "learning_plan",
    "project_plan",
    "job_evidence",
    "retrieval_status",
    "retrieval_diagnostics",
)


@contextlib.contextmanager
def use_growth_store(store: GrowthStore) -> Iterator[GrowthStore]:
    """临时把进程级成长 store 换成 ``store``，退出时还原。

    用于「下一轮规划」：工作流内部的 ``growth_node`` 会走正式路径写成长历史，
    必须让它写进**注入的那个** store（正常是 ``runs/``，测试是临时目录），
    否则注入形同虚设。
    """
    previous = growth_agent.get_store()
    growth_agent.set_store(store)
    try:
        yield store
    finally:
        growth_agent.set_store(previous)


@contextlib.contextmanager
def _isolated_growth_store() -> Iterator[GrowthStore]:
    """把一个**临时**的成长 store 设为进程级全局，退出还原。

    与 :func:`use_growth_store` 的区别：这里的目标目录是新建的临时目录，
    调用方碰不到，因此工作流内任何写入都留在临时目录，随退出一起删除。
    """
    previous = growth_agent.get_store()
    if getattr(previous, "db_backed", False):
        # 数据库档案级 store 没有可复制的 JSON 文件；用只读视图读取真实历史，
        # 分析期间的候选成长写入直接丢弃，不落库。
        isolated = previous.isolated() if hasattr(previous, "isolated") else previous
        growth_agent.set_store(isolated)
        try:
            yield isolated
        finally:
            growth_agent.set_store(previous)
        return

    tmp_dir = Path(tempfile.mkdtemp(prefix="career-analyze-"))
    try:
        isolated = GrowthStore(directory=tmp_dir)
        # 只读复制现有历史：让分析看到与真实一致的基线，但不改动原文件。
        # 复制失败（权限/占用）不该让分析失败 —— 当作空历史继续，只读性质不受影响。
        with contextlib.suppress(OSError):
            if previous.path.exists():
                shutil.copy2(previous.path, isolated.path)
        growth_agent.set_store(isolated)
        yield isolated
    finally:
        growth_agent.set_store(previous)
        shutil.rmtree(tmp_dir, ignore_errors=True)


def requirement_basis(state: dict) -> str:
    """本次岗位要求的**主依据**是哪一档，供前端决定标签与可信度呈现。

    三档，按证据来源判定，不看模型自述：

    - ``user_pasted``    用户当场粘贴的招聘要求原文（逐条能反显原话）
    - ``knowledge_base`` 没有原文，但岗位在内置画像里 —— 技能表有人工整理的
      知识库依据，仍不等于这家公司真实写过的门槛
    - ``model_only``     两者都没有 —— 岗位要求全是模型推断，前端必须标灰、
      且**不许**显示匹配度百分比
    """
    basis = str(state.get("job_requirements_basis") or "").strip()
    if basis in ("user_pasted", "knowledge_base", "model_only"):
        return basis
    # 旧结果 / 未经过 job_node 的运行没有这个字段，退回"有没有原文"的旧口径。
    if state.get("retrieval_status") == STATUS_USER_PASTED:
        return "user_pasted"
    return "model_only"


def project_result(state: dict, llm_warnings: list[str] | None = None) -> dict:
    """把工作流 state 投影成对外结构：白名单字段 + 汇总方案 + 模型降级提示。

    只做投影与序列化，**不改写任何业务字段的取值**。``llm_warnings`` 不是
    state 里的业务数据，而是本次运行中云端自动降级到本地时记下的提示，
    必须随结果一起给前端，否则用户会以为"云端成功了"，实际用的是本机模型。
    """
    payload: dict = {key: state.get(key) for key in _PUBLIC_STATE_FIELDS if key in state}
    payload["growth_diff"] = state.get("growth_diff") or {}
    payload["final_plan"] = build_final_plan(state)
    payload["requirement_basis"] = requirement_basis(state)
    payload["user_jd_provided"] = bool(str(state.get("jd_text") or "").strip())
    payload["source_report"] = build_source_report(state, requirement_basis(state))
    payload["llm_warnings"] = list(llm_warnings or [])
    return jsonable(payload)


def jsonable(payload):
    """JSON 往返，保证响应可安全序列化。

    业务层返回的本来就是 dict/list/str/int/bool，这里走一趟 JSON 只是为了
    **保证**这一点：响应里不许出现不能序列化的对象；``default=str`` 是
    最后一道兜底，只影响类型不是原生 JSON 的个别值，不增删任何字段。
    """
    return json.loads(json.dumps(payload, ensure_ascii=False, default=str))


_NODE_ORDER = tuple(ORDER)


def _node_error_text(error) -> str:
    if isinstance(error, str):
        return error
    return f"{type(error).__name__}: {error}"


def _run_workflow(initial_state: dict) -> dict:
    """执行编译后的图；在后台任务里同时回报节点级进度。

    后台任务里走 ``stream(stream_mode=["debug", "values"])``：debug 事件给出每个
    节点真实的开始/结束（含失败），values 给出最新 state。不在任务里（CLI、
    既有单测）仍用一次性 ``invoke``，行为与返回值都不变。

    成功结束后，本轮没被执行的节点（assessment 之后走 END 的 learning/project）
    记为 ``skipped``——这是图自己的分支决定的，不是按时间猜的进度。
    """
    if not inside_task_run():
        return workflow.invoke(initial_state)

    visited: set[str] = set()
    last_state = initial_state
    try:
        for mode, chunk in workflow.stream(
            initial_state, stream_mode=["debug", "values"]
        ):
            if mode == "values":
                last_state = chunk
                continue
            if mode != "debug" or not isinstance(chunk, dict):
                continue
            kind = chunk.get("type")
            payload = chunk.get("payload") or {}
            node = payload.get("name")
            if node not in _NODE_ORDER:
                continue
            if kind == "task":
                visited.add(node)
                report_node_started(node)
            elif kind == "task_result":
                error = payload.get("error")
                if error is not None:
                    text = _node_error_text(error)
                    report_node_finished(node, error=text)
                    raise RuntimeError(f"节点 {node} 失败：{text}")
                report_node_finished(node)
    except Exception as exc:
        # 节点抛异常时 LangGraph 不补 task_result，直接把异常抛给调用方；
        # 这里把当时在跑的节点如实记为失败，别让它永远停在 running。
        report_node_interrupted(f"{type(exc).__name__}: {exc}")
        raise

    for node in _NODE_ORDER:
        if node not in visited:
            report_node_skipped(node)
    return last_state


def run_workflow(
    payload,
    baseline_store: GrowthStore | None = None,
    enable_job_profile: bool = True,
) -> dict:
    """跑一次只读分析，返回投影后的结果。

    ``payload`` 是 :class:`backend.schemas.AnalyzeRequest`。
    副作用：调用模型做多次推理；**不写任何真实业务数据**。

    ``baseline_store``
    ------------------
    分析要"看着真实历史"做只读复制（见 :func:`_isolated_growth_store`）。
    单用户时代那份历史就是全局默认 store；**按用户隔离数据后**，"真实历史"
    是**当前用户**的 ``growth.json`` —— 由 HTTP 层把 ``stores.growth`` 传进来。
    不传时退回全局默认 store（CLI 与既有测试的口径，行为不变）。

    岗位画像缓存
    ------------
    传了 ``baseline_store`` 时，从**当前用户目录**只读加载同一岗位/原文的画像；
    命中就注入 state，不命中才由 job_node 生成。这里**只读不写** —— 缓存由
    ``services.run_analyze_and_save`` / ``_run_next_round`` 在各自的保存路径落盘。
    ``enable_job_profile=False`` 用于纯诊断的 ``/api/analyze``：允许读缓存，
    但没命中时不额外生成，避免只读接口多一次模型调用。
    """
    initial_state = payload.to_state()

    if baseline_store is not None:
        profile_store = JobProfileStore(baseline_store.directory)
        cached = profile_store.load(
            payload.target_job, initial_state.get("jd_text")
        )
        if cached.found:
            initial_state["job_profile"] = cached.profile
            initial_state["job_profile_warnings"] = list(cached.warnings)
        elif cached.warning:
            initial_state["job_profile_warnings"] = [cached.warning]

    initial_state["enable_job_profile"] = bool(enable_job_profile)

    llm.begin_warning_capture()
    try:
        if baseline_store is None:
            with _isolated_growth_store():
                state = _run_workflow(initial_state)
        else:
            # 先把全局 store 指到当前用户，再进隔离：隔离复制的基线才是这位用户的，
            # 而不是数据根目录下的旧全局文件。两层上下文退出时各自还原。
            with use_growth_store(baseline_store):
                with _isolated_growth_store():
                    state = _run_workflow(initial_state)
    except Exception:
        # 失败时这些提示已经进了异常信息，清掉避免串到下一次运行。
        llm.take_warnings()
        raise
    return project_result(state, llm.take_warnings())
