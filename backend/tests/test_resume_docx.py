"""简历 Word 导出接口：文件可打开、内容不丢、边界不被绕过。"""

from __future__ import annotations

import io
import zipfile
from xml.etree import ElementTree

from fastapi.testclient import TestClient

from backend.app import app
from backend.auth import get_current_user

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


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
        "internships": [],
        "projects": [{
            "title": "校园二手交易平台", "role": "后端",
            "time": "2026.03", "bullets": ["负责订单模块", "压测 QPS 600"],
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


def _document_xml(content: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        names = set(archive.namelist())
        assert "[Content_Types].xml" in names
        assert "_rels/.rels" in names
        assert "word/document.xml" in names
        return archive.read("word/document.xml").decode("utf-8")


def test_export_returns_openable_docx_with_content():
    client = TestClient(app)

    response = client.post("/api/resume/docx", json={"resume": _resume()})

    assert response.status_code == 200
    assert response.content[:2] == b"PK"
    assert response.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    assert "resume.docx" in response.headers["content-disposition"]

    xml_text = _document_xml(response.content)
    root = ElementTree.fromstring(xml_text)
    texts = "".join(node.text or "" for node in root.iter(f"{{{W_NS}}}t"))

    for expected in [
        "王小明", "13800000000", "Java 后端开发", "示例大学", "软件工程",
        "熟练 - Java、Spring Boot", "校园二手交易平台", "负责订单模块",
        "大学英语六级", "英语 CET-6", "自我评价",
    ]:
        assert expected in texts, expected

    # 参考件版式：微软雅黑、姓名 20pt、小节标题 12pt、正文 10pt / 固定 15.6pt 行距
    assert 'w:eastAsia="微软雅黑"' in xml_text
    assert 'w:ascii="Microsoft YaHei"' in xml_text
    assert '<w:sz w:val="40"/>' in xml_text
    assert '<w:sz w:val="24"/>' in xml_text
    assert '<w:sz w:val="20"/>' in xml_text
    assert 'w:line="312" w:lineRule="exact"' in xml_text
    assert 'w:top="1134"' in xml_text and 'w:left="777"' in xml_text


def test_content_flows_without_forced_page_break():
    client = TestClient(app)
    resume = _resume()
    resume["campus"] = [{"title": "学生会", "role": "部长", "time": "2024", "bullets": ["组织活动"]}]
    resume["research"] = [{"title": "课题", "role": "", "time": "2025", "bullets": ["实验"]}]

    response = client.post("/api/resume/docx", json={"resume": resume})

    assert response.status_code == 200
    assert '<w:br w:type="page"/>' not in _document_xml(response.content)


def test_empty_resume_is_rejected_with_actionable_message():
    client = TestClient(app)

    response = client.post("/api/resume/docx", json={"resume": {}})

    assert response.status_code == 422
    assert "简历还是空的" in response.json()["detail"]


def test_overlong_bullet_is_rejected():
    client = TestClient(app)
    resume = _resume(projects=[{
        "title": "项目", "role": "", "time": "",
        "bullets": ["x" * 501],
    }])

    response = client.post("/api/resume/docx", json={"resume": resume})

    assert response.status_code == 422


def test_export_requires_login():
    client = TestClient(app)
    saved = app.dependency_overrides.pop(get_current_user, None)
    try:
        response = client.post("/api/resume/docx", json={"resume": _resume()})
        assert response.status_code == 401
    finally:
        if saved is not None:
            app.dependency_overrides[get_current_user] = saved
