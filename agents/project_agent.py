"""Project Planning Agent：用项目来补齐能力缺口，而不是只推荐"学什么"。"""

import re

from pydantic import BaseModel, Field

from agents.job_catalog import JobCatalog
from agents.llm import structured_invoke
from agents.project_knowledge import DIFFICULTY_RANK, ProjectKnowledgeBase
from agents.skill_dict import SkillVocabulary
from agents.source_attribution import gap_source_hint
from agents.utils import (
    SKILL_LEVELS,
    build_skill_mapping,
    check_non_empty,
    dedupe_by,
    log,
    resolve_canonical,
)
from graph.state import CareerState, check_update

# Assessment Agent 给出的"已满足"状态：这类技能不是待补缺口
SATISFIED_STATUS = "已满足"

# 判断"这是不是技术岗"的信号。岗位、差距、学习路线、已有技能里只要出现一个，
# 就按技术岗处理，不干预模型给出的技术栈 —— 宁可少管，也不误删。
_TECH_CONTEXT_TERMS: tuple[str, ...] = (
    "python", "java", "javascript", "typescript", "c++", "c#", "golang", "go",
    "rust", "php", "ruby", "kotlin", "swift", "scala", "sql", "mysql",
    "postgresql", "sqlite", "mongodb", "redis", "fastapi", "flask", "django",
    "spring", "react", "vue", "angular", "node.js", "docker", "kubernetes",
    "git", "linux", "nginx", "langchain", "langgraph", "pytorch", "tensorflow",
    "rag", "agent", "llm api", "prompt engineering", "function calling",
    "embedding", "后端 api 开发", "后端开发", "api 开发", "数据结构与算法",
    "计算机网络", "操作系统", "测试与效果评测",
)

# 软件开发技术名。非技术岗的项目里出现这些、又没有任何岗位依据时，移除并留痕。
_SOFTWARE_TECH_TERMS: tuple[str, ...] = (
    "python", "java", "javascript", "typescript", "c++", "c#", "golang", "go",
    "rust", "php", "ruby", "kotlin", "swift", "scala", "sql", "mysql",
    "postgresql", "sqlite", "mongodb", "redis", "fastapi", "flask", "django",
    "spring boot", "spring", "react", "vue", "angular", "node.js", "express",
    "docker", "kubernetes", "git", "github", "linux", "nginx", "langchain",
    "langgraph", "pytorch", "tensorflow", "scikit-learn", "pandas", "numpy",
    "openai api", "llm api", "rag", "agent", "prompt engineering",
    "function calling", "embedding", "后端 api 开发", "后端开发", "api 开发",
    "数据库", "代码仓库", "接口开发", "web 应用", "数据结构与算法",
)


def _contains_term(haystack: str, needle: str) -> bool:
    """ASCII 词按词边界匹配，中文按子串匹配；避免 Go 命中 Google、RAG 命中 storage。"""
    if not needle:
        return False
    text = str(haystack or "").lower()
    term = str(needle or "").lower()
    if term.isascii():
        return re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", text) is not None
    return term in text


def _domain_text(target_job: str, gaps: list, learning_plan: list, user_skills: list) -> str:
    """把判断岗位领域要用的文本拼在一起：岗位名、缺口、学习路线、已有技能。"""
    parts = [str(target_job or "")]
    for gap in gaps or []:
        parts.append(str((gap or {}).get("skill") or ""))
    for stage in learning_plan or []:
        if not isinstance(stage, dict):
            continue
        parts.append(str(stage.get("topic") or ""))
        parts.extend(str(c) for c in stage.get("covers") or [])
    for item in user_skills or []:
        if isinstance(item, dict):
            parts.append(str(item.get("skill") or ""))
    return "\n".join(parts)


def _looks_technical(text: str) -> bool:
    return any(_contains_term(text, term) for term in _TECH_CONTEXT_TERMS)


def strip_ungrounded_software_tech(
    projects: list, target_job: str, gaps: list, learning_plan: list, user_skills: list
) -> list[str]:
    """非技术岗的项目里，移除没有任何岗位依据的软件开发技术。

    判定口径只有两条，尽量少管：

    1. 岗位/差距/学习路线/已有技能里出现任何技术岗信号 → 按技术岗处理，原样保留；
    2. 非技术岗里，``tech_stack`` 的某一项属于软件开发技术、且没有在同样的文本里
       出现过 → 移除。Excel、财务软件、统计软件等行业工具不在名单内，不受影响。

    移除不是猜测：技术名来自固定名单，保留条件来自本轮真实数据。返回说明，供节点打印。
    """
    text = _domain_text(target_job, gaps, learning_plan, user_skills)
    if _looks_technical(text):
        return []

    notes: list[str] = []
    for project in projects or []:
        kept: list[str] = []
        removed: list[str] = []
        for raw in project.get("tech_stack") or []:
            name = str(raw or "").strip()
            if not name:
                continue
            software = any(_contains_term(name, term) for term in _SOFTWARE_TECH_TERMS)
            if software and not _contains_term(text, name):
                removed.append(name)
            else:
                kept.append(name)
        if not removed:
            continue
        project["tech_stack"] = kept
        tail = "（已无技术栈，建议按行业工具补充）" if not kept else ""
        notes.append(
            f"项目「{project.get('name')}」的 tech_stack 含与目标岗位领域无关的"
            f"软件开发技术 {removed}，已移除{tail}"
        )
    return notes


def normalize_project_stages(projects: list, learning_plan: list) -> list[str]:
    """把 ``stage`` 统一成「阶段 N」，并尽量对应到真实学习阶段。

    优先级：原值里的数字合法 → 直接用；否则按 ``covers_gaps`` 与学习阶段
    ``covers`` 的重合度取最高；再否则按项目顺序落到对应阶段。没有学习路线时不改。
    """
    stages: list[tuple[int, set[str]]] = []
    for item in learning_plan or []:
        if not isinstance(item, dict):
            continue
        try:
            number = int(item.get("stage"))
        except (TypeError, ValueError):
            continue
        stages.append((number, {str(c).strip() for c in item.get("covers") or [] if str(c).strip()}))
    if not stages:
        return []
    valid = {number for number, _ in stages}

    notes: list[str] = []
    for index, project in enumerate(projects or []):
        raw = str(project.get("stage") or "").strip()
        match = re.search(r"\d+", raw)
        chosen = int(match.group()) if match and int(match.group()) in valid else None
        if chosen is None:
            covered = {str(s).strip() for s in project.get("covers_gaps") or []}
            scored = [(len(covered & covers), number) for number, covers in stages]
            best_count, best_stage = max(scored, key=lambda pair: (pair[0], -pair[1]))
            if best_count > 0:
                chosen = best_stage
        if chosen is None:
            chosen = stages[min(index, len(stages) - 1)][0]

        normalized = f"阶段 {chosen}"
        if normalized == raw:
            continue
        project["stage"] = normalized
        notes.append(f"项目「{project.get('name')}」的 stage 已由 {raw!r} 归一为 {normalized}")
    return notes

PROMPT = """你是一名项目实训导师，熟悉「{target_job}」所在行业的真实项目形态。
请为一名想成为该岗位的大学生设计实战项目。

【他的能力差距（已按优先级排序）】
{gaps}

【已经为他安排的学习路线】
{learning_plan}

【Project 知识库·候选项目模板（检索自 Project 知识库，优先从中挑选/参考）】
{project_knowledge}

【核心理念】
不要只是告诉他"去学什么"，而是**用项目把能力缺口补上**。
一个设计得好的项目，应该能同时覆盖多个能力缺口。
例如：缺 FastAPI + Docker + Agent 编排，就可以设计一个
"AI 智能求职助手"，要求用 FastAPI 提供接口、用 Agent 编排流程、用 Docker 部署，
一个项目同时补三个缺口。
（这是技术岗的示例；其他领域按该行业的真实项目形态类比，
如临床护理个案、财务模拟核算、教学设计作品集、市场调研报告等。）

【先判断岗位领域，再设计项目——这一条优先于下面的技术示例】
- **技术岗**（开发、算法、测试、运维、数据、安全等）：项目可以是软件、服务或系统；
  tech_stack 写实际使用的编程语言、框架与工具。
- **非技术岗**（会计、财务、审计、护理、法律、教育、传媒、市场、人力、行政等）：
  项目必须是该行业真实的工作任务与产出，例如财务报表与底稿、纳税申报模拟、
  护理个案、法律意见书、教学设计、市场调研报告；
  tech_stack 只写该行业实际使用的工具、软件与方法（Excel、财务软件、统计软件、
  行业规范等），**禁止**出现 Python、Java、FastAPI、Docker、Git、数据库、接口、
  代码仓库等软件开发技术，除非上面的能力差距里明确要求这项技术；
  交付物写行业交付物（报告、底稿、方案、作品集、操作记录），
  不要写"可交互系统""Web 应用""代码仓库"。不要因为上面的技术示例就照搬技术栈。
  非技术岗也不要设计以 AI、大模型、编程为核心的项目，除非能力差距里明确出现这些技术；
  项目主体必须是该行业的真实任务，行业工具只能作为辅助。

【要求】
1. 设计 2~3 个项目，难度递进，第 1 个最简单。
2. 每个项目必须同时覆盖**至少 2 项**能力差距，覆盖得越多越好；
   优先覆盖优先级"高"的缺口。
3. **优先从【Project 知识库·候选项目模板】中选择与用户技能差距匹配的模板**，
   在模板基础上结合本用户情况做具体化；若没有合适模板再自行设计。
   不要因为综合项目名字好听就无条件推荐——必须结合本用户的差距与学习计划判断。
   **候选清单里标了"暂不适合作当前项目"的，本轮一律不要推荐**，
   也不要换个名字推荐同一件事；它们是等高优先级缺口补齐之后才做的。
4. 项目顺序按难度递进，第 1 个最简单；候选清单已经按难度从易到难排好，
   照这个顺序来，**不要自己重排成"最能补缺口的排前面"**。
5. covers_gaps 必须使用上面给出的能力差距里的技能原名，一字不差，
   一项技能写完整（写"Agent 与工作流编排"，不要拆成"Agent"、"工作流编排"）。
   **只写状态为"明显不足"、"部分满足"、"疑似具备"或"未验证"的缺口**：
   只有状态"已满足"的技能他已经达标、且有直接证据，不是缺口，不要写进 covers_gaps。
   项目当然可以继续训练他已经会的技术，
   那写在 tech_stack 里就行，不要用它来充"补了多少缺口"。
6. name 只写项目名称本身，短一点、像项目名
   （如"AI 智能求职助手"），不要把阶段号、说明写进名字里。
7. goal 写这个项目要达成什么、补哪些能力，**不要出现"阶段"两个字**；
   阶段信息只写在 stage 字段里。stage **只能**写成"阶段 N"（如"阶段 1"），
   N 必须是上面学习路线里真实存在的阶段号，不要写英文、解释或别的格式。
8. tech_stack 写这个项目实际会用到的工具/技术，并包含 covers_gaps 里的相关技能。
   技术岗写编程语言与框架；非技术岗写行业工具、软件与方法。
   只写名称，不要把能力描述当工具写进来
   （例如"类""类型注解""调试定位问题"都不是技术栈）；
   不要写与目标岗位领域无关的编程技术。
9. deliverable 写最终交付物，要具体可验收。
   技术岗例如"一个能跑的 HTTP 服务 + 仓库 + README 部署说明 + Dockerfile"；
   非技术岗按行业真实的交付形态写，例如"一套完整核算底稿 + 财务报表 + 分析说明"、
   "一份护理个案报告"、"一份市场调研报告"，不要写代码仓库或可交互系统。
10. 项目要做学生真正能做完的规模，不要设计成企业级系统。
11. 全部用中文，专业术语保留原文（英文术语不翻译）。"""


class ProjectItem(BaseModel):
    """一个实战项目。"""

    name: str = Field(description="项目名称，短，不含阶段号")
    goal: str = Field(description="项目目标：这个项目做出来是为了补哪些能力，不要写阶段号")
    covers_gaps: list[str] = Field(description="对应的能力缺口，用技能原名")
    tech_stack: list[str] = Field(description="使用的技术栈")
    stage: str = Field(description="项目阶段，对应学习路线的哪个阶段")
    deliverable: str = Field(description="最终成果，要具体可验收")


class ProjectPlan(BaseModel):
    """Project Agent 的结构化输出。"""

    projects: list[ProjectItem] = Field(description="项目列表，难度递进")


def _format_gaps(gaps: list) -> str:
    """列出能力差距，并标出**状态** —— 模型要靠它区分"还没满足的缺口"与"已经会的技能"。"""
    if not gaps:
        return "（无）"
    return "\n".join(
        f"- {g.get('skill')}｜状态 {g.get('status')}｜优先级 {g.get('priority')}｜"
        f"{g.get('gap')}{gap_source_hint(g)}"
        for g in gaps
    )


def _format_learning_plan(stages: list) -> str:
    if not stages:
        return "（无）"
    return "\n".join(
        f"- 阶段 {s.get('stage')}：{s.get('topic')}（{s.get('goal')}）" for s in stages
    )


def _student_levels(user_skills: list, vocabulary) -> dict:
    """``{规范技能名: 学生等级}`` —— 只采信 Profile Agent 的原数据，不问模型重评。"""
    levels: dict[str, str] = {}
    for item in user_skills or []:
        if not isinstance(item, dict):
            continue
        canonical = resolve_canonical(item.get("skill"), vocabulary)
        if canonical is None:
            continue
        level = str(item.get("level") or "").strip()
        if not level or level == "无":
            continue
        current = levels.get(canonical)
        if current is None or _level_rank(level) > _level_rank(current):
            levels[canonical] = level
    return levels


def _level_rank(level: str) -> int:
    return SKILL_LEVELS.index(level) if level in SKILL_LEVELS else -1


def normalize_gap_skills(gap_skills: list, vocabulary) -> tuple[list[str], list[str]]:
    """把原始技能缺口名归一为**项目知识库使用的规范技能名**（B4 修复）。

    为什么必须归一：项目模板的 ``covers_skills`` 用的是知识库规范名
    （``Agent``、``后端 API 开发``），而 ``skill_gaps`` 里可能是模型写出来的
    非规范写法（``Agent与工作流编排``、``后端服务``）—— 不归一的话覆盖度永远
    算成 0，``score`` 与 ``matched_skills`` 全是错的。

    返回 ``(canonical, unmapped)``：

    - ``canonical``：能唯一归一的规范技能名，去重保序（LangChain 与 LangGraph
      归一后是同一个技能，只保留一个）；
    - ``unmapped``：没命中或有歧义的原始名。**不猜**，并且把它们排除在匹配之外，
      免得"工程化与部署"这类岗位能力分类标签被误算成知识库里的某个具体技能。

    只产出新列表，**不改动**调用方传进来的 ``skill_gaps`` / ``learning_plan``。
    归一复用的是 ``agents.utils`` 里与其它 Agent 共用的同一套工具，避免各写一份
    口径不一致的映射。
    """
    mapping, unmapped = build_skill_mapping(gap_skills or [], vocabulary)
    return list(dict.fromkeys(mapping.values())), unmapped


def resolve_job_domain(target_job: str, gap_skills: list[str] | None = None) -> str | None:
    """目标岗位 → 知识库领域。岗位名认不出时，用规范技能名投票（仅在唯一领域时采用）。"""
    try:
        catalog = JobCatalog()
    except Exception as exc:  # noqa: BLE001 - 领域解析失败只影响模板筛选，不中断流程
        log("Project Agent", f"岗位目录不可用，本轮不按领域筛选项目模板：{exc}")
        return None
    return catalog.resolve_domain(target_job) or catalog.domain_for_skills(gap_skills or [])


def _load_project_templates(gap_skills: list[str], learning_plan: list,
                            student_levels: dict | None = None,
                            domain: str | None = None) -> str:
    """从 Project 知识库检索匹配的项目模板，供 prompt 使用。

    与旧版的三点差别（对应验收报告 S3）：

    1. 候选只取"当前就能上手"的难度（beginner / intermediate），高级综合项目
       进 blocked 列表并注明原因，明确告诉模型本轮不要推荐；
    2. 排序不再只是知识库的覆盖数排序，而是**难度从易到难**，模型照抄也不会
       把最难的排在最前；
    3. 前置技能一个都不具备的项目同样不进入候选。

    ``gap_skills`` 由调用方先经 :func:`normalize_gap_skills` 归一为**规范技能名**
    （B4）—— 模板的 ``covers_skills`` 是规范名，传原始缺口名会让覆盖度恒为 0。

    知识库加载或查询失败时打印警告并回退到纯模型生成，不中断工作流。
    """
    names = [str(s).strip() for s in gap_skills if str(s).strip()]
    plan_skills: list[str] = []
    for stage in learning_plan or []:
        if isinstance(stage, dict):
            plan_skills.extend(c for c in stage.get("covers") or [] if str(c).strip())
    # 差距技能优先，学习计划技能作补充（去重、保序）
    combined: list[str] = []
    for n in names + plan_skills:
        if n not in combined:
            combined.append(n)
    if not combined:
        return "（无差距技能，无法筛选项目模板）"

    try:
        base = ProjectKnowledgeBase()
        executable, blocked = base.recommend_for_user(
            combined, student_levels, domain=domain
        )
        if not executable and not blocked:
            return "（知识库中无可直接匹配的项目模板，请自行结合差距设计）"

        lines = [
            "候选已按**难度从易到难**排列；你输出的项目顺序也必须难度递进，"
            "第 1 个最简单。"
        ]
        for p in executable:
            lines.append(
                f"- {p['name']}（难度 {p['difficulty']}）：覆盖 "
                f"{'、'.join(p.get('covers_skills', [])) or '—'}；前置 "
                f"{'、'.join(p.get('prerequisites', [])) or '—'}；技术栈 "
                f"{'、'.join(p.get('tech_stack', [])) or '—'}；阶段提示 "
                f"{p.get('stage_hint', '—')}；交付物 {p.get('deliverable', '—')}；验收标准 "
                f"{'；'.join(str(c) for c in p.get('acceptance_criteria') or [] if str(c).strip()) or '—'}；"
                f"预计 {p.get('estimated_weeks', '—')} 周"
            )
        if blocked:
            lines.append("")
            lines.append("【以下项目暂不适合作当前执行的项目，本轮不要推荐，也不要换个名字推荐同一件事】")
            for p in blocked:
                lines.append(
                    f"- {p['name']}（难度 {p['difficulty']}）：{p['blocked_reason']}；"
                    f"尚缺前置 {'、'.join(p.get('missing_prerequisites') or []) or '—'}"
                )
        return "\n".join(lines)
    except Exception as exc:  # noqa: BLE001 - 回退路径，捕获一切异常以保证不中断
        log("Project Agent", f"Project 知识库检索失败，回退纯模型生成：{exc}")
        return "（知识库暂时不可用，请按方法自行设计）"


def _blocked_templates(gap_skills: list[str], learning_plan: list,
                       student_levels: dict | None,
                       domain: str | None = None) -> list[dict]:
    """取"暂不推荐"的项目模板（失败返回空列表），供生成后兜底校验。

    ``gap_skills`` 同样要求是归一后的规范技能名，与 :func:`_load_project_templates` 一致。
    """
    names = [str(s).strip() for s in gap_skills if str(s).strip()]
    plan_skills: list[str] = []
    for stage in learning_plan or []:
        if isinstance(stage, dict):
            plan_skills.extend(c for c in stage.get("covers") or [] if str(c).strip())
    combined = list(dict.fromkeys(names + plan_skills))
    if not combined:
        return []
    try:
        _, blocked = ProjectKnowledgeBase().recommend_for_user(
            combined, student_levels, domain=domain
        )
        return blocked
    except Exception:  # noqa: BLE001 - 校验辅助，失败不影响主流程
        return []


def _gap_name_index(gaps: list, vocabulary) -> tuple[dict, set]:
    """``({规范技能名: 原始缺口名}, {原始缺口名})`` —— align / drop 共用同一口径。"""
    canonical_to_gap: dict[str, str] = {}
    for gap in gaps or []:
        name = str(gap.get("skill") or "").strip()
        canonical = resolve_canonical(name, vocabulary)
        if canonical and canonical not in canonical_to_gap:
            canonical_to_gap[canonical] = name
    gap_names = {str(g.get("skill") or "").strip() for g in gaps or []}
    return canonical_to_gap, gap_names


def drop_unlinked_covers_gaps(projects: list, gaps: list, vocabulary) -> list[str]:
    """从 ``covers_gaps`` 里剔除**不属于本用户技能缺口**的技能名（问题 2）。

    ``covers_gaps`` 的语义是"这个项目能补上我的哪几个缺口"，本该只出现
    ``skill_gaps`` 里的名字。但模型常照抄知识库模板的 ``covers_skills``（如
    ``Embedding``），而本用户并不缺这项 —— 留着它下游既对不上号，输出也在骗人。

    这里做的是**剔除**，不是放宽判定：判定标准一个字没改，只是不再把对不上的
    名字塞进结果。被剔除的项会以说明形式返回，不静默吞掉。

    必须在 :func:`align_covers_gaps` **之前**调用：先清掉对不上的，映射阶段
    就不会再碰到"无法关联"的项。能映射的项在这里原样保留，交给映射阶段处理。
    """
    canonical_to_gap, gap_names = _gap_name_index(gaps, vocabulary)
    notes: list[str] = []

    for project in projects or []:
        kept: list[str] = []
        removed: list[str] = []
        for raw in project.get("covers_gaps") or []:
            name = str(raw or "").strip()
            if not name:
                continue
            if name in gap_names:
                kept.append(name)
                continue
            canonical = resolve_canonical(name, vocabulary)
            if canonical and canonical_to_gap.get(canonical):
                kept.append(name)  # 能回映射到缺口，留给 align_covers_gaps 处理
            else:
                removed.append(name)

        project["covers_gaps"] = list(dict.fromkeys(kept))
        if removed:
            tail = "（该项目已无任何关联本用户缺口的技能）" if not project["covers_gaps"] else ""
            notes.append(
                f"项目「{project.get('name')}」的 covers_gaps 中 {removed} 不属于本用户的"
                f"技能缺口，已从该项剔除{tail}"
            )
    return notes


def align_covers_gaps(projects: list, gaps: list, vocabulary) -> list[str]:
    """把 ``covers_gaps`` 映射回**原始 skill_gaps 名称**，返回问题清单。

    模型经常输出知识库里的规范技能名（RAG、Agent、后端 API 开发），而本用户的
    skill_gaps 里叫的是别的写法（Agent与工作流编排、后端服务）。不映射的话下游
    根本对不上号，所以这里按规范名反查回原名；确实对不上的保留原值并报告。
    """
    canonical_to_gap: dict[str, str] = {}
    for gap in gaps or []:
        name = str(gap.get("skill") or "").strip()
        canonical = resolve_canonical(name, vocabulary)
        if canonical and canonical not in canonical_to_gap:
            canonical_to_gap[canonical] = name
    gap_names = {str(g.get("skill") or "").strip() for g in gaps or []}

    issues: list[str] = []
    for project in projects:
        aligned: list[str] = []
        for raw in project.get("covers_gaps") or []:
            name = str(raw or "").strip()
            if not name:
                continue
            if name in gap_names:
                aligned.append(name)
                continue
            canonical = resolve_canonical(name, vocabulary)
            mapped = canonical_to_gap.get(canonical) if canonical else None
            if mapped:
                aligned.append(mapped)
            else:
                aligned.append(name)
                issues.append(
                    f"项目「{project.get('name')}」的 covers_gaps 含无法关联到 skill_gaps 的技能：{name}"
                )
        # 去重保序
        project["covers_gaps"] = list(dict.fromkeys(aligned))
    return issues


def check_blocked_projects(projects: list, blocked: list, threshold: float = 0.6) -> list[str]:
    """兜底校验：模型可能无视"暂不推荐"提示，仍把超难度综合项目排进计划。

    ProjectItem 结构固定、项目名还可能被改写，所以只能按 ``covers_gaps`` 与
    被拦模板的 ``covers_skills`` 的重合度来判断：重合度高就说明它实质上仍是那个
    被拦下来的项目，必须告警。
    """
    issues: list[str] = []
    for project in projects or []:
        covered = {str(s).strip() for s in project.get("covers_gaps") or []}
        if not covered:
            continue
        for template in blocked or []:
            targets = {str(s).strip() for s in template.get("covers_skills") or []}
            if not targets:
                continue
            overlap = len(covered & targets) / len(targets)
            if overlap >= threshold:
                issues.append(
                    f"项目「{project.get('name')}」的 covers_gaps 与暂不推荐的「{template['name']}」"
                    f"（{template.get('blocked_reason', '前置/难度不足')}）重合 {overlap:.0%}，"
                    f"可能仍被当成当前可执行项目"
                )
                break
    return issues


def _template_difficulty_rank(name, base: ProjectKnowledgeBase | None) -> int | None:
    """项目名 → 知识库模板的难度序号；**匹配不到就返回 None，绝不猜一个难度**。

    项目名可能被模型改写、也可能完全自创，所以只有能可靠找回模板的才给难度。
    """
    if base is None:
        return None
    text = str(name or "").strip()
    if not text:
        return None
    for template in base.projects:
        tname = template["name"]
        if text == tname or text in tname or tname in text:
            return DIFFICULTY_RANK[template["difficulty"]]
    return None


def _name_key(name: str) -> str:
    """项目名的比较键：忽略大小写、空白与常见标点，只保留实质字符。"""
    return re.sub(r"[\s，。、：；·\-—_/\\()（）\[\]【】\"'“”‘’!！?？]+", "", str(name or "").lower())


def align_project_names(projects: list, base: ProjectKnowledgeBase | None) -> list[str]:
    """把模型改写过的项目名对齐到知识库模板原名，返回改名说明。

    只做三种确定性匹配：原名完全一致、去掉空白/标点后一致、唯一包含关系。
    语义相近但字面不同的不改 —— 猜错名称会让下游的难度、验收标准与历史记录
    全部对不上号，这比保留模型原名更糟。认不准就原样保留。
    """
    if base is None or not projects:
        return []
    templates = base.projects
    exact = {t["name"] for t in templates}
    by_key: dict[str, list[str]] = {}
    for t in templates:
        by_key.setdefault(_name_key(t["name"]), []).append(t["name"])

    notes: list[str] = []
    for project in projects:
        raw = str(project.get("name") or "").strip()
        if not raw or raw in exact:
            continue
        key = _name_key(raw)
        if not key:
            continue
        target = None
        same_key = by_key.get(key) or []
        if len(same_key) == 1:
            target = same_key[0]
        else:
            hits = [
                name for name in exact
                if key in _name_key(name) or _name_key(name) in key
            ]
            if len(hits) == 1:
                target = hits[0]
        if target and target != raw:
            project["name"] = target
            notes.append(f"项目「{raw}」与知识库模板只是写法不同，已对齐为「{target}」")
    return notes


def check_difficulty_order(projects: list, base: ProjectKnowledgeBase | None) -> list[str]:
    """难度递进的尽力校验：能按项目名找回知识库模板的，检查难度是否递增。

    项目名可能被模型改写、也可能完全自创，因此对不上的直接跳过 —— 这里只做
    能可靠判定的那部分，不猜。
    """
    if base is None:
        return []
    ranks: list[tuple[str, int]] = []
    for project in projects or []:
        name = str(project.get("name") or "").strip()
        rank = _template_difficulty_rank(name, base)
        if rank is not None:
            ranks.append((name, rank))
    issues: list[str] = []
    for (prev_name, prev_rank), (name, rank) in zip(ranks, ranks[1:]):
        if rank < prev_rank:
            issues.append(
                f"项目顺序不是难度递进：「{prev_name}」比后面的「{name}」更难，"
                f"第 1 个应当最简单"
            )
    return issues


def unmet_gaps(gaps: list) -> list:
    """还没满足、需要项目来补的缺口。

    ``covers_gaps`` 的语义是"这个项目能帮我补上哪几个**尚未满足**的缺口"。
    已满足（且有直接证据）的技能不属于缺口 —— 项目当然可以继续训练它（那是
    ``covers_skills``，训练技能），但不能拿它去充缺口覆盖度，否则"教你已经会的东西"
    的项目会排到前面。等级达标但只有自述/跨技能推断的（``display_status`` 为
    「未验证」「疑似具备」）仍算待补缺口 —— 它们需要可验证的产出。
    """
    return [
        g for g in (gaps or [])
        if str(g.get("display_status") or g.get("status") or "") != SATISFIED_STATUS
    ]


def order_projects_by_difficulty(projects: list, gaps: list,
                                 base: ProjectKnowledgeBase | None) -> tuple[list[dict], list[str]]:
    """稳定重排项目，返回 ``(新列表, 说明)``。

    排序规则（依次比较）：

    1. 难度由低到高（难度取自知识库模板，不自行猜测）；
    2. 难度相同时，覆盖**高优先级**缺口更多的排前面；
    3. 仍然相同时保持**原有相对顺序**（排序键末位用原下标，属稳定排序）。

    匹配不到知识库模板的项目**不赋予难度**，统一排在已知难度之后，
    内部保持原有顺序 —— 不会因为"猜一个难度"而被错误地插到中间。

    只重排列表顺序，**不动任何项目字段**（covers_gaps / tech_stack / goal 等原样保留）。
    """
    if not projects:
        return list(projects), []

    high = {
        str(g.get("skill") or "").strip()
        for g in (gaps or [])
        if g.get("priority") == "高"
    }

    known: list[tuple[int, int, int, dict]] = []
    unknown: list[dict] = []
    for index, project in enumerate(projects):
        rank = _template_difficulty_rank(project.get("name"), base)
        if rank is None:
            unknown.append(project)
            continue
        covered = {str(s).strip() for s in project.get("covers_gaps") or []}
        known.append((rank, -len(covered & high), index, project))

    known.sort(key=lambda item: (item[0], item[1], item[2]))
    ordered = [item[3] for item in known] + unknown

    if not any(before is not after for before, after in zip(projects, ordered)):
        return ordered, []

    notes = ["已按「难度升序 → 高优先级缺口覆盖数降序 → 原顺序」重排项目：模型给出的原始顺序不是难度递进"]
    if unknown:
        names = [str(p.get("name") or "") for p in unknown]
        notes.append(f"以下项目匹配不到知识库模板，未赋予难度、统一排在已知难度之后：{names}")
    return ordered, notes


def _load_vocabulary() -> SkillVocabulary | None:
    """取技能词表；不可用返回 None，节点降级为"不做技能名归一"。"""
    try:
        return SkillVocabulary()
    except Exception as exc:  # noqa: BLE001 - 降级路径
        log("Project Agent", f"技能词表不可用，跳过技能名归一：{exc}")
        return None


def project_node(state: CareerState) -> dict:
    """LangGraph 节点：读取 skill_gaps / learning_plan，写入 project_plan。"""
    gaps = state.get("skill_gaps") or []
    if not gaps:
        raise ValueError("state 中缺少 skill_gaps，无法设计实战项目")

    learning_plan = state.get("learning_plan") or []
    vocabulary = _load_vocabulary()
    # 学生真实水平只从 Profile Agent 的原数据来，不问模型
    student_levels = _student_levels(state.get("user_skills") or [], vocabulary)

    # B4：项目模板的 covers_skills 是规范技能名，缺口名必须**先归一**再参与
    # 覆盖度、匹配、评分与排序，否则覆盖率恒为 0。归一只产出新列表，原始
    # skill_gaps / learning_plan 不动；这里刻意**不**碰 covers_gaps 的输出映射，
    # 那是 align_covers_gaps 的职责，两件事不能混。
    # 修复 3：只有**尚未满足**的缺口参与项目匹配、covers_gaps 与覆盖度计算。
    # 已满足的技能属于"项目还能训练什么"（covers_skills），不是"我还缺什么"。
    project_gaps = unmet_gaps(gaps)
    raw_gap_skills = [g.get("skill") for g in project_gaps]
    match_skills, unmapped_skills = normalize_gap_skills(raw_gap_skills, vocabulary)
    if unmapped_skills:
        print(
            f"[Project Agent] 警告：以下缺口无法唯一归一为项目知识库的规范技能名，"
            f"已排除在项目匹配之外（不猜测、不计入覆盖度）：{unmapped_skills}",
            flush=True,
        )

    # 领域过滤：会计岗不该看到 RAG 模板，AI 岗也不该看到会计实训。
    # 岗位名认不出时用规范化后的缺口技能投票；仍认不出就不筛（宁可多给，不误挡）。
    domain = resolve_job_domain(state.get("target_job", ""), match_skills)
    if domain:
        print(f"[Project Agent] 项目模板按领域筛选：{domain}", flush=True)

    prompt = PROMPT.format(
        target_job=state.get("target_job", "目标岗位"),
        gaps=_format_gaps(gaps),
        learning_plan=_format_learning_plan(learning_plan),
        project_knowledge=_load_project_templates(
            match_skills, learning_plan, student_levels, domain=domain
        ),
    )
    plan = structured_invoke(prompt, ProjectPlan)

    projects = [p.model_dump() for p in plan.projects]
    try:
        base = ProjectKnowledgeBase()
    except Exception:  # noqa: BLE001 - 校验辅助，失败不影响主流程
        base = None

    # 模型常把模板名改写成近义说法；先对齐成知识库原名，下游的难度排序、
    # 验收标准与成长记录才能对得上。对齐后可能出现重名，所以再去重。
    for note in align_project_names(projects, base):
        print(f"[Project Agent] {note}", flush=True)
    projects = dedupe_by(projects, key="name")
    check_non_empty(projects, "Project Agent 的项目列表", minimum=1)

    # 兜底校验（先做重合度检查：它看的是模型原始写法的 covers_gaps，
    # 一旦被 align 改写成缺口原名就不好比对了）
    blocked = _blocked_templates(match_skills, learning_plan, student_levels, domain=domain)
    issues = check_blocked_projects(projects, blocked)
    if vocabulary is not None:
        # 先剔除"不属于本用户缺口"的技能名，再做回映射 —— 顺序反了映射阶段
        # 就会对着这些名字报"无法关联"。剔除是说明性提示，不是失败告警。
        for note in drop_unlinked_covers_gaps(projects, project_gaps, vocabulary):
            print(f"[Project Agent] 提示：{note}", flush=True)
        issues.extend(align_covers_gaps(projects, project_gaps, vocabulary))

    # 领域适配：非技术岗不保留凭空出现的软件开发技术；stage 统一成「阶段 N」。
    # 这两步只改可判定的字段，不重写项目名称、目标与交付物。
    for note in strip_ungrounded_software_tech(
        projects,
        state.get("target_job", ""),
        gaps,
        learning_plan,
        state.get("user_skills") or [],
    ):
        print(f"[Project Agent] {note}", flush=True)
    for note in normalize_project_stages(projects, learning_plan):
        print(f"[Project Agent] {note}", flush=True)

    # 修复 2：把模型给出的项目顺序重排成"难度递进 → 高优先级缺口覆盖多的靠前"。
    # 放在 covers_gaps 定稿之后，才能按高优先级缺口覆盖数做次级排序。
    ordered, order_notes = order_projects_by_difficulty(projects, project_gaps, base)
    projects = ordered
    for note in order_notes:
        print(f"[Project Agent] {note}", flush=True)

    issues.extend(check_difficulty_order(projects, base))
    for issue in issues:
        print(f"[Project Agent] 警告：{issue}", flush=True)

    log("Project Agent")
    return check_update({"project_plan": projects}, "Project Agent")