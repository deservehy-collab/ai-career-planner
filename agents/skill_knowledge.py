"""Skill 知识库查询模块（结构化 YAML）。

设计要点
--------
1. **只读**：本模块只读取 `knowledge/skills/*.yaml`，不写、不建索引、不依赖 Ollama。
2. **可测试**：目录通过构造注入，单元测试可指向临时目录或内存数据，不触碰真实文件。
3. **未知技能不悄悄替换**：`get` 对未知技能返回 `None`，`get_many` 同时返回
   `found` 与 `missing`，调用方必须显式处理未知技能，绝不把别的技能伪装成命中。
4. **运行时可降级**：加载或查询异常由调用方捕获并回退到依赖大模型的生成逻辑；
   本模块自身不静默吞错——知识库源文件不合法时抛错，让使用者清楚知道数据有问题。

技能名称的权威来源是 `knowledge/jobs/ai-application-engineer.yaml`（阶段二岗位画像）。
本模块以 `knowledge/skills/` 下的文件为准，二者由 `scripts/validate_knowledge.py` 交叉校验。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

# 与 agents/utils.py 的 SKILL_LEVELS 保持口径一致
SKILL_LEVELS = ("入门", "初级", "中级", "高级")
SKILL_LEVEL_STD = "level_guides"

#: 单个技能最多带几条学习资源进提示词。资源是给模型"安排学什么"的参考，
#: 不是要它逐条罗列；条数太多会挤占上下文，且容易让模型照抄成书目清单。
MAX_RESOURCES_PER_SKILL = 4


def _resource_lines(raw: object) -> list[str]:
    """学习资源 → 「标题（链接）」列表；缺标题或链接的条目直接跳过。"""
    if not isinstance(raw, list):
        return []
    lines: list[str] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        title = str(entry.get("title") or "").strip()
        url = str(entry.get("url") or "").strip()
        if title and url:
            lines.append(f"{title}（{url}）")
        if len(lines) >= MAX_RESOURCES_PER_SKILL:
            break
    return lines

DEFAULT_SKILL_DIR = Path(__file__).resolve().parent.parent / "knowledge" / "skills"


class SkillKnowledgeError(RuntimeError):
    """知识库源文件不合法或加载失败时抛出，便于上层区分"数据错"与"没查到"。"""


@dataclass
class SkillKnowledgeBase:
    """加载并查询 Skill 知识库。"""

    directory: Path = field(default_factory=lambda: DEFAULT_SKILL_DIR)
    _skills: dict[str, dict] = field(default_factory=dict, repr=False)
    _loaded: bool = field(default=False, repr=False)

    def __post_init__(self) -> None:
        self.load()

    # ── 加载 ────────────────────────────────────────────────────────────────
    def load(self) -> None:
        """读取目录下全部 YAML，合并成一个以技能名为键的字典。

        发现技能名重复、缺必填字段、四个等级标准不完整时抛 SkillKnowledgeError，
        不静默忽略。
        """
        skills: dict[str, dict] = {}
        if not self.directory.is_dir():
            raise SkillKnowledgeError(f"Skill 知识库目录不存在：{self.directory}")

        for path in sorted(self.directory.glob("*.yaml")):
            try:
                data = yaml.safe_load(path.read_text(encoding="utf-8"))
            except yaml.YAMLError as exc:
                raise SkillKnowledgeError(f"Skill 知识库 YAML 解析失败：{path}：{exc}") from exc

            if data is None:
                continue
            if not isinstance(data, list):
                raise SkillKnowledgeError(f"Skill 知识库文件顶层应为列表：{path}")

            for item in data:
                self._validate_item(path, item)
                name = str(item["skill"]).strip()
                if name in skills:
                    raise SkillKnowledgeError(
                        f"技能名重复：{name!r}（来自 {path} 与 {skills[name]}）"
                    )
                skills[name] = dict(item)

        if not skills:
            raise SkillKnowledgeError(f"Skill 知识库目录下未加载到任何技能：{self.directory}")

        self._skills = skills
        self._loaded = True

    @staticmethod
    def _validate_item(source: Path, item: dict) -> None:
        if not isinstance(item, dict):
            raise SkillKnowledgeError(f"技能条目应为字典：{source}")
        name = str(item.get("skill", "")).strip()
        if not name:
            raise SkillKnowledgeError(f"{source} 中存在缺 skill 字段的条目")

        guides = item.get(SKILL_LEVEL_STD)
        if not isinstance(guides, dict):
            raise SkillKnowledgeError(f"技能 {name!r} 缺少 level_guides（{source}）")
        missing = [lv for lv in SKILL_LEVELS if not str(guides.get(lv, "")).strip()]
        if missing:
            raise SkillKnowledgeError(
                f"技能 {name!r} 的 level_guides 缺少等级：{', '.join(missing)}（{source}）"
            )

    # ── 查询 ────────────────────────────────────────────────────────────────
    def all_skill_names(self) -> list[str]:
        """返回全部技能名（保持字典插入顺序）。"""
        return list(self._skills.keys())

    @property
    def size(self) -> int:
        return len(self._skills)

    def has(self, skill_name: str) -> bool:
        """精确判断某技能是否在知识库中。"""
        return skill_name.strip() in self._skills

    def get(self, skill_name: str) -> dict | None:
        """按规范技能名精确查询。

        返回该技能条目副本；未知技能返回 None（绝不返回别的技能）。
        """
        item = self._skills.get(skill_name.strip())
        return dict(item) if item else None

    def get_many(self, skill_names: list[str]) -> tuple[list[dict], list[str]]:
        """按多个技能名批量查询。

        返回 (found, missing)：found 为命中的技能条目，missing 为未命中的技能名。
        调用方必须处理 missing，禁止把未知技能静默当作命中。
        """
        found: list[dict] = []
        missing: list[str] = []
        for name in skill_names:
            item = self.get(name)
            if item is not None:
                found.append(item)
            else:
                missing.append(name)
        return found, missing

    def format_for_prompt(self, skill_names: list[str], step_limit: int = 3) -> str:
        """把若干技能的能力标准格式化，供 Learning/Project Agent 拼进 prompt。

        只输出存在对应等级标准的技能；未知技能不进入输出（由 get_many 另作处理）。
        """
        found, _ = self.get_many(skill_names)
        blocks: list[str] = []
        for item in found:
            guides = item.get(SKILL_LEVEL_STD) or {}
            lines = [f"【{item['skill']}】"]
            for lv in SKILL_LEVELS:
                text = str(guides.get(lv, "")).strip()
                if text:
                    lines.append(f"- {lv}：{text}")
            resources = _resource_lines(item.get("learning_resources"))
            if resources:
                lines.append(f"- 学习资源：{'；'.join(resources)}")
            blocks.append("\n".join(lines))
        return "\n\n".join(blocks)