"""面试出题：证据只取真实资料、模型输出必须过格式闸门、接口失败要说人话。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend import interview
from backend.app import app
from backend.deps import create_archive_factory

PLAN = {
    "id": "plan-1",
    "title": "Java 后端",
    "target_job": "Java 后端开发",
    "jd_text": "岗位要求：熟悉 Java 并发、Spring Boot、MySQL 索引优化，了解 Redis。",
    "profile_snapshot": {"major": "软件工程", "grade": "大三"},
    "current_skills": [{"skill": "Java", "level": "掌握"}],
    "goal": {},
}

RESUME = {
    "basic": {"name": "王小明", "phone": "13800000000", "email": "ming@example.com"},
    "intent": {"targetJob": "Java 后端开发"},
    "education": [{"school": "示例大学", "major": "软件工程", "degree": "本科", "time": ""}],
    "skills": [{"label": "熟练", "items": ["Java", "Spring Boot"]}],
    "internships": [],
    "projects": [{
        "title": "校园二手交易平台",
        "role": "后端",
        "time": "2026.03",
        "bullets": ["负责订单模块", "压测 QPS 600"],
    }],
    "campus": [],
    "research": [],
    "certificates": ["大学英语六级"],
    "languages": [],
    "interests": [],
    "summary": "",
    "photo": "",
}

GOOD_RAW = """```json
[
  {"category":"技术","question":"讲讲 Spring Boot 自动配置原理。","why":"核对框架基础。","key_points":["自动配置类","条件注解"],"follow_up":"如果冲突怎么排查？"},
  {"category":"技术","question":"MySQL 索引在什么情况下失效？","why":"核对检索基础。","key_points":["前置模糊匹配","函数包裹列"],"follow_up":"你怎么验证？"},
  {"category":"项目","question":"二手交易平台的订单模块你怎么设计？","why":"核对项目深度。","key_points":["订单状态机","压测 QPS 600"],"follow_up":"遇到过并发问题吗？"},
  {"category":"项目","question":"项目里最难的取舍是什么？","why":"核对判断力。","key_points":["需要补充：你实际做的取舍和结果"],"follow_up":"如果重做你会改什么？"},
  {"category":"动机","question":"为什么想做后端开发？","why":"核对动机。","key_points":["结合你已完成的学习任务回答"],"follow_up":"三年内的目标是什么？"},
  {"category":"反问","question":"入职后前三个月的工作重点是什么？","why":"了解岗位预期。","key_points":["问清团队分工与考核"],"follow_up":""}
]
```"""


def test_evidence_uses_resume_facts_and_drops_contact_info():
    evidence = interview.build_evidence(PLAN, RESUME)

    assert evidence["target_job"] == "Java 后端开发"
    assert "Spring Boot" in evidence["skills"]
    assert any("校园二手交易平台" in item for item in evidence["facts"])
    assert any("QPS 600" in item for item in evidence["facts"])
    joined = "\n".join(evidence["facts"]) + "\n".join(evidence["skills"])
    assert "13800000000" not in joined
    assert "ming@example.com" not in joined


def test_evidence_falls_back_to_plan_when_resume_is_missing():
    evidence = interview.build_evidence(PLAN, None)

    assert "Java" in evidence["skills"]
    assert evidence["source"] == "plan"


def test_jd_is_truncated_before_prompt():
    evidence = interview.build_evidence({**PLAN, "jd_text": "x" * 9000}, RESUME)

    assert len(evidence["jd_text"]) == interview.MAX_JD_CHARS


def test_parse_questions_accepts_fenced_json():
    questions = interview.parse_questions(GOOD_RAW)

    assert len(questions) == 6
    assert questions[0]["category"] == "技术"
    assert questions[0]["key_points"] == ["自动配置类", "条件注解"]


def test_parse_questions_accepts_object_wrapper_and_drops_bad_items():
    raw = (
        '{"questions": ['
        '{"category": "项目", "question": "讲讲你的项目", "why": "", "key_points": [], "follow_up": ""},'
        '{"category": "技术", "question": "", "key_points": ["x"]},'
        '{"category": "技术", "question": "讲讲并发", "key_points": ["线程池"]},'
        '{"category": "动机", "question": "为什么做后端", "key_points": ["结合学习任务"]}'
        "]}"
    )

    questions = interview.parse_questions(raw)

    assert [item["question"] for item in questions] == ["讲讲你的项目", "讲讲并发", "为什么做后端"]
    assert questions[0]["key_points"] == []


def test_parse_questions_rejects_garbage_and_too_few_items():
    with pytest.raises(interview.InterviewFormatError):
        interview.parse_questions("这次我不想出题。")

    with pytest.raises(interview.InterviewFormatError):
        interview.parse_questions('[{"question": "只有一个问题"}]')


def test_parse_questions_caps_item_count():
    items = ",".join(
        '{"category":"技术","question":"问题 %d","key_points":["a"]}' % index
        for index in range(12)
    )
    questions = interview.parse_questions(f"[{items}]")

    assert len(questions) == interview.MAX_QUESTIONS


def test_coverage_replaces_model_reverse_question_with_canonical_one():
    parsed = interview.parse_questions(GOOD_RAW)

    filled = interview.ensure_category_coverage(parsed, "Java 后端开发")

    reverse = [item for item in filled if item["category"] == "反问"]
    assert len(reverse) == 1
    assert reverse[0]["question"].startswith("关于Java 后端开发")
    assert len(filled) == len(parsed)


def test_coverage_appends_reverse_question_when_model_forgets_it():
    parsed = interview.parse_questions(GOOD_RAW)
    without_reverse = [item for item in parsed if item["category"] != "反问"]

    filled = interview.ensure_category_coverage(without_reverse, "Java 后端开发")

    assert filled[-1]["category"] == "反问"
    assert "Java 后端开发" in filled[-1]["question"]
    assert len(filled) == len(without_reverse) + 1


def test_local_call_uses_structured_output_schema(monkeypatch):
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b'{"message": {"content": "[]"}}'

    def fake_post(target, payload, headers, timeout):
        captured.update(payload)
        return FakeResponse()

    monkeypatch.setattr(interview.chat, "_probe_models", lambda url: (True, [interview.MODEL_NAME], None))
    monkeypatch.setattr(interview.chat, "_model_installed", lambda names: True)
    monkeypatch.setattr(interview, "_post_json", fake_post)

    interview._call_local([{"role": "user", "content": "出题"}])

    schema = captured["format"]
    assert schema["type"] == "array"
    assert schema["minItems"] == 6
    assert "category" in schema["items"]["required"]
    assert schema["items"]["properties"]["category"]["enum"] == list(interview.CATEGORIES)


def test_generate_retries_once_when_output_is_not_json(monkeypatch):
    calls = []

    def flaky_local(messages):
        calls.append(messages)
        return "这次我不想按 JSON 返回。" if len(calls) == 1 else GOOD_RAW

    monkeypatch.setattr(interview, "get_channel", lambda: "local")
    monkeypatch.setattr(interview, "_call_local", flaky_local)

    questions = interview.generate_questions(interview.build_evidence(PLAN, RESUME))

    assert len(calls) == 2
    assert len(questions) == 6


def test_generate_gives_up_after_second_bad_output(monkeypatch):
    calls = []

    def broken_local(messages):
        calls.append(messages)
        return "还是没有 JSON。"

    monkeypatch.setattr(interview, "get_channel", lambda: "local")
    monkeypatch.setattr(interview, "_call_local", broken_local)

    with pytest.raises(interview.InterviewFormatError):
        interview.generate_questions(interview.build_evidence(PLAN, RESUME))
    assert len(calls) == 2


def _client(monkeypatch, *, plan=PLAN, outcome=None):
    def fake_factory():
        return None

    app.dependency_overrides[create_archive_factory] = fake_factory
    monkeypatch.setattr(interview, "get_plan", lambda factory, owner, plan_id: plan)

    def fake_generate(evidence):
        if isinstance(outcome, Exception):
            raise outcome
        return outcome or interview.parse_questions(GOOD_RAW)

    monkeypatch.setattr(interview, "generate_questions", fake_generate)
    return TestClient(app)


def test_endpoint_returns_questions(monkeypatch):
    client = _client(monkeypatch)

    response = client.post("/api/interview/questions", json={"plan_id": "plan-1", "resume": RESUME})

    assert response.status_code == 200
    body = response.json()
    assert len(body["questions"]) == 6
    assert body["target_job"] == "Java 后端开发"
    assert body["sources"]["resume"] is True


def test_endpoint_404_when_plan_is_gone(monkeypatch):
    client = _client(monkeypatch, plan=None)

    response = client.post("/api/interview/questions", json={"plan_id": "plan-1"})

    assert response.status_code == 404
    assert "档案" in response.json()["detail"]


def test_endpoint_maps_model_failures_to_human_messages(monkeypatch):
    client = _client(
        monkeypatch,
        outcome=interview.InterviewUnavailable("本地模型没启动，请先运行 ollama serve。"),
    )
    assert client.post("/api/interview/questions", json={"plan_id": "plan-1"}).status_code == 503

    client = _client(monkeypatch, outcome=interview.InterviewFormatError("格式不对"))
    response = client.post("/api/interview/questions", json={"plan_id": "plan-1"})
    assert response.status_code == 502
    assert "再试一次" in response.json()["detail"]
