"""请求 / 响应模型。

字段命名与 ``graph/state.py`` 的 ``CareerState`` 对齐，前端可以直接把它们
塞进 ``workflow.invoke``。画像**不做后端持久化** —— 由前端 localStorage 保存，
每次请求携带（见 :class:`AnalyzeRequest`）。

被测试锁定的两个模型（不要改字段名）：
- :class:`AnalyzeRequest` —— tests/test_readonly_boundary.py 直接构造
- :class:`CompleteTaskRequest` —— tests/test_growth_loop_api.py 校验 422 行为
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, StringConstraints, model_validator

from backend import config

__all__ = [
    "UserProfileRequest", "AnalyzeRequest", "CompleteTaskRequest",
    "AttachEvidenceRequest",
    "NextRoundRequest", "ManualGrowthEventRequest", "EvidenceText",
    "WithdrawPendingRequest", "VoidGrowthEventRequest", "LlmChannelRequest",
]


class UserProfileRequest(BaseModel):
    """学生个人信息。等价于 ``CareerState["user_profile"]`` 的四字段。"""

    major: str = Field(default="", description="专业")
    grade: str = Field(default="", description="年级")
    skills: list[str] = Field(default_factory=list, description="自述技能")
    projects: list[str] = Field(default_factory=list, description="项目经历")


# 证据文本：先去掉首尾空白，再校验长度。
# 顺序很重要 —— 先 strip 才能让 "   " / "\n\t" 这类纯空白被判为空而拒绝（422），
# 否则它们会以"长度 3"混过 min_length 检查，被原样写进 loop.json。
EvidenceText = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=config.EVIDENCE_MAX_LENGTH,
    ),
]

# 手动补录成果（/api/growth/manual-event）的字段上限。动机与证据文本同款：
# loop.json 是全量读-改-写的，任何无界写入都会拖慢所有人的后续查询。
ManualEventTitle = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=config.MANUAL_TITLE_MAX_CHARS),
]
ManualSkill = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=config.MANUAL_SKILL_MAX_CHARS),
]


class AnalyzeRequest(BaseModel):
    """``POST /api/analyze`` 的请求体。

    对应 ``workflow.invoke`` 的输入：画像 + 目标岗位 + 可选成长事件。

    ``jd_text``
    -----------
    用户**当场粘贴**的招聘要求原文。传了它，岗位要求就以这段原文为唯一依据
    （见 ``agents.user_jd``），每条要求都能反查到原话的第几句。不传时岗位要求
    全是模型推断，前端会如实标灰并**不给**匹配度百分比。
    """

    user_profile: UserProfileRequest
    target_job: str = Field(
        min_length=1, max_length=config.TARGET_JOB_MAX_CHARS, description="目标岗位，不能为空"
    )
    growth_events: list[dict[str, Any]] = Field(
        default_factory=list,
        max_length=config.GROWTH_EVENTS_MAX_ITEMS,
        description="本轮新完成的学习/项目成果；直接透传给 growth_node，由业务层校验",
    )
    jd_text: str | None = Field(
        default=None,
        max_length=config.JD_TEXT_MAX_CHARS,
        description="用户粘贴的招聘要求原文，本次岗位要求的唯一依据",
    )
    new_plan: bool = Field(
        default=False,
        description=(
            "已有轮次时是否明确把本次分析另存为新的一轮计划。"
            "默认 False = 只预览，不新建轮次；旧轮次与任务原样保留"
        ),
    )

    def to_state(self) -> dict:
        """转成 ``CareerState`` 的输入子集。"""
        state: dict = {
            "user_profile": self.user_profile.model_dump(),
            "target_job": self.target_job,
            "growth_events": [dict(item) for item in self.growth_events],
        }
        if self.jd_text and self.jd_text.strip():
            state["jd_text"] = self.jd_text.strip()
        return state


class CompleteTaskRequest(BaseModel):
    """``POST /api/tasks/{task_id}/complete`` 的请求体。

    **点提交即完成**：``evidence`` 与 ``note`` 都是可选的。

    - 不填 ``evidence`` → 记成 ``self_report``（自述完成）。技能照既有规则升级，
      但评估只算"未验证"；之后可以再补凭证。
    - 填了 ``evidence`` → 记成 ``growth``（附凭证），才算直接证据。
    - ``note`` 是心得，单独保存，**不并入 evidence、也不当凭证**。
    """

    evidence: EvidenceText | None = Field(
        default=None, description="可选：完成凭证（链接/产出/截图说明），填了才算直接证据"
    )
    profile_skills: list[dict[str, Any]] | None = Field(
        default=None,
        description=(
            "可选：最近一次分析得到的能力表，用于让「升级预览」与下一轮 growth_node "
            "使用同一基线（见 agents/growth_loop.py 的 _evaluate_event）。"
            "不传则退回成长历史基线，预览会比真实判定更严格。"
        ),
    )
    note: Annotated[
        str,
        StringConstraints(strip_whitespace=True, max_length=config.NOTE_MAX_CHARS),
    ] | None = Field(default=None, description="可选：心得笔记（写不写都能完成）")

    @model_validator(mode="after")
    def check_total_length(self):
        total = len(self.evidence or "") + len(self.note or "")
        if total > config.EVIDENCE_MAX_LENGTH:
            raise ValueError(
                f"证据与心得笔记的合计不能超过 {config.EVIDENCE_MAX_LENGTH} 字"
            )
        return self


class AttachEvidenceRequest(BaseModel):
    """``POST /api/tasks/{task_id}/evidence`` 的请求体：给已完成任务补凭证。"""

    evidence: EvidenceText = Field(description="补上的凭证：链接、产出或截图说明")


class ClearTasksRequest(BaseModel):
    """``POST /api/tasks/clear`` 的请求体（整体可选）。

    ``drop_pending_events`` 默认 False：清单里如果有**还没进成长历史**的打卡成果，
    服务层会拒绝清空并回 ``needs_confirmation``，前端必须让用户确认后才带着
    ``true`` 重试 —— 那些成果是真实进度，不能在一次"清理界面"里被顺手冲掉。
    """

    drop_pending_events: bool = Field(
        default=False,
        description="确认连同尚未记进成长历史的打卡成果一起删除",
    )


class WithdrawPendingRequest(BaseModel):
    """``POST /api/growth/pending/withdraw`` 的请求体：撤回一条待处理成果。

    只撤回 ``loop.json`` 里那条完成记录（任务退回待完成），不碰成长历史。
    """

    task_id: str = Field(
        min_length=1,
        max_length=64,
        description="任务 ID，例如 r001-l01",
    )
    round_no: int | None = Field(default=None, ge=1, description="可选：限定轮次")


class VoidGrowthEventRequest(BaseModel):
    """``POST /api/growth/history/void`` 的请求体：作废一条已入账成果。

    不删原记录，只追加一条调整记录并重算能力表。``entry_index`` /
    ``event_index`` 来自 ``GET /api/growth/history`` 返回数组的下标。
    """

    entry_index: int = Field(ge=0, description="历史条目下标（0 开始）")
    event_index: int = Field(ge=0, description="条目内成果下标（0 开始）")
    reason: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=2, max_length=200),
    ] = Field(description="作废原因，会写进调整记录")


class NextRoundRequest(BaseModel):
    """``POST /api/growth/next-round`` 的请求体（整体可选）。

    测试以**无 body** 的方式触发该接口（tests/test_growth_next_round.py），
    所以整份 body 必须可缺省；缺省时由服务层从 ``loop.json`` 的最近一轮取
    ``target_job``，画像则退化为空 —— 那样 profile 节点会明确报错并使该次
    run 标记为 failed，**不会伪造出一轮计划**。
    """

    user_profile: UserProfileRequest | None = None
    target_job: str | None = Field(default=None, max_length=config.TARGET_JOB_MAX_CHARS)
    jd_text: str | None = Field(
        default=None,
        max_length=config.JD_TEXT_MAX_CHARS,
        description="沿用上一轮的招聘要求原文；不传则这一轮没有原文依据",
    )


class ManualGrowthEventRequest(BaseModel):
    """``POST /api/growth/manual-event`` 的请求体：手动补录一条成长成果。

    用于打破"空系统死锁"：全新用户还没有任何任务时，也可以直接提交一条
    自己学过/做过的成果，让「生成下一轮」有内容可处理。

    字段上限与任务证据同一套口径（evidence 直接复用 :data:`EvidenceText`，
    也是 5000 字）—— loop.json 是全量读-改-写的，无界写入会拖慢所有后续查询。
    ``evidence`` 可选：不填就是自述成果，升级照规则走，但评估按未验证处理。
    """

    kind: str = Field(description="成果类型，learning|project")
    title: ManualEventTitle = Field(description="成果标题")
    skills: list[ManualSkill] = Field(
        default_factory=list,
        max_length=config.MANUAL_SKILLS_MAX_ITEMS,
        description="成果关联技能",
    )
    evidence: EvidenceText | None = Field(
        default=None, description="可选：成果凭证（具体学了/做了什么，有产出再填）"
    )
    target_job: str | None = Field(default=None, max_length=config.TARGET_JOB_MAX_CHARS, description="目标岗位（可选，首次建任务时用）")


class LlmChannelRequest(BaseModel):
    """``PUT /api/status/llm-channel`` 的请求体：切换模型通道。

    非法值由 Literal 直接拦成 422，业务层不用再校验。
    """

    channel: Literal["local", "cloud"] = Field(description="目标通道，local|cloud")