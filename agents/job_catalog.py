"""岗位目录：把 `knowledge/jobs/*.yaml` 读成「岗位 → 领域 → 规范技能」的映射。

为什么需要它
------------
阶段二的岗位画像只服务 AI 应用开发一个岗位，技能词表也硬编码指向那一个文件。
一旦知识库扩到会计、运营、教师，三条链路会同时断：

- 技能词表不认识新岗位的技能名，成长记账会把真实成果判成"无法归一"；
- 项目库无法区分领域，AI 学生会被推荐会计项目，反之亦然；
- 校验脚本只认一份岗位画像，新文件永远过不了交叉校验。

本模块只做**查表**：不调模型、不写文件、不做语义联想。领域归属以岗位名/别名
的确定性匹配为准；匹配不到时才退回用技能名投票，且只在唯一领域时采用。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_JOB_DIR = REPO_ROOT / "knowledge" / "jobs"

__all__ = ["DEFAULT_JOB_DIR", "JobCatalog", "JobCatalogError"]

# 领域标签用于项目模板过滤，必须与 knowledge/projects/*.yaml 的 domain 一致。
VALID_DOMAINS = (
    "ai-application", "accounting", "operations", "teaching",
    "data-analysis", "software-testing",
    "human-resources", "design", "healthcare", "electronics",
    "mechanical-civil", "media-language", "legal-public",
    "agriculture-environment", "sports", "bio-chemical",
)


class JobCatalogError(RuntimeError):
    """岗位目录源文件不合法时抛出，便于上层区分"数据错"与"没匹配到"。"""


def _norm(text: object) -> str:
    return "".join(str(text or "").split()).lower()


@dataclass
class JobCatalog:
    """加载全部岗位画像，提供技能并集与领域解析。"""

    directory: Path = field(default_factory=lambda: DEFAULT_JOB_DIR)
    _profiles: list[dict] = field(default_factory=list, repr=False)
    _skills_by_domain: dict[str, list[str]] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        self.load()

    def load(self) -> None:
        profiles: list[dict] = []
        skills_by_domain: dict[str, list[str]] = {}
        seen_ids: set[str] = set()

        if not self.directory.is_dir():
            raise JobCatalogError(f"岗位目录不存在：{self.directory}")

        for path in sorted(self.directory.glob("*.yaml")):
            try:
                data = yaml.safe_load(path.read_text(encoding="utf-8"))
            except yaml.YAMLError as exc:
                raise JobCatalogError(f"岗位画像 YAML 解析失败：{path}：{exc}") from exc
            if data is None:
                continue
            self._validate_item(path, data)

            job_id = str(data.get("job_id") or "").strip()
            if job_id in seen_ids:
                raise JobCatalogError(f"岗位 ID 重复：{job_id!r}（{path}）")
            seen_ids.add(job_id)

            domain = str(data.get("domain") or "").strip()
            if domain and domain not in VALID_DOMAINS:
                raise JobCatalogError(
                    f"岗位 {job_id!r} 的 domain={domain!r} 非法，应为 {', '.join(VALID_DOMAINS)}"
                )

            profiles.append(dict(data))
            if domain:
                bucket = skills_by_domain.setdefault(domain, [])
                for item in data["skills"]:
                    name = str(item["skill"]).strip()
                    if name not in bucket:
                        bucket.append(name)

        if not profiles:
            raise JobCatalogError(f"岗位目录下未加载到任何岗位：{self.directory}")

        self._profiles = profiles
        self._skills_by_domain = skills_by_domain

    @staticmethod
    def _validate_item(source: Path, item: object) -> None:
        if not isinstance(item, dict):
            raise JobCatalogError(f"岗位画像顶层应为字典：{source}")
        if not str(item.get("job_id") or "").strip():
            raise JobCatalogError(f"{source} 缺少 job_id")
        if not str(item.get("job_name") or "").strip():
            raise JobCatalogError(f"{source} 缺少 job_name")
        skills = item.get("skills")
        if not isinstance(skills, list) or not skills:
            raise JobCatalogError(f"{source} 缺少非空 skills 列表")
        for skill in skills:
            if not isinstance(skill, dict) or not str(skill.get("skill") or "").strip():
                raise JobCatalogError(f"{source} 的 skills 中存在缺少 skill 字段的条目")

        if "ground_requirements" in item and not isinstance(item["ground_requirements"], bool):
            raise JobCatalogError(f"{source} 的 ground_requirements 必须是布尔值")

        if "profile" in item:
            profile = item.get("profile")
            if not isinstance(profile, dict):
                raise JobCatalogError(f"{source} 的 profile 应为字典")
            if not str(profile.get("summary") or "").strip():
                raise JobCatalogError(f"{source} 的 profile 缺少 summary")
            for key in ("core_tasks", "tools", "deliverables", "entry_requirements", "career_path"):
                entries = profile.get(key)
                if not isinstance(entries, list) or not entries:
                    raise JobCatalogError(f"{source} 的 profile.{key} 应为非空列表")
                for entry in entries:
                    if isinstance(entry, str):
                        name = entry.strip()
                    elif isinstance(entry, dict):
                        name = str(entry.get("name") or "").strip()
                    else:
                        name = ""
                    if not name:
                        raise JobCatalogError(f"{source} 的 profile.{key} 存在缺少 name 的条目")

    # ── 查询 ────────────────────────────────────────────────────────────
    @property
    def profiles(self) -> list[dict]:
        return [dict(p) for p in self._profiles]

    @property
    def skill_names(self) -> list[str]:
        """全部岗位的技能名并集（保序去重）。"""
        names: list[str] = []
        for profile in self._profiles:
            for item in profile.get("skills") or []:
                name = str(item["skill"]).strip()
                if name not in names:
                    names.append(name)
        return names

    @property
    def domains(self) -> list[str]:
        return list(self._skills_by_domain)

    def skills_for_domain(self, domain: str) -> list[str]:
        return list(self._skills_by_domain.get(str(domain or "").strip(), []))

    def domain_names(self) -> dict[str, str]:
        """``{job_name: domain}``，供校验与展示使用。"""
        return {
            str(p.get("job_name")): str(p.get("domain") or "")
            for p in self._profiles
        }

    def resolve_profile(self, target_job: object) -> dict | None:
        """按岗位名/别名解析到具体岗位画像；认不出返回 None。

        先精确匹配，再按"最长别名是目标岗位子串"兜底（"会计助理" → 会计）。
        不做分词与语义联想，避免把"AI 产品经理"误认成 AI 应用开发。
        """
        target = _norm(target_job)
        if not target:
            return None

        for profile in self._profiles:
            names = [profile.get("job_name")] + list(profile.get("aliases") or [])
            for name in names:
                if _norm(name) == target:
                    return dict(profile)

        candidates: list[tuple[int, dict]] = []
        for profile in self._profiles:
            names = [profile.get("job_name")] + list(profile.get("aliases") or [])
            for name in names:
                needle = _norm(name)
                if len(needle) >= 2 and needle in target:
                    candidates.append((len(needle), profile))
        if not candidates:
            return None
        return dict(max(candidates, key=lambda pair: pair[0])[1])

    def resolve_domain(self, target_job: object) -> str | None:
        """按岗位名/别名把用户输入解析到领域；认不出返回 None。"""
        profile = self.resolve_profile(target_job)
        if profile is None:
            return None
        return str(profile.get("domain") or "").strip() or None

    def domain_for_skills(self, skills: list[str]) -> str | None:
        """技能名 → 唯一领域；跨领域或认不出时返回 None（宁可不筛，也不猜）。"""
        wanted = {str(s).strip() for s in (skills or []) if str(s).strip()}
        if not wanted:
            return None
        matched = {
            domain
            for domain, names in self._skills_by_domain.items()
            if wanted & set(names)
        }
        return matched.pop() if len(matched) == 1 else None
