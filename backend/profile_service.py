"""账号基础资料的读写。

基础资料是**账号级**的：换岗位、开新档案都不清空它。但它只作为新档案的
草稿与快照来源；修改这里不会追溯改写任何已创建档案的 ``profile_snapshot_json``。

版本号用于乐观并发：前端提交它读到的 ``version``，不一致返回 409，避免两个
页面互相覆盖。没有资料时 ``version=0``，第一次保存得到 1。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from backend.models import UserProfile

__all__ = ["VersionConflict", "get_profile", "put_profile"]

MAX_MAJOR = 64
MAX_GRADE = 32
MAX_SKILLS = 200
MAX_PROJECTS = 100
MAX_ITEM = 200


class VersionConflict(RuntimeError):
    """提交的版本与库中版本不一致。"""


def _clean_list(values, limit: int) -> list[str]:
    cleaned: list[str] = []
    for raw in values or []:
        text = str(raw).strip()
        if not text:
            continue
        cleaned.append(text[:MAX_ITEM])
        if len(cleaned) >= limit:
            break
    return cleaned


def _as_dict(row: UserProfile | None) -> dict:
    if row is None:
        return {"major": "", "grade": "", "skills": [], "projects": [], "version": 0}
    return {
        "major": row.major or "",
        "grade": row.grade or "",
        "skills": list(row.skills_json or []),
        "projects": list(row.projects_json or []),
        "version": int(row.version or 0),
    }


def get_profile(factory: sessionmaker, user_id: int) -> dict:
    with factory() as session:
        return _as_dict(session.get(UserProfile, int(user_id)))


def put_profile(
    factory: sessionmaker, user_id: int, payload: dict, expected_version: int
) -> dict:
    major = str(payload.get("major") or "").strip()[:MAX_MAJOR]
    grade = str(payload.get("grade") or "").strip()[:MAX_GRADE]
    skills = _clean_list(payload.get("skills"), MAX_SKILLS)
    projects = _clean_list(payload.get("projects"), MAX_PROJECTS)

    try:
        with factory.begin() as session:
            row = session.scalar(
                select(UserProfile)
                .where(UserProfile.user_id == int(user_id))
                .with_for_update()
            )
            current = int(row.version) if row is not None else 0
            if int(expected_version) != current:
                raise VersionConflict(
                    f"基础资料已被其他页面更新（当前版本 {current}），请刷新后重试"
                )
            if row is None:
                row = UserProfile(
                    user_id=int(user_id),
                    major=major,
                    grade=grade,
                    skills_json=skills,
                    projects_json=projects,
                    version=1,
                )
                session.add(row)
            else:
                row.major = major
                row.grade = grade
                row.skills_json = skills
                row.projects_json = projects
                row.version = current + 1
            session.flush()
            return _as_dict(row)
    except IntegrityError as exc:  # 两个请求同时创建同一行
        raise VersionConflict("基础资料已被其他页面更新，请刷新后重试") from exc


def snapshot_for_plan(factory: sessionmaker, user_id: int) -> dict:
    """创建档案时复制的资料快照（不含版本号）。"""
    data = get_profile(factory, user_id)
    data.pop("version", None)
    return data


def write_snapshot(
    session: Session,
    user_id: int,
    profile: dict,
    *,
    bump_version: bool = True,
) -> UserProfile:
    """在调用方事务内写入资料（创建档案时同步保存用户本次填写的值）。"""
    row = session.get(UserProfile, int(user_id))
    skills = _clean_list(profile.get("skills"), MAX_SKILLS)
    projects = _clean_list(profile.get("projects"), MAX_PROJECTS)
    if row is None:
        row = UserProfile(
            user_id=int(user_id),
            major=str(profile.get("major") or "").strip()[:MAX_MAJOR],
            grade=str(profile.get("grade") or "").strip()[:MAX_GRADE],
            skills_json=skills,
            projects_json=projects,
            version=1,
        )
        session.add(row)
    elif bump_version:
        major = str(profile.get("major") or "").strip()[:MAX_MAJOR]
        grade = str(profile.get("grade") or "").strip()[:MAX_GRADE]
        if (
            row.major != major
            or row.grade != grade
            or list(row.skills_json or []) != skills
            or list(row.projects_json or []) != projects
        ):
            row.major = major
            row.grade = grade
            row.skills_json = skills
            row.projects_json = projects
            row.version = int(row.version or 0) + 1
    return row
