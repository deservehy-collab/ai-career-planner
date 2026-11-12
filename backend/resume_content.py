"""简历内容中间层：把前端清洗后的字典整理成 Word / PDF 共用的版块。

导出的两个格式必须长得一样，所以“写什么”只在这里决定一次：
版式常量、字体、列位、分页交给各自的渲染器。这里只做四件事：

* 顺序与取舍：姓名、两行联系方式、求职意向、经历、技能证书、自我评价；
* 推导：年龄由生日推算，学历取教育经历里的最高一档；
* 清洗：空值一律不出现（绝不写“无”），课程与证书统一分隔符；
* 照片：把 data URL 解成字节并读出像素尺寸，供两边按比例排版。
"""

from __future__ import annotations

import base64
import binascii
import re
import struct
from datetime import date

__all__ = ["build_resume_content", "normalize_photo", "photo_extent"]

_MAX_PHOTO_BYTES = 2 * 1024 * 1024
_MAX_PHOTO_W_EMU = 32 * 36000
_MAX_PHOTO_H_EMU = 38 * 36000

_DEGREE_RANK = (
    ("博士", 5),
    ("硕士", 4),
    ("研究生", 4),
    ("本科", 3),
    ("学士", 3),
    ("大专", 2),
    ("专科", 2),
    ("高职", 2),
    ("高中", 1),
    ("中专", 1),
)

_MIME_EXT = {
    "image/png": ("png", "png"),
    "image/jpeg": ("jpg", "jpeg"),
    "image/jpg": ("jpg", "jpeg"),
}


def _text(value: object) -> str:
    return str(value or "").strip()


def _split_items(value: object) -> list[str]:
    """把“Java, Spring Boot、MySQL”这类手填串拆成干净条目。"""
    if isinstance(value, (list, tuple)):
        raw = [str(item) for item in value]
    else:
        raw = re.split(r"[,，、;；/|\n]+", _text(value))
    return [item.strip() for item in raw if item and item.strip()]


def _join_items(value: object, sep: str = "、") -> str:
    return sep.join(_split_items(value))


def _parse_birth(value: str) -> tuple[int, int, int] | None:
    match = re.search(r"(\d{4})\s*[-./年]?\s*(\d{1,2})?\s*[-./月]?\s*(\d{1,2})?", value)
    if not match:
        return None
    year = int(match.group(1))
    month = int(match.group(2) or 1)
    day = int(match.group(3) or 1)
    if not (1900 <= year <= 2100 and 1 <= month <= 12 and 1 <= day <= 31):
        return None
    return year, month, day


def _age(birth: str, today: date | None = None) -> str:
    parsed = _parse_birth(birth)
    if not parsed:
        return ""
    year, month, day = parsed
    today = today or date.today()
    age = today.year - year - ((today.month, today.day) < (month, day))
    return str(age) if 14 <= age <= 80 else ""


def _degree(education: list[dict]) -> str:
    best = ""
    best_rank = 0
    for item in education:
        degree = _text(item.get("degree"))
        for keyword, rank in _DEGREE_RANK:
            if keyword in degree and rank > best_rank:
                best, best_rank = degree, rank
    return best


def _png_size(data: bytes) -> tuple[int, int] | None:
    if data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        return None
    width, height = struct.unpack(">II", data[16:24])
    return width, height


def _jpeg_size(data: bytes) -> tuple[int, int] | None:
    if data[:2] != b"\xff\xd8":
        return None
    index = 2
    size = len(data)
    while index + 9 < size:
        if data[index] != 0xFF:
            index += 1
            continue
        marker = data[index + 1]
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            index += 2
            continue
        segment = struct.unpack(">H", data[index + 2:index + 4])[0]
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                      0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            height, width = struct.unpack(">HH", data[index + 5:index + 9])
            return width, height
        index += 2 + segment
    return None


def normalize_photo(value: object) -> dict | None:
    """解析 ``data:image/...;base64,...``；格式不符或过大一律当没传。"""
    if not isinstance(value, str):
        return None
    match = re.match(r"^data:(image/[a-zA-Z+]+);base64,(.*)$", value.strip(), re.S)
    if not match:
        return None
    mime = match.group(1).lower()
    if mime not in _MIME_EXT:
        return None
    try:
        data = base64.b64decode(match.group(2), validate=True)
    except (binascii.Error, ValueError):
        return None
    if not data or len(data) > _MAX_PHOTO_BYTES:
        return None

    ext, _ = _MIME_EXT[mime]
    dimensions = _png_size(data) if ext == "png" else _jpeg_size(data)
    if not dimensions:
        return None
    width, height = dimensions
    if width <= 0 or height <= 0:
        return None
    return {
        "data": data,
        "mime": "image/jpeg" if ext == "jpg" else mime,
        "ext": ext,
        "width": width,
        "height": height,
    }


def photo_extent(photo: dict) -> tuple[int, int]:
    """按原图比例缩进 32 × 38 mm 的框，返回 EMU。"""
    width = int(photo.get("width") or 0)
    height = int(photo.get("height") or 0)
    if width <= 0 or height <= 0:
        width, height = 3, 4
    scale = min(_MAX_PHOTO_W_EMU / width, _MAX_PHOTO_H_EMU / height)
    return max(1, round(width * scale)), max(1, round(height * scale))


def _contact_rows(basic: dict, intent: dict, degree: str) -> list[list[dict]]:
    birth = _text(basic.get("birth"))
    first = [
        {"label": "年龄", "value": _age(birth)},
        {"label": "经验", "value": _text(basic.get("experience"))},
        {"label": "手机", "value": _text(basic.get("phone"))},
        {"label": "邮箱", "value": _text(basic.get("email"))},
    ]
    second = [
        {"label": "性别", "value": _text(basic.get("gender"))},
        {"label": "学历", "value": degree},
        {"label": "生日", "value": birth},
        {"label": "现居", "value": _text(basic.get("city"))},
    ]
    rows = [row for row in (first, second) if any(cell["value"] for cell in row)]
    return rows


def _intent_rows(intent: dict) -> list[list[dict]]:
    first = [
        {"label": "意向城市", "value": _text(intent.get("city"))},
        {"label": "意向岗位", "value": _text(intent.get("targetJob"))},
        {"label": "薪资要求", "value": _text(intent.get("salary"))},
        {"label": "期望行业", "value": _text(intent.get("industry"))},
    ]
    second = [{"label": "求职状态", "value": _text(intent.get("availability"))}]
    rows = [row for row in (first, second) if any(cell["value"] for cell in row)]
    return rows


def _education_block(education: list[dict]) -> dict | None:
    items: list[dict] = []
    for item in education:
        school = _text(item.get("school"))
        major = _text(item.get("major"))
        degree = _text(item.get("degree"))
        grade = _text(item.get("grade"))
        gpa = _text(item.get("gpa"))
        ranking = _text(item.get("ranking"))
        courses = _join_items(item.get("courses"))
        time = _text(item.get("time"))

        degree_text = degree
        if grade:
            degree_text = f"{degree}（{grade}）" if degree else grade

        score = gpa
        if ranking:
            score = f"{gpa}，{ranking}" if gpa else ranking

        lines = [
            {"label": "*学校名称", "value": school},
            {"label": "*专业名称", "value": major},
            {"label": "*学历", "value": degree_text},
            {"label": "*学分绩点", "value": score},
            {"label": "*核心课程", "value": courses},
        ]
        lines = [line for line in lines if line["value"]]
        if not lines and not time:
            continue
        items.append({"time": time, "degree": degree_text, "lines": lines})

    if not items:
        return None
    return {"kind": "education", "title": "教育经历", "items": items}


def _entry_items(entries: list[dict]) -> list[dict]:
    items: list[dict] = []
    for item in entries:
        title = _text(item.get("title"))
        role = _text(item.get("role"))
        time = _text(item.get("time"))
        bullets = [text for text in (_text(line) for line in item.get("bullets") or []) if text]
        if not any((title, role, time, bullets)):
            continue
        items.append({"time": time, "title": title, "role": role, "bullets": bullets})
    return items


def _entries_block(title: str, entries: list[dict], *, numbered: bool = True) -> dict | None:
    items = _entry_items(entries)
    if not items:
        return None
    return {"kind": "entries", "title": title, "items": items, "numbered": numbered}


def _skills_block(skills: list[dict], certificates: list, languages: list, interests: list) -> dict | None:
    certs = _split_items(certificates)
    groups: list[str] = []
    for group in skills:
        label = _text(group.get("label"))
        items = _split_items(group.get("items"))
        if not items:
            continue
        groups.append(f"{label} - {'、'.join(items)}" if label else "、".join(items))
    lines = [
        {"label": "证书", "value": "、".join(certs)},
        {"label": "技能", "value": "；".join(groups)},
        {"label": "语言", "value": "、".join(_split_items(languages))},
        {"label": "兴趣", "value": "、".join(_split_items(interests))},
    ]
    lines = [line for line in lines if line["value"]]
    if not lines:
        return None
    return {"kind": "labeled", "title": "技能证书", "items": lines}


def _summary_block(summary: str) -> dict | None:
    items: list[dict] = []
    for raw in summary.splitlines():
        line = raw.strip()
        if not line:
            continue
        lead = ""
        text = line
        match = re.match(r"^([^：:，。；\s]{1,10})[：:]\s*(.+)$", line)
        if match:
            lead, text = match.group(1), match.group(2).strip()
        if text:
            items.append({"lead": lead, "text": text})
    if not items:
        return None
    return {"kind": "paragraphs", "title": "自我评价", "items": items}


def build_resume_content(resume: dict) -> dict:
    """把 ``ResumeData.model_dump()`` 整理成与排版无关的版块列表。"""
    basic = resume.get("basic") or {}
    intent = resume.get("intent") or {}
    education = resume.get("education") or []
    skills = resume.get("skills") or []
    summary = _text(resume.get("summary"))
    degree = _degree(education)

    blocks = [
        {"kind": "grid", "title": "求职意向", "rows": _intent_rows(intent)},
        _education_block(education),
        _entries_block("工作经历", resume.get("internships") or []),
        _entries_block("项目经历", resume.get("projects") or []),
        _entries_block("校园实践", resume.get("campus") or []),
        _entries_block("研究经历", resume.get("research") or [], numbered=False),
        _skills_block(
            skills,
            resume.get("certificates") or [],
            resume.get("languages") or [],
            resume.get("interests") or [],
        ),
        _summary_block(summary),
    ]

    return {
        "name": _text(basic.get("name")) or "个人简历",
        "photo": normalize_photo(resume.get("photo")),
        "contacts": _contact_rows(basic, intent, degree),
        "blocks": [block for block in blocks if block],
    }
