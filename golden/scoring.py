"""黄金评测集的纯计分逻辑。

这里**不调模型、不写业务文件**：输入是工作流结果，输出是逐条检查项。
判定只依赖确定性的知识库（岗位目录 / 项目库 / 技能词表）和结果字段本身，
因此同一条用例在改 prompt、换模型之后可以直接比较通过与否。

判定分两类：
1. 通用不变量（每条用例都查）—— 例如「已满足」必须有直接证据、
   高优先级缺口必须进学习路线、项目 covers_gaps 不能拿已满足的技能充数。
   这些是产品底线，不靠人工在 YAML 里逐条重复。
2. 用例专属期望 —— 人工在 cases.yaml 里写的必须出现 / 必须不出现 /
   必须未满足 / 必须覆盖等。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from agents.job_catalog import VALID_DOMAINS, JobCatalog
from agents.project_knowledge import ProjectKnowledgeBase
from agents.skill_dict import SkillVocabulary
from agents.utils import resolve_canonical

DEFAULT_CASES_PATH = Path(__file__).with_name("cases.yaml")

DISPLAY_STATUSES = ("已满足", "疑似具备", "未验证", "部分满足", "明显不足")
RAW_STATUSES = ("已满足", "部分满足", "明显不足")
VERIFICATIONS = ("direct", "inferred", "unverified")
PRIORITIES = ("高", "中", "低")
STRONG_EVIDENCE_KINDS = ("project", "growth")
SATISFIED = "已满足"


def normalize(text: object) -> str:
    """比较用的键：去空白、小写。"""
    return "".join(str(text or "").split()).lower()


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str = ""

    def to_dict(self) -> dict:
        return {"name": self.name, "ok": self.ok, "detail": self.detail}


@dataclass
class CaseReport:
    case_id: str
    checks: list[Check] = field(default_factory=list)
    meta: dict = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return all(check.ok for check in self.checks)

    @property
    def failures(self) -> list[Check]:
        return [check for check in self.checks if not check.ok]

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append(Check(name=name, ok=bool(ok), detail=detail))

    def to_dict(self) -> dict:
        return {
            "id": self.case_id,
            "passed": self.passed,
            "failures": [check.to_dict() for check in self.failures],
            "checks": [check.to_dict() for check in self.checks],
            **self.meta,
        }


@dataclass
class EvalContext:
    catalog: JobCatalog
    projects: ProjectKnowledgeBase
    vocabulary: SkillVocabulary

    @classmethod
    def build(cls) -> "EvalContext":
        return cls(
            catalog=JobCatalog(),
            projects=ProjectKnowledgeBase(),
            vocabulary=SkillVocabulary(),
        )

    def canonical(self, name: object) -> str:
        text = str(name or "").strip()
        if not text:
            return ""
        try:
            resolved = resolve_canonical(text, self.vocabulary)
        except Exception:  # noqa: BLE001 - 词表异常时退回原名比较，不中断计分
            resolved = None
        return normalize(resolved or text)


def load_cases(path: str | Path = DEFAULT_CASES_PATH) -> list[dict]:
    """读取并做最基本的形状校验；内容校验在 tests/test_golden_cases.py。"""
    source = Path(path)
    data = yaml.safe_load(source.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("cases")
    if not isinstance(data, list) or not data:
        raise ValueError(f"黄金集应为非空用例列表：{source}")
    for index, case in enumerate(data, start=1):
        if not isinstance(case, dict) or not str(case.get("id") or "").strip():
            raise ValueError(f"第 {index} 条用例缺少 id：{source}")
    return data


def _requirements(result: dict) -> list[dict]:
    return [item for item in (result.get("job_requirements") or []) if isinstance(item, dict)]


def _gaps(result: dict) -> list[dict]:
    return [item for item in (result.get("skill_gaps") or []) if isinstance(item, dict)]


def _stages(result: dict) -> list[dict]:
    return [item for item in (result.get("learning_plan") or []) if isinstance(item, dict)]


def _project_items(result: dict) -> list[dict]:
    return [item for item in (result.get("project_plan") or []) if isinstance(item, dict)]


def _display(gap: dict) -> str:
    return str(gap.get("display_status") or gap.get("status") or "")


def _gap_detail(gap: dict) -> str:
    return (
        f"{gap.get('skill')}（display={_display(gap) or '—'}, "
        f"verification={gap.get('verification') or '—'}, "
        f"evidence_kind={gap.get('evidence_kind') or '—'}, "
        f"supported_by={gap.get('supported_by') or '—'}）"
    )


def _stage_text(stage: dict) -> str:
    return json.dumps(stage, ensure_ascii=False, default=str).lower()


def _check_workflow_shape(report: CaseReport, result: dict) -> None:
    requirements = _requirements(result)
    gaps = _gaps(result)
    stages = _stages(result)
    projects = _project_items(result)

    report.add(
        "workflow.requirements_present",
        bool(requirements),
        f"job_requirements={len(requirements)}",
    )
    report.add(
        "workflow.gaps_present",
        bool(gaps) or not requirements,
        f"skill_gaps={len(gaps)}",
    )
    report.add(
        "workflow.learning_present",
        bool(stages) or not gaps,
        f"learning_plan={len(stages)} 阶段",
    )
    report.add(
        "workflow.projects_present",
        bool(projects),
        f"project_plan={len(projects)} 个",
    )

    seen: dict[str, str] = {}
    duplicates = []
    for req in requirements:
        key = normalize(req.get("skill"))
        if key in seen:
            duplicates.append(str(req.get("skill")))
        else:
            seen[key] = str(req.get("skill"))
    report.add(
        "requirements.no_duplicates",
        not duplicates,
        "重复：" + "、".join(duplicates) if duplicates else "无重复",
    )


def _check_assessment(report: CaseReport, gaps: list[dict], ctx: EvalContext) -> None:
    problems: list[str] = []
    for gap in gaps:
        display = _display(gap)
        status = str(gap.get("status") or "")
        verification = str(gap.get("verification") or "")
        priority = str(gap.get("priority") or "")
        evidence_kind = str(gap.get("evidence_kind") or "")
        supported_by = str(gap.get("supported_by") or "")

        if display not in DISPLAY_STATUSES:
            problems.append(f"{gap.get('skill')} 的 display_status 非法：{display or '空'}")
        if status not in RAW_STATUSES:
            problems.append(f"{gap.get('skill')} 的 status 非法：{status or '空'}")
        if verification not in VERIFICATIONS:
            problems.append(f"{gap.get('skill')} 的 verification 非法：{verification or '空'}")
        if priority not in PRIORITIES:
            problems.append(f"{gap.get('skill')} 的 priority 非法：{priority or '空'}")

        if display == SATISFIED:
            if status != SATISFIED:
                problems.append(f"{gap.get('skill')} 显示已满足但等级 status={status or '空'}")
            if verification != "direct":
                problems.append(f"{_gap_detail(gap)} 显示已满足，但不是直接证据")
            if evidence_kind not in STRONG_EVIDENCE_KINDS:
                problems.append(f"{_gap_detail(gap)} 显示已满足，证据类型却不够强")
        if verification == "direct":
            if supported_by in ("", "无"):
                problems.append(f"{_gap_detail(gap)} 是直接证据却没有支撑技能")
            elif evidence_kind not in STRONG_EVIDENCE_KINDS:
                problems.append(f"{_gap_detail(gap)} 是直接证据但证据类型不够强")
        if display == "疑似具备" and verification != "inferred":
            problems.append(f"{_gap_detail(gap)} 疑似具备只能来自跨技能推断")
        if display == "未验证" and verification != "unverified":
            problems.append(f"{_gap_detail(gap)} 未验证只能来自无强证据")
        if display in ("疑似具备", "未验证") and status != SATISFIED:
            problems.append(f"{_gap_detail(gap)} 只有等级已满足才可能降级为疑似/未验证")

    report.add(
        "assessment.honesty_invariant",
        not problems,
        "；".join(problems[:8]) + ("……" if len(problems) > 8 else "")
        if problems
        else f"{len(gaps)} 项缺口的等级与证据字段自洽",
    )


def _check_requirements(
    report: CaseReport, case: dict, result: dict, ctx: EvalContext
) -> None:
    expect = case.get("expect") or {}
    requirements = _requirements(result)
    keys = {ctx.canonical(item.get("skill")) for item in requirements}
    raw_names = [str(item.get("skill") or "") for item in requirements]

    includes = [str(name) for name in expect.get("requirements_include") or []]
    missing = [name for name in includes if ctx.canonical(name) not in keys]
    report.add(
        "requirements.include",
        not missing,
        "缺少：" + "、".join(missing) + f"；实际：{'、'.join(raw_names)}"
        if missing
        else f"均已出现（{len(includes)} 项）",
    )

    excludes = [str(name) for name in expect.get("requirements_exclude") or []]
    present = [name for name in excludes if ctx.canonical(name) in keys]
    report.add(
        "requirements.exclude",
        not present,
        "不应出现却出现了：" + "、".join(present)
        if present
        else f"未出现（{len(excludes)} 项跨领域干扰）",
    )


def _check_assessment_expectations(
    report: CaseReport, case: dict, result: dict, ctx: EvalContext
) -> None:
    expect = (case.get("expect") or {}).get("assessment") or {}
    gap_map = {ctx.canonical(gap.get("skill")): gap for gap in _gaps(result)}

    must_fail = [str(name) for name in expect.get("not_satisfied") or []]
    problems: list[str] = []
    for name in must_fail:
        gap = gap_map.get(ctx.canonical(name))
        if gap is None:
            problems.append(f"{name} 没有出现在能力差距里")
        elif _display(gap) == SATISFIED:
            problems.append(_gap_detail(gap))
    report.add(
        "assessment.not_satisfied",
        not problems,
        "应未满足却已满足：" + "；".join(problems)
        if problems
        else f"{len(must_fail)} 项均未判为已满足",
    )

    must_pass = [str(name) for name in expect.get("satisfied") or []]
    problems = []
    for name in must_pass:
        gap = gap_map.get(ctx.canonical(name))
        if gap is None:
            problems.append(f"{name} 没有出现在能力差距里")
        elif _display(gap) != SATISFIED:
            problems.append(_gap_detail(gap))
    report.add(
        "assessment.satisfied",
        not problems,
        "应满足却未满足：" + "；".join(problems)
        if problems
        else f"{len(must_pass)} 项均判为已满足",
    )


def _check_learning(
    report: CaseReport, case: dict, result: dict, ctx: EvalContext
) -> None:
    expect = (case.get("expect") or {}).get("learning") or {}
    stages = _stages(result)
    covers = {
        ctx.canonical(name)
        for stage in stages
        for name in (stage.get("covers") or [])
        if str(name).strip()
    }

    must_cover = [str(name) for name in expect.get("cover") or []]
    missing = [name for name in must_cover if ctx.canonical(name) not in covers]
    report.add(
        "learning.cover",
        not missing,
        "未覆盖：" + "、".join(missing) if missing else f"{len(must_cover)} 项均已覆盖",
    )

    forbidden = [str(word) for word in expect.get("forbid_keywords") or []]
    all_text = " ".join(_stage_text(stage) for stage in stages)
    hits = [word for word in forbidden if normalize(word) in all_text]
    report.add(
        "learning.forbid_keywords",
        not hits,
        "出现不应教的内容：" + "、".join(hits) if hits else f"未出现（{len(forbidden)} 个词）",
    )

    high_gaps = [gap for gap in _gaps(result) if str(gap.get("priority") or "") == "高"]
    uncovered = [
        str(gap.get("skill")) for gap in high_gaps
        if ctx.canonical(gap.get("skill")) not in covers
    ]
    report.add(
        "learning.high_priority_covered",
        not uncovered,
        "高优先级缺口未进学习路线：" + "、".join(uncovered)
        if uncovered
        else f"{len(high_gaps)} 个高优先级缺口均已覆盖",
    )


def _check_projects(
    report: CaseReport, case: dict, result: dict, ctx: EvalContext
) -> None:
    expect = (case.get("expect") or {}).get("projects") or {}
    projects = _project_items(result)
    gap_map = {ctx.canonical(gap.get("skill")): gap for gap in _gaps(result)}

    allowed = [str(domain) for domain in expect.get("allowed_domains") or []]
    known: list[tuple[str, str | None]] = []
    wrong: list[str] = []
    for project in projects:
        name = str(project.get("name") or "").strip()
        template = ctx.projects.get(name) if name else None
        if template is None:
            continue
        domain = str(template.get("domain") or "").strip() or None
        known.append((name, domain))
        if allowed and domain not in allowed:
            wrong.append(f"{name}（domain={domain or '无'}）")
    report.add(
        "projects.domain",
        not wrong,
        "跨领域项目：" + "、".join(wrong)
        if wrong
        else f"知识库项目 {len(known)} 个，领域均合规"
        if allowed
        else "未限定领域（未知岗位，跳过）",
    )

    minimum = int(expect.get("min_from_kb") or 0)
    report.add(
        "projects.min_from_kb",
        len(known) >= minimum,
        f"知识库模板 {len(known)} 个，要求至少 {minimum} 个",
    )

    problems: list[str] = []
    for project in projects:
        for name in project.get("covers_gaps") or []:
            gap = gap_map.get(ctx.canonical(name))
            if gap is None:
                problems.append(f"{project.get('name')} 的 covers_gaps 含非缺口技能：{name}")
            elif _display(gap) == SATISFIED:
                problems.append(f"{project.get('name')} 把已满足的 {name} 当作缺口")
    report.add(
        "projects.covers_only_unmet",
        not problems,
        "；".join(problems[:8]) if problems else "covers_gaps 全部指向未满足的缺口",
    )


def _check_job_profile(
    report: CaseReport, case: dict, result: dict, ctx: EvalContext
) -> None:
    expected_domain = (case.get("expect") or {}).get("domain")
    actual_domain = ctx.catalog.resolve_domain(case.get("target_job"))
    report.add(
        "job.domain",
        actual_domain == expected_domain,
        f"期望 {expected_domain or '无（未知岗位）'}，实际 {actual_domain or '无（未知岗位）'}",
    )

    origin = str(result.get("job_profile_origin") or "")
    if expected_domain is None:
        report.add(
            "job.profile_origin",
            origin == "unavailable",
            f"未知岗位不应伪造岗位画像：origin={origin or '空'}",
        )
    else:
        report.add(
            "job.profile_origin",
            origin == "knowledge_base",
            f"知识库岗位应使用人工画像：origin={origin or '空'}",
        )

    expected_basis = (case.get("expect") or {}).get("requirement_basis")
    if expected_basis:
        actual = result.get("requirement_basis")
        report.add(
            "job.requirement_basis",
            actual == expected_basis,
            f"期望 {expected_basis}，实际 {actual or '空'}",
        )


def evaluate_case(case: dict, result: dict, ctx: EvalContext) -> CaseReport:
    """用一条用例核对一次工作流结果。"""
    report = CaseReport(
        case_id=str(case.get("id") or ""),
        meta={
            "target_job": case.get("target_job"),
            "requirement_basis": result.get("requirement_basis"),
            "job_profile_origin": result.get("job_profile_origin"),
        },
    )
    _check_workflow_shape(report, result)
    _check_assessment(report, _gaps(result), ctx)
    _check_job_profile(report, case, result, ctx)
    _check_requirements(report, case, result, ctx)
    _check_assessment_expectations(report, case, result, ctx)
    _check_learning(report, case, result, ctx)
    _check_projects(report, case, result, ctx)
    return report


def summarize(reports: list[CaseReport]) -> dict:
    total = len(reports)
    passed = sum(1 for report in reports if report.passed)
    failed_ids = [report.case_id for report in reports if not report.passed]
    return {
        "total": total,
        "passed": passed,
        "failed": total - passed,
        "pass_rate": round(passed / total, 4) if total else 0.0,
        "failed_ids": failed_ids,
    }


def regression_diff(current: list[CaseReport], baseline: dict) -> dict:
    """对比上一份 JSON 报告，分清"变坏"与"修好"。"""
    before = {
        str(item.get("id")): bool(item.get("passed"))
        for item in (baseline.get("cases") or [])
        if isinstance(item, dict)
    }
    now = {report.case_id: report.passed for report in current}
    regressions = sorted(cid for cid, ok in now.items() if before.get(cid) and not ok)
    fixes = sorted(cid for cid, ok in now.items() if before.get(cid) is False and ok)
    new_cases = sorted(cid for cid in now if cid not in before)
    missing = sorted(cid for cid in before if cid not in now)
    return {
        "regressions": regressions,
        "fixes": fixes,
        "new": new_cases,
        "missing": missing,
    }


__all__ = [
    "DEFAULT_CASES_PATH",
    "DISPLAY_STATUSES",
    "EvalContext",
    "CaseReport",
    "Check",
    "VALID_DOMAINS",
    "evaluate_case",
    "load_cases",
    "normalize",
    "regression_diff",
    "summarize",
]
