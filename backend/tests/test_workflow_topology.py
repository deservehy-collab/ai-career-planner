"""工作流拓扑：retrieval 与 profile/growth 并行，job 等两条分支都完成。

这些测试只锁图的连接关系与真实并发，不调模型：
- 拓扑用 ``get_graph().edges`` 断言；
- 并发用假的节点函数 + 事件互相等待 —— 如果图退化成顺序执行，
  两边会互相等超时，测试立刻失败。
"""

from __future__ import annotations

import threading

from graph import workflow as workflow_mod


def _edges(graph) -> set[tuple[str, str]]:
    return {(edge.source, edge.target) for edge in graph.get_graph().edges}


def test_retrieval_starts_directly_from_start_instead_of_waiting_for_growth():
    edges = _edges(workflow_mod.workflow)

    assert ("__start__", "profile") in edges
    assert ("__start__", "retrieval") in edges
    # 旧的顺序链必须断开：retrieval 不再挂在 growth 后面
    assert ("growth", "retrieval") not in edges
    assert ("profile", "growth") in edges


def test_job_waits_for_both_skill_and_requirement_branches():
    edges = _edges(workflow_mod.workflow)

    assert ("growth", "job") in edges
    assert ("retrieval", "job") in edges
    # 条件分支保持不变
    assert ("job", "assessment") in edges
    conditional = {
        (edge.source, edge.target)
        for edge in workflow_mod.workflow.get_graph().edges
        if edge.conditional
    }
    assert ("assessment", "learning") in conditional
    assert ("assessment", "__end__") in conditional


def test_each_node_runs_once_and_job_sees_both_branches(monkeypatch):
    updates = {
        "profile": {"user_skills": ["Python"]},
        "growth": {"skill_updates": ["升级"]},
        "retrieval": {"job_evidence": ["原文依据"]},
        "job": {"job_requirements": []},
        "assessment": {"skill_gaps": []},
        "learning": {"learning_plan": []},
        "project": {"project_plan": []},
    }
    runs: dict[str, int] = {}
    seen: dict[str, dict] = {}

    def make_node(name):
        def node(state):
            runs[name] = runs.get(name, 0) + 1
            seen[name] = dict(state)
            return dict(updates[name])
        return node

    for name in updates:
        monkeypatch.setitem(workflow_mod.NODES, name, make_node(name))

    workflow_mod.build_workflow().invoke({})

    # 两个单独 add_edge 汇入 job 时，LangGraph 会每条边各触发一次：
    # job 及整条下游链跑两遍，第二遍还可能带着不完整的上游状态。
    assert runs == {name: 1 for name in updates}
    assert seen["job"]["user_skills"] == ["Python"]
    assert seen["job"]["skill_updates"] == ["升级"]
    assert seen["job"]["job_evidence"] == ["原文依据"]


def test_profile_and_retrieval_actually_run_concurrently(monkeypatch):
    profile_started = threading.Event()
    retrieval_started = threading.Event()
    finished: dict[str, bool] = {}

    def fake_profile(state):
        profile_started.set()
        assert retrieval_started.wait(2), "profile 没有和 retrieval 同时开始"
        finished["profile"] = True
        return {}

    def fake_retrieval(state):
        retrieval_started.set()
        assert profile_started.wait(2), "retrieval 没有和 profile 同时开始"
        finished["retrieval"] = True
        return {}

    def fake_growth(state):
        assert finished.get("profile"), "growth 在 profile 完成前就开始了"
        finished["growth"] = True
        return {}

    def fake_job(state):
        assert finished.get("growth"), "job 在 growth 完成前就开始了"
        assert finished.get("retrieval"), "job 在 retrieval 完成前就开始了"
        finished["job"] = True
        return {}

    monkeypatch.setitem(workflow_mod.NODES, "profile", fake_profile)
    monkeypatch.setitem(workflow_mod.NODES, "growth", fake_growth)
    monkeypatch.setitem(workflow_mod.NODES, "retrieval", fake_retrieval)
    monkeypatch.setitem(workflow_mod.NODES, "job", fake_job)
    monkeypatch.setitem(workflow_mod.NODES, "assessment", lambda state: {})
    monkeypatch.setitem(workflow_mod.NODES, "learning", lambda state: {})
    monkeypatch.setitem(workflow_mod.NODES, "project", lambda state: {})

    graph = workflow_mod.build_workflow()
    graph.invoke({})

    assert finished == {
        "profile": True,
        "retrieval": True,
        "growth": True,
        "job": True,
    }
