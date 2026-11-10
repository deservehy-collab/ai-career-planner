"""技能词表：规范技能名 + 别名/上位词，供各 Agent 做技能归一与前置门禁。

这里只有**查表**，没有任何检索：
- `SkillVocabulary` 从 `knowledge/jobs/*.yaml` 读全部岗位的规范技能名（并集），
  再叠上 `SKILL_ALIASES`；单文件路径仍可通过 `profile_path` 注入，便于测试；
- 命中口径是字面/别名的确定性匹配，不调模型、不连向量库、不读索引。

原来它和 Chroma 向量检索器同住 `agents/retriever.py`。向量检索链路整体下线后，
其余六个模块（growth_agent / growth_loop / job_agent / learning_agent /
project_agent / graph.workflow）仍依赖这份词表，于是把它单独搬出来 ——
词表是**事实来源**，不是可选增强，删掉它会让成长升级与技能归一全部失去依据。
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from agents.job_catalog import JobCatalog

REPO_ROOT = Path(__file__).resolve().parent.parent

#: 单岗位词表的兼容入口；默认已改为读取 `knowledge/jobs/` 下全部岗位。
DEFAULT_PROFILE_PATH = REPO_ROOT / "knowledge" / "jobs" / "ai-application-engineer.yaml"

__all__ = ["DEFAULT_PROFILE_PATH", "SKILL_ALIASES", "SkillVocabulary"]

# ── 技能别名 / 上位词 ───────────────────────────────────────────────────
# 键必须是岗位画像 YAML 中真实存在的技能名（`SkillVocabulary` 会再校验一次，
# 目标技能不在词表内时该别名直接失效，避免把查询引向不存在的技能）。
# 别名只解决"问法与原文用词不同"的问题，不做同义词联想以外的泛化：
# 例如「框架」单独出现**不**触发任何技能，只有「开发框架」「智能体框架」这类
# 明确指向 Agent/RAG 开发框架的说法才映射到 LangChain / LangGraph。
SKILL_ALIASES: dict[str, tuple[str, ...]] = {
    # 注意：这里**只收**明确指向 Agent/RAG 开发框架的说法。
    # 裸词「开发框架」会把"后端开发框架"（=Flask/SpringBoot，属后端 API 开发）错误地
    # 引到这里，因此不收；后端侧的说法收在「后端 API 开发」下。
    "LangChain / LangGraph": (
        "agent 开发框架", "agent开发框架", "agent 框架", "agent框架",
        "智能体框架", "智能体开发框架", "编排框架", "大模型开发框架",
        "大模型开发工具", "lang chain", "lang graph",
    ),
    "Agent Skill / MCP 工具封装": (
        "mcp", "model context protocol", "mcp server", "mcp 工具", "mcp工具",
        "mcp 工具封装", "mcp工具封装", "agent skill",
    ),
    "Agent": ("智能体", "ai agent", "agent 架构"),
    "RAG": ("检索增强生成", "检索增强", "知识库检索"),
    "LLM API": ("大模型 api", "大模型api", "大模型的调用", "模型调用接口"),
    "Prompt Engineering": ("提示词工程", "提示词", "prompt 工程", "prompt工程", "prompt 设计"),
    "Function Calling": ("函数调用", "工具调用"),
    "Embedding": ("向量化", "向量数据库", "向量检索", "向量存储", "文本向量",
                  "embedding 模型", "embedding模型"),
    # 后端侧的上位词：`后端开发` 让"后端开发框架"落在正确地技能上，而不是 LangChain。
    "后端 API 开发": ("后端服务", "后端开发", "服务端开发", "接口开发", "api 开发", "后端框架"),
    "测试与效果评测": ("效果评测", "测试体系", "评测体系", "应用评测"),
    "数据结构与算法": ("算法基础",),
    "Git": ("版本控制",),
    "Linux": ("linux 环境", "linux环境"),
    "SQL": ("数据库操作",),
    "Docker": ("容器化", "容器化部署", "容器技术", "容器环境", "容器"),
    # —— 会计 ——
    "会计准则": ("企业会计准则", "会计准则与制度"),
    "财务报表分析": ("报表分析", "财务分析", "财务报表"),
    "纳税申报": ("税务申报", "报税", "纳税实务", "税务法规", "税法", "税务处理"),
    "财务软件": ("用友", "金蝶", "财务系统", "会计软件", "会计软件使用"),
    "审计基础": ("审计实务", "审计"),
    "成本核算": ("成本会计", "成本分析", "成本控制", "成本管理"),
    "预算管理": ("预算编制", "全面预算"),
    "内部控制": ("内控", "内部控制制度"),
    "出纳实务": ("出纳",),
    # —— 新媒体运营 ——
    "内容策划": ("选题策划", "内容规划"),
    "文案写作": ("文案撰写", "文案"),
    "短视频剪辑": ("视频剪辑", "剪辑"),
    "运营数据分析": ("运营数据", "数据复盘"),
    "用户增长": ("拉新", "增长运营"),
    "社群运营": ("社群",),
    "投放优化": ("信息流投放", "广告投放", "投放"),
    "竞品分析": ("竞品调研",),
    "平台规则与合规": ("平台规则", "内容合规"),
    "图片设计": ("海报设计", "平面设计"),
    # —— 中学教师 ——
    "学科专业知识": ("学科知识",),
    "教学设计": ("教案设计",),
    "课堂管理": ("课堂组织", "班级管理"),
    "命题与作业设计": ("命题", "作业设计"),
    "学情分析": ("学情诊断",),
    "教育心理学": ("教育心理",),
    "课件制作": ("PPT课件", "课件"),
    "家校沟通": ("家长沟通",),
    "教育政策与师德规范": ("师德", "教育政策"),
    "教学反思与研究": ("教学反思", "教研"),
}


def _contains_term(haystack: str, needle: str) -> bool:
    """ASCII 词按词边界匹配（避免 Git 命中 digit、RAG 命中 storage），中文按子串匹配。"""
    if not needle:
        return False
    if needle.isascii():
        return re.search(rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])", haystack) is not None
    return needle in haystack


class SkillVocabulary:
    """技能词表：从岗位画像 YAML 读取技能名，用于"技能精确检索"。

    也可以直接用 `skill_names=` 注入内存词表，便于测试。
    除字面名称外，还带别名/上位词（`SKILL_ALIASES`），使"开发框架""MCP"这类
    与原文用词不同的问法也能走确定性路径。
    """

    def __init__(
        self,
        skill_names: list[str] | None = None,
        profile_path: str | Path | None = None,
    ) -> None:
        if skill_names is not None:
            self._names = list(skill_names)
        elif profile_path is not None:
            path = Path(profile_path)
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            self._names = [s["skill"] for s in data.get("skills", []) if s.get("skill")]
        else:
            # 多岗位并集：会计/运营/教师的技能名也要能被归一与记账。
            self._names = JobCatalog().skill_names
        # 只为"词表里确实存在的技能"启用别名：目标技能不存在时别名直接失效，
        # 避免把查询引向知识库里根本没有的技能。
        present = set(self._names)
        self._aliases = {
            canonical: tuple(variants)
            for canonical, variants in SKILL_ALIASES.items()
            if canonical in present
        }

    @property
    def names(self) -> list[str]:
        return list(self._names)

    @property
    def aliases(self) -> dict[str, tuple[str, ...]]:
        return dict(self._aliases)

    @staticmethod
    def _variants(name: str) -> set[str]:
        """技能名的字面匹配变体。含 "/" 的名称额外拆成各部分（如 LangChain / LangGraph）。"""
        variants = {name}
        if "/" in name:
            variants.update(p.strip() for p in name.split("/") if p.strip())
        return {v for v in variants if v}

    @staticmethod
    def _contains(haystack: str, needle: str) -> bool:
        return _contains_term(haystack, needle)

    def match_detailed(self, query: str) -> list[tuple[str, str]]:
        """返回 [(技能名, "literal"|"alias")]。字面命中优先，长名称优先。"""
        if not query:
            return []
        lowered = query.lower()
        found: list[tuple[str, str]] = []
        for name in sorted(self._names, key=len, reverse=True):
            if any(self._contains(lowered, v.lower()) for v in self._variants(name)):
                found.append((name, "literal"))

        literal = {name for name, _ in found}
        alias_only: list[tuple[str, str]] = []
        for canonical, variants in sorted(
            self._aliases.items(), key=lambda kv: -max(len(v) for v in kv[1])
        ):
            if canonical in literal:
                continue
            if any(self._contains(lowered, v.lower()) for v in variants):
                alias_only.append((canonical, "alias"))
        return found + alias_only

    def match(self, query: str) -> list[str]:
        """返回 query 中命中的技能名（含别名命中的技能）。"""
        return [name for name, _ in self.match_detailed(query)]
