"""Profile Agent：分析学生个人信息，输出结构化技能画像。"""

from typing import Literal

from pydantic import BaseModel, Field, field_validator

from agents.llm import structured_invoke
from agents.utils import check_non_empty, dedupe_by, log, normalize_level
from graph.state import CareerState, check_update

PROMPT = """你是一名资深职业规划师。请分析下面这名大学生的个人信息，
提取他已掌握的技能、技能水平，以及优势和短板。

【学生信息】
专业：{major}
年级：{grade}
自述技能：{skills}
项目经历：{projects}

【评级标准】必须严格按证据分级，不要因为技能出现在自述里就给高分：
- 入门：只是听说过或跟着教程/演示练过，说不清原理
- 初级：能在指导下完成简单任务，不熟常见坑
- 中级：能独立完成常规任务，遇到问题能自己查资料解决
- 高级：能独立主导、做过优化或踩坑总结，能讲清背后的原理与规范

【要求】
1. 只能依据上面给出的信息判断，不要虚构学生没有的经历和技能。
2. 技能按**该专业与其目标方向的真实技能**理解，不限于信息技术领域：
   护理专业的"静脉输液操作"、会计专业的"财务报表编制"与"Python"同等对待。
3. level 只能从这四档里选：入门、初级、中级、高级。
4. 如果某项技能只出现在"自述技能"里、没有任何项目经历佐证，最高只能给"中级"。
5. evidence 要写明判断依据，例如"在××项目中实际使用过"；
   依据相同也不要机械复制同一句话，要结合该技能本身说明。
   evidence_kind 只能填 project 或 self_report：
   - project：依据来自"项目经历"里的具体项目；
   - self_report：只来自"自述技能"，没有项目经历佐证。
   项目经历为空时，所有技能都必须是 self_report，不得凭空写成 project。
6. strengths 给 2~4 条优势，weaknesses 给 2~4 条短板，每条一句话。
   短板要具体到能力（如"缺少独立负责一个完整项目的经验"），
   不要写"需加强时间管理"这类空话。
7. 全部用中文。"""


class Skill(BaseModel):
    """单个技能的画像。"""

    skill: str = Field(description="技能名称，可以是任何专业领域的技能")
    level: Literal["入门", "初级", "中级", "高级"] = Field(description="掌握水平")
    evidence: str = Field(description="判断依据，来自自述或项目经历")
    evidence_kind: Literal["project", "self_report"] = Field(
        default="self_report",
        description="依据类型：project=来自项目经历，self_report=只来自自述技能",
    )

    @field_validator("level", mode="before")
    @classmethod
    def _normalize_level(cls, value):
        # 小模型常常冒出"熟练掌握"这类说法，先归一化再交给 Literal 校验
        return normalize_level(value)


class ProfileAnalysis(BaseModel):
    """Profile Agent 的结构化输出。"""

    skills: list[Skill] = Field(description="学生已掌握的技能列表")
    strengths: list[str] = Field(description="优势")
    weaknesses: list[str] = Field(description="短板")


def _clean_list(value) -> list[str]:
    """把「技能 / 项目经历」统一成去空白后的字符串列表（字符串也接受）。"""
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item or "").strip()]


def profile_node(state: CareerState) -> dict:
    """LangGraph 节点：读取 user_profile，写入 user_skills / strengths / weaknesses。"""
    profile = state.get("user_profile") or {}
    if not profile:
        raise ValueError("state 中缺少 user_profile，无法分析学生能力")

    raw_skills = _clean_list(profile.get("skills"))
    raw_projects = _clean_list(profile.get("projects"))
    # 零基础也能开始规划：没有任何可分析的信息时，不调模型凭空编造"优势"，
    # 直接交出一张空能力表，让下游按岗位要求如实列出"明显不足"的缺口。
    if not raw_skills and not raw_projects:
        log("Profile Agent")
        return check_update(
            {
                "user_skills": [],
                "user_strengths": [],
                "user_weaknesses": ["还没有填写任何技能或项目经历，本轮按零基础规划"],
            },
            "Profile Agent",
        )

    prompt = PROMPT.format(
        major=profile.get("major", "未知"),
        grade=profile.get("grade", "未知"),
        skills="、".join(profile.get("skills") or []) or "无",
        projects="、".join(profile.get("projects") or []) or "无",
    )

    analysis = structured_invoke(prompt, ProfileAnalysis)

    # 同一个技能可能被模型重复列出，去重后再交给下游
    skills = dedupe_by([s.model_dump() for s in analysis.skills], key="skill")
    check_non_empty(skills, "Profile Agent 的技能列表")

    # 代码兜底：没有项目经历时，任何"项目证据"都不可能是真的；
    # 证据文本为空的自述同样不能算项目证据。
    projects = profile.get("projects") or []
    if isinstance(projects, str):
        projects = [projects] if projects.strip() else []
    for item in skills:
        if not projects or not str(item.get("evidence") or "").strip():
            item["evidence_kind"] = "self_report"
        elif item.get("evidence_kind") not in ("project", "self_report"):
            item["evidence_kind"] = "self_report"

    log("Profile Agent")
    return check_update(
        {
            "user_skills": skills,
            "user_strengths": analysis.strengths,
            "user_weaknesses": analysis.weaknesses,
        },
        "Profile Agent",
    )
