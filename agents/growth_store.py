"""阶段五：成长记录的持久化层（``runs/growth.json``）。

职责边界
--------
本模块**只负责存取**：读快照、算指纹集合、追加历史、原子写、把历史与本次 profile
合并成基线。**不做任何等级判定**（那是 growth_rules 的活），也不调模型。

数据安全（本项目第一次引入写盘，口径从严）
------------------------------------------
- **只追加**：``history`` 只 append，既有条目一个字段都不改。
- **原子写**：临时文件 + ``os.replace()``，中途失败不会留下半截文件。
- **损坏不覆盖**：JSON 损坏时只告警 + 返回明确错误状态，**绝不覆盖、绝不删除**，
  并**拒绝后续写入**（否则一次 append 就会把损坏文件冲掉）。
  需要继续用，得显式调用 :meth:`GrowthStore.backup_corrupt` 做一次**复制**备份。
- **可注入目录**：测试一律指向 ``tempfile``，不碰项目的 ``runs/``。

"未收录"≠"知识库加载失败"（重要）
----------------------------------
:func:`load_skill_base` 返回显式状态：``ok`` / ``load_failed``。
- ``ok`` + ``base.has(skill) is False`` → 该技能**未收录**，跳过前置门禁是正常语义。
- ``load_failed`` → 知识库**没读起来**，前置门禁**本次根本没执行**，
  必须按 :data:`KB_UNAVAILABLE_WARNING` 明确告警，**不得静默当成"未收录"**。
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from agents.growth_rules import level_rank, pair_key
from agents.skill_knowledge import SkillKnowledgeBase

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_RUNS_DIR = REPO_ROOT / "runs"
DEFAULT_STORE_NAME = "growth.json"

SCHEMA_VERSION = 1

#: 用户作废成果时追加的调整记录类型。正常成长记录没有 kind 字段（保持向后兼容）。
ENTRY_KIND_ADJUSTMENT = "adjustment"
#: 完成后再补凭证时追加的验证记录类型：只覆盖证据类型，不新增升级。
ENTRY_KIND_VERIFICATION = "verification"
_STRONG_EVIDENCE_KINDS = ("project", "growth")

# ── 加载状态 ────────────────────────────────────────────────────────────
STATUS_OK = "ok"
STATUS_EMPTY = "empty"                     # 文件不存在，首次运行
STATUS_CORRUPT = "corrupt"                 # JSON 坏了 —— 只告警，不动文件
STATUS_UNREADABLE = "unreadable"           # 文件在但读不出来（权限/占用）
STATUS_SCHEMA_MISMATCH = "schema_mismatch"  # 版本对不上，不擅自迁移

# ── 写入状态 ────────────────────────────────────────────────────────────
WRITE_OK = "written"
WRITE_REFUSED = "refused"

_SAFE_TO_WRITE = (STATUS_OK, STATUS_EMPTY)


class GrowthStoreError(RuntimeError):
    """存储层不可恢复的错误（例如调用方传了非法条目）。"""


# ── 知识库加载状态（"未收录" vs "加载失败"）─────────────────────────────
KB_STATUS_OK = "ok"
KB_STATUS_LOAD_FAILED = "load_failed"

KB_UNAVAILABLE_WARNING = (
    "技能知识库加载失败：本次升级**没有执行**前置技能门禁，"
    "结果可能偏宽松。请修复 knowledge/skills/ 后重跑，不要把它当作"
    "「这些技能都未收录」——那是两回事。"
)


@dataclass
class SkillBaseStatus:
    """技能知识库的加载结果。

    ``base is None`` 只可能出现在 ``status == load_failed`` 的情况；
    调用方**必须**检查 ``status``，不能拿 ``base is None`` 当作"所有技能都未收录"。
    """

    base: SkillKnowledgeBase | None
    status: str
    warning: str | None = None

    @property
    def gate_available(self) -> bool:
        return self.status == KB_STATUS_OK and self.base is not None


def load_skill_base(directory=None) -> SkillBaseStatus:
    """加载技能知识库；**永不抛出**，用状态表达成功与失败。"""
    try:
        base = SkillKnowledgeBase(directory=directory) if directory else SkillKnowledgeBase()
    except Exception as exc:  # noqa: BLE001 - 加载失败必须以状态返回，不能中断流程
        return SkillBaseStatus(
            base=None,
            status=KB_STATUS_LOAD_FAILED,
            warning=f"{KB_UNAVAILABLE_WARNING}（{type(exc).__name__}: {exc}）",
        )
    return SkillBaseStatus(base=base, status=KB_STATUS_OK, warning=None)


# ── 载入结果 ────────────────────────────────────────────────────────────
@dataclass
class LoadResult:
    """一次 ``load()`` 的结果。

    ``data`` 在损坏/不可读时是**全新的空结构**，只用于本次运行降级，
    不代表磁盘上那份文件已经修好 —— ``status`` 才是真相。
    """

    data: dict
    status: str
    warning: str | None = None
    path: Path | None = None

    @property
    def writable(self) -> bool:
        """磁盘上的文件是否处于"可以安全追加"的状态。"""
        return self.status in _SAFE_TO_WRITE


@dataclass
class MergeResult:
    """历史与本次 profile 合并成基线（``user_skills_before``）的结果。"""

    user_skills: list[dict] = field(default_factory=list)
    restored: list[str] = field(default_factory=list)          # 只来自历史、本次 profile 没有
    disagreements: list[dict] = field(default_factory=list)    # profile 低于历史已确认值
    status: str = STATUS_EMPTY
    warning: str | None = None


def _empty_data(user_id: str) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "user_id": user_id,
        "created_at": None,
        "updated_at": None,
        "baseline": {"source": None, "at": None, "user_skills": []},
        "history": [],
    }


def _now(now: datetime | None = None) -> str:
    return (now or datetime.now()).isoformat(timespec="seconds")


def voided_index(entries) -> tuple[set[tuple[int, int]], set[str]]:
    """汇总所有调整记录里的作废引用。

    返回 ``(refs, fingerprints)``：
    - ``refs`` 是 ``(历史条目下标, 事件下标)`` 的集合；
    - ``fingerprints`` 是这些被作废事件对应的指纹。没有指纹的成果（被前置拦下、
      重复或非法）在作废时也会补算一个，避免下一轮又把它自动排进待办。
    """
    refs: set[tuple[int, int]] = set()
    fingerprints: set[str] = set()
    for entry in entries or []:
        if not isinstance(entry, dict) or entry.get("kind") != ENTRY_KIND_ADJUSTMENT:
            continue
        for item in entry.get("voided") or []:
            if not isinstance(item, dict):
                continue
            try:
                entry_index = int(item.get("entry_index"))
                event_index = int(item.get("event_index"))
            except (TypeError, ValueError):
                continue
            refs.add((entry_index, event_index))
            fingerprint = str(item.get("fingerprint") or "").strip()
            if fingerprint:
                fingerprints.add(fingerprint)
    return refs, fingerprints


class GrowthStore:
    """``runs/growth.json`` 的读写入口。目录可注入，默认项目下的 ``runs/``。"""

    def __init__(
        self,
        directory: str | Path | None = None,
        filename: str = DEFAULT_STORE_NAME,
        user_id: str = "default",
    ) -> None:
        self.directory = Path(directory) if directory is not None else DEFAULT_RUNS_DIR
        self.filename = filename
        self.user_id = user_id

    @property
    def path(self) -> Path:
        return self.directory / self.filename

    # ── 读 ──────────────────────────────────────────────────────────────
    def load(self) -> LoadResult:
        """读取成长记录。任何异常都转成状态，不抛出。"""
        path = self.path
        if not path.exists():
            return LoadResult(data=_empty_data(self.user_id), status=STATUS_EMPTY, path=path)

        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            return LoadResult(
                data=_empty_data(self.user_id),
                status=STATUS_UNREADABLE,
                warning=f"成长记录读取失败（{type(exc).__name__}: {exc}）；"
                        f"本次按 user_profile 基线继续，**原文件未改动**",
                path=path,
            )

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            return LoadResult(
                data=_empty_data(self.user_id),
                status=STATUS_CORRUPT,
                warning=f"成长记录不是合法 JSON（{exc}）；本次按 user_profile 基线继续，"
                        f"**原文件未覆盖也未删除**，请人工处理后重跑",
                path=path,
            )

        if not isinstance(data, dict) or not isinstance(data.get("history"), list):
            return LoadResult(
                data=_empty_data(self.user_id),
                status=STATUS_CORRUPT,
                warning="成长记录结构不合法（缺 history 列表）；本次按 user_profile 基线继续，"
                        "**原文件未覆盖也未删除**",
                path=path,
            )

        version = data.get("schema_version")
        if version != SCHEMA_VERSION:
            return LoadResult(
                data=_empty_data(self.user_id),
                status=STATUS_SCHEMA_MISMATCH,
                warning=f"成长记录 schema_version={version!r} 与本程序（{SCHEMA_VERSION}）不一致；"
                        f"不擅自迁移，本次按 user_profile 基线继续，**原文件未改动**",
                path=path,
            )

        return LoadResult(data=data, status=STATUS_OK, path=path)

    # ── 指纹（扫整个历史，不只最近一条）────────────────────────────────
    def seen_fingerprints(self, data: dict | None = None) -> set[str]:
        """历史里出现过的**全部**成果指纹。

        遍历所有历史条目的所有事件 —— 只查最近一条会漏掉更早的重复提交。
        被用户作废的成果仍然算"已处理"（它的指纹在调整记录里），否则下一轮会
        自动把同一条成果重新入账，作废就失去了意义。
        """
        payload = data if data is not None else self.load().data
        refs, fingerprints = voided_index(payload.get("history") or [])
        seen: set[str] = set(fingerprints)
        for entry_index, entry in enumerate(payload.get("history") or []):
            if not isinstance(entry, dict):
                continue
            for event_index, event in enumerate(entry.get("events") or []):
                if not isinstance(event, dict):
                    continue
                if (entry_index, event_index) in refs:
                    continue
                fingerprint = str(event.get("fingerprint") or "").strip()
                if fingerprint:
                    seen.add(fingerprint)
        return seen

    # ── 恢复已确认的能力 ────────────────────────────────────────────────
    def latest_skills(self, data: dict | None = None) -> list[dict]:
        """最近一次已确认的 ``user_skills_after``；没有历史时回落到 baseline。

        调整记录的空能力表是**有效结果**（作废后可能一项不剩），不能像普通
        空快照那样跳过 —— 否则界面会回显作废前的能力。

        旧版本的记录没有 ``evidence_kind``：凡是出现在历史 ``level_changes``
        里的技能，补标为 ``growth``（升级确实由成长事件产生）；其余按
        ``self_report`` 保守处理，不凭空把自述当成项目证据。
        """
        payload = data if data is not None else self.load().data
        for entry in reversed(payload.get("history") or []):
            if not isinstance(entry, dict) or not isinstance(entry.get("user_skills_after"), list):
                continue
            if entry["user_skills_after"]:
                return self._backfill_evidence_kind(
                    payload, [dict(s) for s in entry["user_skills_after"]]
                )
            if entry.get("kind") == ENTRY_KIND_ADJUSTMENT:
                return []
        baseline = payload.get("baseline") or {}
        return self._backfill_evidence_kind(
            payload, [dict(s) for s in (baseline.get("user_skills") or [])]
        )

    @staticmethod
    def _backfill_evidence_kind(payload: dict, skills: list[dict]) -> list[dict]:
        """给旧记录里的技能补证据类型；新记录已有的字段原样保留。"""
        growth_refs: dict[str, str] = {}
        for entry in payload.get("history") or []:
            if not isinstance(entry, dict):
                continue
            for change in entry.get("level_changes") or []:
                if not isinstance(change, dict):
                    continue
                key = pair_key(change.get("skill"))
                if key and key not in growth_refs:
                    growth_refs[key] = str(change.get("source") or "").strip()

        for item in skills:
            if not isinstance(item, dict):
                continue
            if item.get("evidence_kind") in ("growth", "project", "self_report"):
                continue
            key = pair_key(item.get("skill"))
            if key in growth_refs:
                item["evidence_kind"] = "growth"
                if growth_refs[key]:
                    item.setdefault("evidence_ref", growth_refs[key])
            else:
                item["evidence_kind"] = "self_report"
        return skills

    # ── 完成后再补凭证：只覆盖证据类型，不新增升级 ────────────────────
    def find_verification(self, task_id: str, data: dict | None = None) -> dict | None:
        """历史里是否已有针对这条任务的验证记录。"""
        payload = data if data is not None else self.load().data
        wanted = str(task_id or "").strip()
        if not wanted:
            return None
        for entry in payload.get("history") or []:
            if not isinstance(entry, dict):
                continue
            if entry.get("kind") == ENTRY_KIND_VERIFICATION and str(
                entry.get("task_id") or ""
            ).strip() == wanted:
                return entry
        return None

    def build_verification_entry(
        self,
        data: dict,
        task_id: str,
        title: str,
        skills: list | None,
        evidence: str,
        target_job: str | None = None,
        now: datetime | None = None,
    ) -> dict | None:
        """为"完成后补凭证"构造一条验证记录；找不到对应历史成果时返回 None。

        这条记录**不新增升级**：``level_changes`` 为空，只把同一成果涉及的技能
        的 ``evidence_kind`` 从 ``self_report`` 覆盖成 ``growth``，并重算
        ``user_skills_after``。原历史一条不改，``latest_skills`` 自然取到新快照。
        """
        history = [entry for entry in (data.get("history") or []) if isinstance(entry, dict)]
        key_skills: set[str] = set()
        matched_title = ""
        for entry in history:
            if entry.get("kind") in (ENTRY_KIND_ADJUSTMENT, ENTRY_KIND_VERIFICATION):
                continue
            for event in entry.get("events") or []:
                if not isinstance(event, dict):
                    continue
                if str(event.get("title") or "").strip() != str(title or "").strip():
                    continue
                matched_title = str(event.get("title") or "").strip()
                key_skills.update(
                    pair_key(s) for s in (event.get("skills") or []) if pair_key(s)
                )
        if not key_skills:
            return None
        wanted = {pair_key(s) for s in (skills or []) if pair_key(s)}
        if wanted:
            key_skills &= wanted
        if not key_skills:
            return None

        before = [dict(item) for item in self.latest_skills(data) if isinstance(item, dict)]
        after: list[dict] = []
        changed = 0
        for item in before:
            updated = dict(item)
            if pair_key(updated.get("skill")) in key_skills:
                updated["evidence"] = evidence
                updated["evidence_kind"] = "growth"
                updated["evidence_ref"] = matched_title or title
                changed += 1
            after.append(updated)

        return {
            "kind": ENTRY_KIND_VERIFICATION,
            "at": _now(now),
            "task_id": task_id,
            "target_job": str(target_job or ""),
            "events": [],
            "level_changes": [],
            "user_skills_before": before,
            "user_skills_after": after,
            "blocked_upgrades": [],
            "voided_event_indices": [],
            # 没有任何技能需要翻证据类型时，这条记录没有意义
            "changed_skills": changed,
        }

    def baseline_skills(self, data: dict | None = None) -> list[dict]:
        payload = data if data is not None else self.load().data
        baseline = payload.get("baseline") or {}
        return [dict(s) for s in (baseline.get("user_skills") or [])]

    def merge_with_profile(
        self,
        profile_skills: list,
        vocabulary=None,
        data: dict | None = None,
    ) -> MergeResult:
        """把历史已确认的能力与本次 profile 的提取结果合并成**基线**。

        合并规则（只升不降在跨运行层面同样成立）：

        1. 两边都有的技能 → **取序数更高的那个**（R4 只升不降）。
        2. 只在历史里有 → **保留历史值**（不因一次 profile 重跑就丢掉已确认的成长）。
        3. 只在本次 profile 里有 → 采用 profile 值。
        4. 历史值高于本次 profile 时，记入 ``disagreements`` —— **不静默**，
           让上层能显示"本次提取低于历史已确认值，已按历史保留"。

        返回的 ``user_skills`` 就是要喂给下游的 ``user_skills_before``。
        注意：**这不是** profile 的原始输出，否则同一份成长会在差异里被算两次
        （一次"从历史恢复"，一次"由本次成果升级"）。
        """
        loaded_result = self.load() if data is None else LoadResult(
            data=data, status=STATUS_OK, path=self.path
        )

        merged: dict[str, dict] = {}
        order: list[str] = []

        def put(raw_skill) -> None:
            key = pair_key(raw_skill.get("skill"), vocabulary)
            if not key:
                return
            if key not in merged:
                # 第一次出现的源决定 evidence：历史先处理，所以已确认的证据优先保留
                merged[key] = {
                    "skill": str(raw_skill.get("skill") or "").strip(),
                    "level": raw_skill.get("level"),
                    "evidence": str(raw_skill.get("evidence") or "").strip(),
                    "evidence_kind": str(raw_skill.get("evidence_kind") or "").strip(),
                    "evidence_ref": str(raw_skill.get("evidence_ref") or "").strip(),
                }
                order.append(key)
                return
            # 同键再次出现：只升等级，证据保持第一次出现的那份；
            # 但如果等级来自更强的证据源（项目/成长），不能还把证据标成自述。
            current = merged[key]
            if level_rank(raw_skill.get("level")) > level_rank(current.get("level")):
                current["level"] = raw_skill.get("level")
                raw_kind = str(raw_skill.get("evidence_kind") or "").strip()
                if raw_kind in ("project", "growth") and current.get("evidence_kind") not in (
                    "project", "growth"
                ):
                    current["evidence_kind"] = raw_kind
                    current["evidence"] = str(raw_skill.get("evidence") or "").strip()
                    current["evidence_ref"] = str(raw_skill.get("evidence_ref") or "").strip()

        stored = self.latest_skills(loaded_result.data)
        for item in stored:
            if isinstance(item, dict):
                put(item)
        for item in profile_skills or []:
            if isinstance(item, dict):
                put(item)

        stored_by_key = {pair_key(i.get("skill"), vocabulary): i for i in stored if isinstance(i, dict)}
        profile_by_key = {
            pair_key(i.get("skill"), vocabulary): i for i in (profile_skills or []) if isinstance(i, dict)
        }

        # restored 返回**展示用技能名**（merged 里第一次出现时记的那个），
        # 不是内部的配对键 —— 配对键在不传 vocabulary 时是小写形式，直接透出会难读
        restored = sorted(
            merged[key]["skill"] for key in stored_by_key
            if key not in profile_by_key and key in merged
        )
        disagreements = []
        for key, stored_item in stored_by_key.items():
            profile_item = profile_by_key.get(key)
            if profile_item is None:
                continue
            stored_rank = level_rank(stored_item.get("level"))
            profile_rank = level_rank(profile_item.get("level"))
            if profile_rank < stored_rank:
                disagreements.append({
                    "skill": merged[key]["skill"],
                    "profile_level": profile_item.get("level"),
                    "stored_level": stored_item.get("level"),
                    "kept": merged[key]["level"],
                })

        return MergeResult(
            user_skills=[merged[key] for key in order],
            restored=restored,
            disagreements=disagreements,
            status=loaded_result.status,
            warning=loaded_result.warning,
        )

    # ── 写 ──────────────────────────────────────────────────────────────
    def initialize(self, user_skills: list, now: datetime | None = None,
                   source: str = "user_profile") -> dict:
        """首次初始化：写入 baseline 与空历史。

        已经**有内容**的记录不会被覆盖（拒绝并说明），避免把历史冲掉。
        "有内容"指历史非空或 baseline 非空 —— 一个刚被
        :meth:`backup_corrupt` 重建过的空记录不算，允许重新初始化。
        """
        loaded = self.load()
        if not loaded.writable:
            return {"status": WRITE_REFUSED, "warning": loaded.warning, "path": str(self.path)}

        has_content = bool(loaded.data.get("history")) or bool(
            (loaded.data.get("baseline") or {}).get("user_skills")
        )
        if has_content:
            return {"status": WRITE_REFUSED, "warning": "成长记录已存在，拒绝覆盖既有历史",
                    "path": str(self.path)}

        stamp = _now(now)
        data = _empty_data(self.user_id)
        data["created_at"] = stamp
        data["updated_at"] = stamp
        data["baseline"] = {
            "source": source,
            "at": stamp,
            "user_skills": [dict(s) for s in (user_skills or [])],
        }
        self._write(data)
        return {"status": WRITE_OK, "path": str(self.path), "data": data}

    def append(self, entry: dict, now: datetime | None = None,
               baseline_skills: list | None = None) -> dict:
        """追加一条成长历史并原子落盘。

        - 既有历史条目**原样保留**（读出来什么就是什么，一个字段都不改）。
        - 磁盘上的文件损坏/不可读/版本不符时**拒绝写入**，
          绝不覆盖或删除损坏文件；需要继续用请先 :meth:`backup_corrupt`。
        - ``baseline_skills`` 只在记录里还没有 baseline 时用于补齐（首次 append
          发生在 initialize 之前的情况），不会覆盖已有的 baseline。
        """
        if not isinstance(entry, dict):
            raise GrowthStoreError("成长历史条目必须是字典")

        loaded = self.load()
        if not loaded.writable:
            return {
                "status": WRITE_REFUSED,
                "warning": loaded.warning or "成长记录当前不可安全写入",
                "path": str(self.path),
                "history_length": len((loaded.data.get("history") or [])),
            }

        data = json.loads(json.dumps(loaded.data))  # 深拷贝，避免污染入参
        history = data.setdefault("history", [])
        record = dict(entry)
        record.setdefault("at", _now(now))
        history.append(record)

        stamp = _now(now)
        data["updated_at"] = stamp
        if not data.get("created_at"):
            data["created_at"] = stamp
        if not (data.get("baseline") or {}).get("at"):
            data["baseline"] = {
                "source": "user_profile",
                "at": stamp,
                "user_skills": [dict(s) for s in (baseline_skills or [])],
            }

        self._write(data)
        return {"status": WRITE_OK, "path": str(self.path),
                "history_length": len(history), "data": data}

    def backup_corrupt(self, now: datetime | None = None, then_reset: bool = False) -> dict:
        """把损坏文件**复制**成带时间戳的备份，用于人工回看。

        **默认只复制，不碰原文件** —— 复制完仍然拒绝写入（因为原文件还在那儿）。
        只有在显式传 ``then_reset=True`` 时，才会在备份成功之后把 store 重置成空结构
        （原文件的字节已经完整保存在备份里）。这一步是**显式选择**，不会自动发生。
        """
        loaded = self.load()
        if loaded.status in _SAFE_TO_WRITE:
            return {"status": "not_needed", "path": str(self.path)}

        stamp = (now or datetime.now()).strftime("%Y%m%dT%H%M%S")
        target = self.path.with_name(f"{self.path.name}.corrupt.{stamp}")
        try:
            shutil.copy2(self.path, target)
        except OSError as exc:
            return {"status": "failed", "warning": f"备份失败：{type(exc).__name__}: {exc}",
                    "path": str(self.path)}

        result = {"status": "backed_up", "backup": str(target), "path": str(self.path)}
        if then_reset:
            self._write(_empty_data(self.user_id))
            result["status"] = "reset_after_backup"
            result["warning"] = (
                f"原文件已备份到 {target}，随后重建了空的成长记录；"
                f"旧内容请从备份文件中恢复"
            )
        return result

    # ── 内部 ────────────────────────────────────────────────────────────
    def _write(self, data: dict) -> None:
        """原子写：同目录临时文件 → fsync → ``os.replace``。

        不用 ``sort_keys``，保持既有条目的字段顺序原样往返。
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        handle, tmp_name = tempfile.mkstemp(
            dir=str(self.path.parent), prefix=self.path.name + ".", suffix=".tmp"
        )
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp_name, self.path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
