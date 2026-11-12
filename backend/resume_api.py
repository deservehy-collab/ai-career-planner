"""简历导出接口。

只做两件事：校验内容边界、把清洗后的简历交给
:mod:`backend.resume_docx` 生成字节流。**不读也不写任何业务数据**，
因此没有档案归属、事务或后台任务生命周期问题；登录仍是硬要求，
避免匿名请求把服务当文件生成器用。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field, StringConstraints

from backend.auth import get_current_user
from backend.resume_content import normalize_photo
from backend.resume_docx import build_resume_docx
from backend.resume_pdf import ResumePdfUnavailable, build_resume_pdf

__all__ = ["router"]

router = APIRouter(tags=["resume"])

_NAME = Annotated[str, StringConstraints(strip_whitespace=True, max_length=60)]
_TEXT = Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)]
_LINE = Annotated[str, StringConstraints(strip_whitespace=True, max_length=500)]
_ITEM = Annotated[str, StringConstraints(strip_whitespace=True, max_length=100)]

_DOCX_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)


class ResumeBasic(BaseModel):
    name: _NAME = ""
    gender: _ITEM = ""
    birth: _ITEM = ""
    phone: _ITEM = ""
    email: _TEXT = ""
    city: _ITEM = ""
    experience: _ITEM = ""


class ResumeIntent(BaseModel):
    targetJob: _TEXT = ""
    city: _ITEM = ""
    salary: _ITEM = ""
    industry: _TEXT = ""
    availability: _ITEM = ""


class ResumeEducation(BaseModel):
    school: _TEXT = ""
    major: _TEXT = ""
    degree: _ITEM = ""
    time: _ITEM = ""
    grade: _ITEM = ""
    gpa: _ITEM = ""
    ranking: _ITEM = ""
    courses: _TEXT = ""


class ResumeSkillGroup(BaseModel):
    label: _ITEM = ""
    items: list[_ITEM] = Field(default_factory=list, max_length=50)


class ResumeEntry(BaseModel):
    title: _TEXT = ""
    role: _TEXT = ""
    time: _ITEM = ""
    bullets: list[_LINE] = Field(default_factory=list, max_length=20)


class ResumeData(BaseModel):
    basic: ResumeBasic = Field(default_factory=ResumeBasic)
    intent: ResumeIntent = Field(default_factory=ResumeIntent)
    education: list[ResumeEducation] = Field(default_factory=list, max_length=10)
    skills: list[ResumeSkillGroup] = Field(default_factory=list, max_length=20)
    internships: list[ResumeEntry] = Field(default_factory=list, max_length=20)
    projects: list[ResumeEntry] = Field(default_factory=list, max_length=20)
    campus: list[ResumeEntry] = Field(default_factory=list, max_length=20)
    research: list[ResumeEntry] = Field(default_factory=list, max_length=20)
    certificates: list[_TEXT] = Field(default_factory=list, max_length=30)
    languages: list[_ITEM] = Field(default_factory=list, max_length=10)
    interests: list[_ITEM] = Field(default_factory=list, max_length=10)
    summary: Annotated[str, StringConstraints(strip_whitespace=True, max_length=3000)] = ""
    photo: Annotated[str, StringConstraints(strip_whitespace=True, max_length=4_000_000)] = ""


class ResumeExportRequest(BaseModel):
    resume: ResumeData


def _has_content(resume: ResumeData) -> bool:
    basic = resume.basic
    if any((
        basic.name,
        basic.gender,
        basic.birth,
        basic.phone,
        basic.email,
        basic.city,
        basic.experience,
    )):
        return True
    intent = resume.intent
    if any((intent.targetJob, intent.city, intent.salary, intent.industry, intent.availability)):
        return True
    if any(
        any((
            item.school,
            item.major,
            item.degree,
            item.time,
            item.grade,
            item.gpa,
            item.ranking,
            item.courses,
        ))
        for item in resume.education
    ):
        return True
    if any(group.label or group.items for group in resume.skills):
        return True
    if any(item.title or item.role or item.time or item.bullets for item in (
        [*resume.internships, *resume.projects, *resume.campus, *resume.research]
    )):
        return True
    return bool(
        resume.certificates
        or resume.languages
        or resume.interests
        or resume.summary
    )


def _check_photo(resume: ResumeData) -> None:
    if resume.photo and normalize_photo(resume.photo) is None:
        raise HTTPException(
            status_code=422,
            detail="照片无法使用：请上传 2MB 以内的 JPG 或 PNG 图片，或先移除照片。",
        )


@router.post(
    "/resume/docx",
    summary="把当前简历导出为 Word（不写业务数据）",
    response_class=Response,
)
def export_resume_docx(
    payload: ResumeExportRequest,
    _current: dict = Depends(get_current_user),
) -> Response:
    if not _has_content(payload.resume):
        raise HTTPException(
            status_code=422,
            detail="简历还是空的：先填写姓名、求职意向或任意一段经历再导出。",
        )
    _check_photo(payload.resume)

    document = build_resume_docx(payload.resume.model_dump())
    return Response(
        content=document,
        media_type=_DOCX_MEDIA_TYPE,
        headers={"Content-Disposition": 'attachment; filename="resume.docx"'},
    )


@router.post(
    "/resume/pdf",
    summary="把当前简历导出为 PDF（不写业务数据）",
    response_class=Response,
)
def export_resume_pdf(
    payload: ResumeExportRequest,
    _current: dict = Depends(get_current_user),
) -> Response:
    if not _has_content(payload.resume):
        raise HTTPException(
            status_code=422,
            detail="简历还是空的：先填写姓名、求职意向或任意一段经历再导出。",
        )
    _check_photo(payload.resume)

    try:
        document = build_resume_pdf(payload.resume.model_dump())
    except ResumePdfUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    return Response(
        content=document,
        media_type="application/pdf",
        headers={"Content-Disposition": 'attachment; filename="resume.pdf"'},
    )
