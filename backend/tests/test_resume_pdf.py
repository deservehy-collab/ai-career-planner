"""简历 PDF 导出：A4 排版、只输出真填内容、缺浏览器时给出可操作提示。"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from backend import resume_pdf
from backend.app import app
from backend.resume_pdf import ResumePdfUnavailable, build_resume_pdf, build_resume_pdf_html


def _resume(**overrides) -> dict:
    resume = {
        "basic": {
            "name": "王小明",
            "gender": "",
            "birth": "",
            "phone": "13800000000",
            "email": "ming@example.com",
            "city": "杭州",
            "experience": "",
        },
        "intent": {
            "targetJob": "Java 后端开发", "city": "杭州", "availability": "随时",
            "salary": "", "industry": "",
        },
        "education": [{
            "school": "示例大学", "major": "软件工程", "degree": "本科",
            "time": "2022.09 – 2026.06", "grade": "大三", "gpa": "3.6",
            "ranking": "5/120", "courses": "数据结构、操作系统",
        }],
        "skills": [{"label": "熟练", "items": ["Java", "Spring Boot"]}],
        "internships": [{
            "title": "示例科技", "role": "后端实习生",
            "time": "2025.07 – 2025.09", "bullets": ["负责订单模块"],
        }],
        "projects": [{
            "title": "校园二手交易平台", "role": "后端",
            "time": "2026.03", "bullets": ["压测 QPS 600"],
        }],
        "campus": [],
        "research": [],
        "certificates": ["大学英语六级"],
        "languages": ["英语 CET-6"],
        "interests": [],
        "photo": "",
        "summary": "求职目标：Java 后端开发。",
    }
    resume.update(overrides)
    return resume


def test_html_escapes_user_input():
    html = build_resume_pdf_html(_resume(basic={
        "name": "<script>alert(1)</script>", "gender": "", "birth": "",
        "phone": "", "email": "", "city": "",
    }))

    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


def test_empty_sections_are_not_rendered():
    resume = _resume(
        projects=[], campus=[], skills=[], certificates=[], languages=[], interests=[], summary="",
    )

    html = build_resume_pdf_html(resume)

    assert "项目经历" not in html
    assert "技能证书" not in html
    assert "自我评价" not in html


def test_html_renders_one_continuous_sheet():
    html = build_resume_pdf_html(_resume())

    assert "工作经历" in html
    assert "项目经历" in html
    assert "技能证书" in html
    assert '<body>\n<header class="head">' in html
    assert "page-break-before" not in html


def test_html_does_not_invent_contact_details():
    html = build_resume_pdf_html(_resume(basic={
        "name": "王小明", "gender": "", "birth": "", "phone": "", "email": "", "city": "",
    }))

    assert "138-0000-0000" not in html
    assert "you@example.com" not in html


@pytest.mark.skipif(resume_pdf._find_browser() is None, reason="本机没有 Chrome / Edge")
def test_rendered_pdf_is_a4_and_flows():
    data = build_resume_pdf(_resume())

    assert data.startswith(b"%PDF")

    boxes = re.findall(rb"/MediaBox\s*\[0 0 ([\d.]+) ([\d.]+)\]", data)
    assert boxes, "PDF 里没有页面尺寸"
    width, height = (float(value) for value in boxes[0])
    assert 594 <= width <= 597
    assert 840 <= height <= 844

    counts = [int(value) for value in re.findall(rb"/Count\s+(\d+)", data)]
    assert max(counts) >= 1


def test_missing_browser_returns_actionable_503(monkeypatch):
    monkeypatch.setattr(resume_pdf, "_find_browser", lambda: None)
    client = TestClient(app)

    response = client.post("/api/resume/pdf", json={"resume": _resume()})

    assert response.status_code == 503
    assert "没有找到 Chrome 或 Edge" in response.json()["detail"]

    with pytest.raises(ResumePdfUnavailable):
        build_resume_pdf(_resume())


def test_empty_resume_is_rejected_before_rendering(monkeypatch):
    monkeypatch.setattr(resume_pdf, "_find_browser", lambda: None)
    client = TestClient(app)

    response = client.post("/api/resume/pdf", json={"resume": {}})

    assert response.status_code == 422
    assert "简历还是空的" in response.json()["detail"]
