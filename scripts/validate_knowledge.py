"""校验技能库、项目库与全部岗位画像的交叉引用。

检查内容
--------
1. YAML 能否正确解析、顶层结构是否正确。
2. 岗位画像：job_id / job_name / skills 必填；domain 合法；技能名不重复。
3. Skill：名称必须出现在某个岗位画像里、名称不重复、level_guides 四档完整、
   prerequisites 引用有效；AI 岗位首批 12 项仍须全部收录。
4. Project：字段完整且类型正确、covers_skills / prerequisites 引用有效、
   难度梯度合法；domain 必须是岗位目录里的真实领域，且每个领域至少有 3 个项目。
5. 输出知识库内容概览。

用法
----
    python scripts/validate_knowledge.py

任何校验失败都会以非零退出码报错，绝不静默忽略。
本脚本只读知识库源文件，不写入、不修改任何文件。
"""

import sys
from collections import Counter
from pathlib import Path

import yaml

# 允许作为脚本直接运行时找到 agents 包（脚本目录是 scripts/）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.external_sources import evidence_inventory  # noqa: E402
from agents.job_catalog import DEFAULT_JOB_DIR, VALID_DOMAINS, JobCatalog  # noqa: E402
from agents.project_knowledge import (  # noqa: E402
    DIFFICULTY_RANK,
    VALID_DIFFICULTY,
    ProjectKnowledgeBase,
)
from agents.skill_knowledge import SKILL_LEVELS, SkillKnowledgeBase  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# AI 岗位首批必须收录的技能（其余岗位按自己的画像收录即可）
AI_JOB_ID = "ai_application_engineer"
EXPECTED_AI_SKILLS = [
    "Python", "RAG", "Agent", "Prompt Engineering", "LLM API", "Function Calling",
    "后端 API 开发", "测试与效果评测", "Git", "Docker", "Embedding", "LangChain / LangGraph",
]

REQUIRED_PROJECT_FIELDS = {
    "name": str,
    "difficulty": str,
    "covers_skills": list,
    "prerequisites": list,
    "tech_stack": list,
    "stage_hint": str,
    "deliverable": str,
    "acceptance_criteria": list,
    "estimated_weeks": int,
}


def load_job_profiles() -> tuple[list[dict], dict[str, set[str]]]:
    """读取全部岗位画像，返回 (profiles, {domain: skill_names})。"""
    if not DEFAULT_JOB_DIR.is_dir():
        raise SystemExit(f"岗位目录不存在：{DEFAULT_JOB_DIR}")
    profiles: list[dict] = []
    by_domain: dict[str, set[str]] = {}
    seen_ids: set[str] = set()
    for path in sorted(DEFAULT_JOB_DIR.glob("*.yaml")):
        with path.open(encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        if not isinstance(data, dict):
            raise SystemExit(f"岗位画像顶层应为字典：{path}")
        job_id = str(data.get("job_id") or "").strip()
        job_name = str(data.get("job_name") or "").strip()
        if not job_id or not job_name:
            raise SystemExit(f"{path} 缺少 job_id 或 job_name")
        if job_id in seen_ids:
            raise SystemExit(f"岗位 ID 重复：{job_id}")
        seen_ids.add(job_id)
        domain = str(data.get("domain") or "").strip()
        if domain not in VALID_DOMAINS:
            raise SystemExit(f"岗位 {job_id!r} 的 domain={domain!r} 非法")
        skills = data.get("skills")
        if not isinstance(skills, list) or not skills:
            raise SystemExit(f"岗位 {job_id!r} 缺少非空 skills 列表")
        names = []
        for item in skills:
            name = str((item or {}).get("skill") or "").strip()
            if not name:
                raise SystemExit(f"岗位 {job_id!r} 存在缺少 skill 字段的技能条目")
            names.append(name)
        if len(names) != len(set(names)):
            raise SystemExit(f"岗位 {job_id!r} 内技能名重复")
        if "ground_requirements" in data and not isinstance(data["ground_requirements"], bool):
            raise SystemExit(f"岗位 {job_id!r} 的 ground_requirements 必须是布尔值")
        if "profile" in data:
            profile = data.get("profile")
            if not isinstance(profile, dict):
                raise SystemExit(f"岗位 {job_id!r} 的 profile 应为字典")
            if not str(profile.get("summary") or "").strip():
                raise SystemExit(f"岗位 {job_id!r} 的 profile 缺少 summary")
            for key in ("core_tasks", "tools", "deliverables",
                        "entry_requirements", "career_path"):
                entries = profile.get(key)
                if not isinstance(entries, list) or not entries:
                    raise SystemExit(f"岗位 {job_id!r} 的 profile.{key} 应为非空列表")
                for entry in entries:
                    if isinstance(entry, str):
                        ok = bool(entry.strip())
                    elif isinstance(entry, dict):
                        ok = bool(str(entry.get("name") or "").strip())
                    else:
                        ok = False
                    if not ok:
                        raise SystemExit(f"岗位 {job_id!r} 的 profile.{key} 存在缺少 name 的条目")
        profiles.append(data)
        by_domain.setdefault(domain, set()).update(names)
    if not profiles:
        raise SystemExit("岗位目录下没有任何岗位画像")
    return profiles, by_domain


def validate_skills(skills: SkillKnowledgeBase, profile_names: set[str]) -> int:
    names = skills.all_skill_names()

    unknown = [n for n in names if n not in profile_names]
    if unknown:
        raise SystemExit(f"Skill 知识库含所有岗位画像之外的技能名：{unknown}")

    missing = [n for n in EXPECTED_AI_SKILLS if n not in set(names)]
    if missing:
        raise SystemExit(f"Skill 知识库缺少 AI 岗位首批要求技能：{missing}")

    for name in names:
        item = skills.get(name)
        for req in item.get("prerequisites") or []:
            if req not in names:
                raise SystemExit(f"技能 {name!r} 的前置技能 {req!r} 在知识库中不存在")

    print(f"[Skill] 收录技能 {len(names)} 项，前置引用全部有效")
    return len(names)


def validate_projects(projects: ProjectKnowledgeBase, profile_names: set[str],
                      domains: set[str]) -> int:
    items = projects.projects
    if len(items) < 4:
        raise SystemExit(f"Project 知识库项目数不足（预期 >=4，实际 {len(items)}）")

    difficulties = [DIFFICULTY_RANK[p["difficulty"]] for p in items]
    per_domain: Counter = Counter()
    for p in items:
        name = p["name"]
        for field_name, expected_type in REQUIRED_PROJECT_FIELDS.items():
            value = p.get(field_name)
            if value is None:
                raise SystemExit(f"项目 {name!r} 缺少字段 {field_name!r}")
            if not isinstance(value, expected_type):
                raise SystemExit(
                    f"项目 {name!r} 字段 {field_name!r} 类型错误："
                    f"应为 {expected_type.__name__}，实际 {type(value).__name__}"
                )
        if p["difficulty"] not in VALID_DIFFICULTY:
            raise SystemExit(f"项目 {name!r} 的 difficulty 非法：{p['difficulty']!r}")
        for key in ("covers_skills", "prerequisites"):
            for skill in p.get(key, []):
                if skill not in profile_names:
                    raise SystemExit(f"项目 {name!r} 的 {key} 引用了非规范技能：{skill!r}")
        domain = str(p.get("domain") or "").strip()
        if domain:
            if domain not in domains:
                raise SystemExit(f"项目 {name!r} 的 domain={domain!r} 不在岗位目录中")
            per_domain[domain] += 1

    if len(set(difficulties)) < 2:
        raise SystemExit("项目难度需有梯度（覆盖至少两档）")

    for domain in sorted(domains):
        if per_domain[domain] < 3:
            raise SystemExit(
                f"领域 {domain!r} 的项目模板不足（至少 3 个，实际 {per_domain[domain]}）"
            )

    print(f"[Project] 项目模板 {len(items)} 个：")
    for p in items:
        print(
            f"  - {p['name']} ｜领域 {p.get('domain') or '通用'} ｜难度 {p['difficulty']}"
            f" ｜覆盖 {len(p['covers_skills'])} 技能 ｜预计 {p.get('estimated_weeks')} 周"
        )
    for domain in sorted(per_domain):
        print(f"  · {domain}: {per_domain[domain]} 个")
    return len(items)


def validate_job_evidence(profiles: list[dict]) -> set[str]:
    """检查每份画像的 evidence 是否**成对可用**（source_id + 原句），并如实报覆盖率。

    没粘招聘原文时，岗位要求的可信度全靠这批登记（见 agents/external_sources.py）。
    缺任一半的条目运行时会静默丢掉，所以在这里直接判失败；
    某项技能压根没登记 evidence 只是覆盖率低，如实打印，不算错误。
    """
    source_ids: set[str] = set()
    for data in profiles:
        skills = data.get("skills") or []
        covered = 0
        broken: list[str] = []
        for item in skills:
            entries = [entry for entry in (item.get("evidence") or []) if entry]
            if not entries:
                continue
            covered += 1
            for entry in entries:
                if not isinstance(entry, dict):
                    broken.append(str(item.get("skill")))
                    continue
                if not str(entry.get("source_id") or "").strip() or not str(entry.get("note") or "").strip():
                    broken.append(str(item.get("skill")))
        if broken:
            raise SystemExit(
                f"岗位 {data['job_id']!r} 的 evidence 有缺 source_id 或原句的条目：{sorted(set(broken))}"
            )
        inventory = evidence_inventory(data)
        source_ids.update(inventory["source_ids"])
        print(
            f"  - 资料出处：{data['job_name']} {covered}/{len(skills)} 项技能有 evidence，"
            f"共 {inventory['entries']} 条、{len(inventory['source_ids'])} 份资料"
        )
    return source_ids


def main() -> None:
    print("=== 知识库校验 ===")
    profiles, by_domain = load_job_profiles()
    print(f"[Job] 岗位画像 {len(profiles)} 份，领域 {len(by_domain)} 个：")
    for p in profiles:
        extras = []
        if p.get("profile"):
            extras.append(f"人工画像 {len(p['profile'].get('core_tasks') or [])} 项任务")
        if p.get("ground_requirements"):
            extras.append("岗位要求以知识库为准")
        suffix = f"，{'；'.join(extras)}" if extras else ""
        print(f"  - {p['job_name']}（{p['domain']}，{len(p['skills'])} 项技能{suffix}）")

    all_sources = validate_job_evidence(profiles)
    print(f"[Sources] 全部岗位共 {len(all_sources)} 份可回查资料：{', '.join(sorted(all_sources))}")

    profile_names = {name for names in by_domain.values() for name in names}
    catalog = JobCatalog()
    if set(catalog.skill_names) != profile_names:
        raise SystemExit("岗位目录加载结果与校验脚本不一致")

    skills = SkillKnowledgeBase()
    validate_skills(skills, profile_names)

    projects = ProjectKnowledgeBase()
    validate_projects(projects, profile_names, set(by_domain))

    print("\n校验通过：技能、项目与岗位画像的交叉引用全部有效。")


if __name__ == "__main__":
    main()
