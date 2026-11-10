"""Project 知识库查询模块（结构化 YAML）。

设计要点
--------
1. **只读**：本模块只读取 `knowledge/projects/*.yaml`，不写、不建索引、不依赖 Ollama。
2. **可测试**：目录通过构造注入，单元测试可指向临时目录，不触碰真实文件。
3. **不无条件返回同一项目**：推荐会结合技能覆盖匹配度、已具备的前置技能与难度排序；
   高级综合项目（如"AI 求职规划助手"）默认不作为**当前立即执行**的候选，
   而是进 blocked 列表并说明原因，避免推荐给前置不足的学生。
4. **运行时可降级**：查询异常由调用方捕获并回退到依赖大模型的生成逻辑；
   本模块自身不静默吞错。

`covers_skills` / `prerequisites` 中的技能必须来自岗位知识库的规范技能列表，
由 `scripts/validate_knowledge.py` 与 Skill 知识库交叉校验。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

# 项目难度梯度：数值越大越难
DIFFICULTY_RANK = {"beginner": 1, "intermediate": 2, "advanced": 3}
VALID_DIFFICULTY = tuple(DIFFICULTY_RANK)

DEFAULT_PROJECT_DIR = Path(__file__).resolve().parent.parent / "knowledge" / "projects"

# 难度认知上限：超过该难度的项目不推荐给学生当前阶段做（可被覆盖）
MAX_DIFFICULTY = "advanced"

# 学生"当前就能上手做"的难度上限。高级综合项目要先把前置补齐，
# 因此默认不作为**当前立即执行**的候选，只会进 blocked 列表。
MAX_EXECUTABLE_DIFFICULTY = "intermediate"

# 视为"完全没有这项能力"的等级写法
_ABSENT_LEVELS = {"", "无", "none", "n/a", "-", "无经验"}

# 打分权重（都是真实参与排序的项，不再是恒零的装饰）
_COVERAGE_WEIGHT = 100  # 覆盖一项技能差距
_READY_PREREQ_WEIGHT = 10  # 已具备一项前置技能
_DIFFICULTY_PENALTY = 5  # 难度越高扣得越多，同覆盖下优先更简单的


class ProjectKnowledgeError(RuntimeError):
    """项目知识库源文件不合法或加载失败时抛出，便于上层区分"数据错"与"没查到"。"""


@dataclass
class ProjectKnowledgeBase:
    """加载并查询 Project 知识库。"""

    directory: Path = field(default_factory=lambda: DEFAULT_PROJECT_DIR)
    _projects: list[dict] = field(default_factory=list, repr=False)

    def __post_init__(self) -> None:
        self.load()

    # ── 加载 ────────────────────────────────────────────────────────────────
    def load(self) -> None:
        if not self.directory.is_dir():
            raise ProjectKnowledgeError(f"Project 知识库目录不存在：{self.directory}")

        projects: list[dict] = []
        seen_names: set[str] = set()
        for path in sorted(self.directory.glob("*.yaml")):
            try:
                data = yaml.safe_load(path.read_text(encoding="utf-8"))
            except yaml.YAMLError as exc:
                raise ProjectKnowledgeError(f"Project 知识库 YAML 解析失败：{path}：{exc}") from exc
            if data is None:
                continue
            if not isinstance(data, dict):
                raise ProjectKnowledgeError(f"Project 知识库文件顶层应为单条项目字典：{path}")
            self._validate_item(path, data)
            name = str(data["name"]).strip()
            if name in seen_names:
                raise ProjectKnowledgeError(f"项目名重复：{name!r}")
            seen_names.add(name)
            projects.append(dict(data))

        if not projects:
            raise ProjectKnowledgeError(f"Project 知识库目录下未加载到任何项目：{self.directory}")
        self._projects = projects

    @staticmethod
    def _validate_item(source: Path, item: dict) -> None:
        if not isinstance(item, dict):
            raise ProjectKnowledgeError(f"项目条目应为字典：{source}")
        name = str(item.get("name", "")).strip()
        if not name:
            raise ProjectKnowledgeError(f"{source} 中存在缺 name 字段的项目")

        difficulty = str(item.get("difficulty", "")).strip()
        if difficulty not in VALID_DIFFICULTY:
            raise ProjectKnowledgeError(
                f"项目 {name!r} 的 difficulty={difficulty!r} 非法，应为 {', '.join(VALID_DIFFICULTY)}"
            )
        for key in ("covers_skills", "prerequisites", "tech_stack", "acceptance_criteria"):
            value = item.get(key)
            if not isinstance(value, list):
                raise ProjectKnowledgeError(
                    f"项目 {name!r} 的 {key} 应为列表，当前为 {type(value).__name__}"
                )
        domain = item.get("domain")
        if domain is not None and not str(domain).strip():
            raise ProjectKnowledgeError(f"项目 {name!r} 的 domain 不能为空字符串")

    # ── 查询 ────────────────────────────────────────────────────────────────
    @property
    def projects(self) -> list[dict]:
        """返回全部项目模板的浅拷贝副本。"""
        return [dict(p) for p in self._projects]

    def get(self, project_name: str) -> dict | None:
        """按项目名精确查询；未知项目返回 None。"""
        for p in self._projects:
            if p["name"] == project_name.strip():
                return dict(p)
        return None

    def missing_prerequisites(self, project: dict, student_levels: dict | None) -> list[str]:
        """项目前置技能里学生尚不具备的项。

        ``student_levels`` 为 None/空表示"不知道学生情况"（拿不到 Profile 数据），
        此时一律返回空列表 —— 宁可不过滤，也不要凭空把人挡在门外。
        """
        if not student_levels:
            return []
        missing = []
        for skill in project.get("prerequisites") or []:
            level = str(student_levels.get(str(skill).strip(), "") or "").strip()
            if level.lower() in _ABSENT_LEVELS:
                missing.append(str(skill).strip())
        return missing

    def recommend_for_user(
        self,
        skills: list[str],
        student_levels: dict | None = None,
        max_difficulty: str = MAX_EXECUTABLE_DIFFICULTY,
        domain: str | None = None,
    ) -> tuple[list[dict], list[dict]]:
        """按"可立即执行 / 暂不推荐"分组返回项目候选。

        ``domain`` 用于跨职业领域过滤：给了领域时只保留同领域项目（以及没有
        ``domain`` 字段的历史项目）。会计岗不会被推荐去做 RAG 问答系统，AI 岗
        也不会看到会计实训模板。传 None 表示不按领域过滤，保持旧行为。

        返回 `(executable, blocked)`：

        - ``executable``：难度不超过 ``max_difficulty``，且不会"前置技能一个都不具备"。
          已按 **难度从易到难** 排序（同难度覆盖多的在前）—— 上层按这个顺序注入，
          模型照抄也不会把最难的排到最前面。
        - ``blocked``：难度超限（如高级综合项目）或前置技能全缺的项目，
          附带 ``blocked_reason`` 与 ``missing_prerequisites``，供上层显式提示"暂不推荐"。

        评分同时考虑三件事，不再只看覆盖数量：覆盖的技能差距数、已具备的前置技能数、
        项目自身难度。两个列表里的条目都带 ``coverage`` / ``matched_skills`` /
        ``missing_prerequisites``，便于上层解释推荐理由。
        """
        capacity = DIFFICULTY_RANK.get(max_difficulty, DIFFICULTY_RANK[MAX_EXECUTABLE_DIFFICULTY])
        skill_set = {str(s).strip() for s in (skills or []) if str(s).strip()}
        wanted_domain = str(domain or "").strip()

        executable: list[dict] = []
        blocked: list[dict] = []

        for p in self._projects:
            if wanted_domain and p.get("domain") not in (None, wanted_domain):
                continue
            difficulty_value = DIFFICULTY_RANK[p["difficulty"]]
            covers = {str(c).strip() for c in p.get("covers_skills", [])}
            prereqs = [str(s).strip() for s in p.get("prerequisites", [])]
            matched = sorted(skill_set & covers)
            missing = self.missing_prerequisites(p, student_levels)
            coverage = len(matched)

            if difficulty_value > capacity:
                blocked.append(
                    self._decorate(p, coverage, matched, missing,
                                   f"难度 {p['difficulty']} 超出当前可执行上限 {max_difficulty}，"
                                   f"需要先补齐前置与工程基础")
                )
                continue
            if prereqs and len(missing) == len(prereqs):
                # 前置一个都不具备：不把它当作"当前就能做"的项目
                blocked.append(
                    self._decorate(p, coverage, matched, missing, "前置技能一个都不具备")
                )
                continue

            ready = len(prereqs) - len(missing)
            score = (
                coverage * _COVERAGE_WEIGHT
                + ready * _READY_PREREQ_WEIGHT
                - difficulty_value * _DIFFICULTY_PENALTY
            )
            item = self._decorate(p, coverage, matched, missing)
            item["score"] = score
            executable.append(item)

        # 难度递进：先按难度从易到难，同难度再看覆盖多少
        executable.sort(key=lambda c: (DIFFICULTY_RANK[c["difficulty"]], -c["coverage"], c["name"]))
        blocked.sort(key=lambda c: (DIFFICULTY_RANK[c["difficulty"]], c["name"]))
        return executable, blocked

    @staticmethod
    def _decorate(project: dict, coverage: int, matched: list, missing: list,
                  blocked_reason: str | None = None) -> dict:
        """给候选项副本补上可解释字段；不改动知识库里的原始数据。"""
        item = dict(project)
        item["coverage"] = coverage
        item["matched_skills"] = matched
        item["missing_prerequisites"] = missing
        if blocked_reason:
            item["blocked_reason"] = blocked_reason
        return item

    def recommend(self, skills: list[str], max_difficulty: str = MAX_DIFFICULTY,
                  domain: str | None = None) -> list[dict]:
        """按技能覆盖排序的候选项目（不检查前置技能，保持既有语义）。

        评分 = 覆盖技能数 * 100 + 已具备前置数 * 10 - 难度 * 5。
        难度是真实参与排序的项（旧版这一项恒为 0，等于没算）。

        参数
        ----
        skills : 学生的技能差距（或学习计划涉及技能），建议去重后传入。
        max_difficulty : 学生当前能做的最大难度，默认"高级"（不过度拦截，留由模型裁决）。
            只想要"当前就能上手"的项目时，请改用 :meth:`recommend_for_user`。
        """
        executable, _ = self.recommend_for_user(
            skills, student_levels=None, max_difficulty=max_difficulty, domain=domain
        )
        ordered = sorted(
            executable,
            key=lambda c: (-c["coverage"], DIFFICULTY_RANK[c["difficulty"]], c["name"]),
        )
        result = [c for c in ordered if c.get("covers_skills")]
        if not result:
            # 没有任何覆盖技能的项目也保留，但不伪装成"强匹配"
            return ordered
        return result