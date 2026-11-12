"""简历内容中间层：新字段进版块、空值不出现、照片只认真图。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from backend.app import app
from backend.resume_content import build_resume_content, normalize_photo
from backend.resume_pdf import build_resume_pdf_html

_PNG_1X1 = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def _resume(**overrides) -> dict:
    resume = {
        "basic": {
            "name": "王小明",
            "gender": "男",
            "birth": "2004-03-05",
            "phone": "13800000000",
            "email": "ming@example.com",
            "city": "杭州",
            "experience": "2 年",
        },
        "intent": {
            "targetJob": "Java 后端开发",
            "city": "杭州",
            "availability": "随时",
            "salary": "15-20K",
            "industry": "互联网",
        },
        "education": [{
            "school": "示例大学", "major": "软件工程", "degree": "本科",
            "time": "2022.09 – 2026.06", "grade": "大三", "gpa": "3.6",
            "ranking": "5/120", "courses": "数据结构, 操作系统",
        }],
        "skills": [{"label": "熟练", "items": ["Java", "Spring Boot"]}],
        "internships": [],
        "projects": [],
        "campus": [],
        "research": [{
            "title": "实验室课题", "role": "成员",
            "time": "2025.03", "bullets": ["负责数据清洗"],
        }],
        "certificates": [],
        "languages": ["英语 CET-6"],
        "interests": ["篮球"],
        "summary": "",
        "photo": "",
    }
    resume.update(overrides)
    return resume


def _block(content: dict, title: str) -> dict | None:
    return next((block for block in content["blocks"] if block["title"] == title), None)


def test_new_fields_land_in_the_right_blocks():
    content = build_resume_content(_resume())

    contacts = {cell["label"]: cell["value"] for row in content["contacts"] for cell in row}
    assert contacts["经验"] == "2 年"
    assert contacts["学历"] == "本科"

    intent = _block(content, "求职意向")
    intent_values = [cell["value"] for row in intent["rows"] for cell in row]
    assert "15-20K" in intent_values
    assert "互联网" in intent_values

    education = _block(content, "教育经历")
    lines = {line["label"]: line["value"] for line in education["items"][0]["lines"]}
    assert lines["*学历"] == "本科（大三）"
    assert lines["*学分绩点"] == "3.6，5/120"
    assert lines["*核心课程"] == "数据结构、操作系统"

    research = _block(content, "研究经历")
    assert research["numbered"] is False
    assert research["items"][0]["title"] == "实验室课题"

    skills = _block(content, "技能证书")
    values = {line["label"]: line["value"] for line in skills["items"]}
    assert values["技能"] == "熟练 - Java、Spring Boot"
    assert values["语言"] == "英语 CET-6"
    assert values["兴趣"] == "篮球"


def test_blank_fields_never_print_placeholder_text():
    content = build_resume_content(_resume(basic={
        "name": "王小明", "gender": "", "birth": "", "phone": "", "email": "",
        "city": "", "experience": "",
    }))

    labels = [cell["label"] for row in content["contacts"] for cell in row]
    assert "年龄" not in labels
    assert "手机" not in labels
    assert all(
        any(cell["value"] for cell in row) for row in content["contacts"]
    )
    assert "无" not in str(content["blocks"])
    assert _block(content, "自我评价") is None


def test_rendered_html_skips_empty_contact_cells():
    html = build_resume_pdf_html(_resume(basic={
        "name": "王小明", "gender": "", "birth": "", "phone": "", "email": "",
        "city": "", "experience": "",
    }))

    assert "性别：" not in html
    assert "生日：" not in html
    assert "现居：" not in html
    assert "学历：本科" in html


def test_photo_accepts_real_image_and_rejects_garbage():
    photo = normalize_photo(_PNG_1X1)

    assert photo is not None
    assert photo["ext"] == "png"
    assert photo["width"] == 1 and photo["height"] == 1
    assert normalize_photo("data:image/png;base64,bm90LWEtcG5n") is None
    assert normalize_photo("https://example.com/a.png") is None


def test_export_rejects_invalid_photo_with_actionable_message():
    client = TestClient(app)

    response = client.post(
        "/api/resume/docx",
        json={"resume": _resume(photo="data:image/png;base64,bm90LWEtcG5n")},
    )

    assert response.status_code == 422
    assert "照片无法使用" in response.json()["detail"]
