"""并行分支里的降级提示必须仍能被本次运行收走。

LangGraph 的同步并行节点跑在线程池里（contextvar 会随任务复制过去）。
警告捕获不能是 ``threading.local``，否则 profile / retrieval 并行时丢提示。
"""

from __future__ import annotations

from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from agents import llm


class _State(TypedDict, total=False):
    a: int
    b: int


def _parallel_graph():
    def left(state):
        llm._warn_once("左侧分支的降级提示")
        return {"a": 1}

    def right(state):
        llm._warn_once("右侧分支的降级提示")
        return {"b": 2}

    def join(state):
        return {}

    builder = StateGraph(_State)
    builder.add_node("left", left)
    builder.add_node("right", right)
    builder.add_node("join", join)
    builder.add_edge(START, "left")
    builder.add_edge(START, "right")
    builder.add_edge("left", "join")
    builder.add_edge("right", "join")
    builder.add_edge("join", END)
    return builder.compile()


def test_warnings_from_parallel_langgraph_nodes_are_captured():
    llm.begin_warning_capture()
    try:
        _parallel_graph().invoke({})
        warnings = llm.take_warnings()
    finally:
        llm.take_warnings()

    assert sorted(warnings) == ["右侧分支的降级提示", "左侧分支的降级提示"]
