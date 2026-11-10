"""阶段六：成长闭环的持久化层（``runs/loop.json``）。

职责边界
--------
本模块**只负责存取**：保存每一轮的「目标岗位 + 学习计划 + 项目计划 + 任务完成状态」，
读回最新轮次，按任务 ID 标注完成。它**不**更新技能等级、**不**改 ``user_skills``、
**不**调用模型 —— 能力怎么变是阶段五（``growth_rules`` / ``growth_agent``）的事，
这里只如实记录"哪个任务做完了、拿什么当证据"。

与 ``growth.json`` 的关系
-------------------------
两份文件**互不引用、互不覆盖**：``growth.json`` 记"能力确认"，``loop.json`` 记
"任务进度"。谁都不读写对方的文件，因此本模块的任何操作都不可能动到成长记录。

数据安全（沿用 GrowthStore 的损坏保护口径）
------------------------------------------
- **只追加**：``rounds`` 只 append。既有轮次与任务原样保留；唯一会变的字段是
  :meth:`LoopStore.mark_completed` 标注的那**一个**任务的 ``status`` /
  ``completed_at`` / ``evidence``，外加顶层的 ``updated_at``。
- **原子写**：同目录临时文件 → ``fsync`` → ``os.replace()``。中途失败会删掉临时文件，
  **磁盘上原文件一个字节都不动**。
- **损坏不覆盖**：JSON 坏了 / 结构不合法 / ``schema_version`` 对不上时，只告警 +
  返回明确状态，**绝不覆盖、绝不删除**，并**拒绝后续写入**（否则一次 append 就会把
  损坏文件冲掉）。要继续用，得显式调用 :meth:`LoopStore.backup_corrupt` 做一次
  **复制**备份。
- **可注入目录**：测试一律指向 ``tempfile``，不碰项目的 ``runs/``。

错误处理约定（本模块内部统一口径）
----------------------------------
- **数据结构错误 → 抛** :class:`LoopStoreError`：``learning_plan`` / ``project_plan``
  不是列表、元素不是字典之类，属于调用方的编程错误，必须立刻听见。
- **标识符与文件现状 → 返回状态码**：任务 ID 格式不对或不存在、轮次号非法或不存在、
  存储不可写、原子写失败。这些来自用户输入或磁盘现状，不该让程序崩。

任何**不是** :data:`WRITE_OK` 的写入结果，都意味着**磁盘文件没有被改动**。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_RUNS_DIR = REPO_ROOT / "runs"
DEFAULT_STORE_NAME = "loop.json"

SCHEMA_VERSION = 1

# ── 加载状态（与 GrowthStore 同名同义，便于上层统一处理）──────────────────
STATUS_OK = "ok"
STATUS_EMPTY = "empty"                      # 文件不存在，首次运行
STATUS_CORRUPT = "corrupt"                  # JSON 坏了或结构不合法 —— 只告警，不动文件
STATUS_UNREADABLE = "unreadable"            # 文件在但读不出来（权限/占用）
STATUS_SCHEMA_MISMATCH = "schema_mismatch"  # 版本对不上，不擅自迁移

# ── 写入状态 ────────────────────────────────────────────────────────────
WRITE_OK = "written"
WRITE_REFUSED = "refused"           # 存储当前不可安全写入（损坏/版本不符/读不到）
WRITE_FAILED = "write_failed"       # 原子写本身失败，原文件未改动

_SAFE_TO_WRITE = (STATUS_OK, STATUS_EMPTY)

# ── 任务状态 ────────────────────────────────────────────────────────────
TASK_PENDING = "pending"
TASK_COMPLETED = "completed"
TASK_STATUSES = (TASK_PENDING, TASK_COMPLETED)

# ── 任务类型 ────────────────────────────────────────────────────────────
KIND_LEARNING = "learning"
KIND_PROJECT = "project"
KINDS = (KIND_LEARNING, KIND_PROJECT)
_KIND_PREFIX = {KIND_LEARNING: "l", KIND_PROJECT: "p"}

# 各类型从计划条目里取标题 / 技能名的字段
_TITLE_FIELD = {KIND_LEARNING: "topic", KIND_PROJECT: "name"}
_SKILLS_FIELD = {KIND_LEARNING: "covers", KIND_PROJECT: "covers_gaps"}

# ── 轮次号的状态码（append_round 与 mark_completed 共用）────────────────
ROUND_INVALID = "invalid_round"  # 不是正整数，或 append_round 里没有严格递增
ROUND_UNKNOWN = "unknown_round"  # 指定的轮次在文件里不存在

# ── mark_completed 的返回状态 ───────────────────────────────────────────
COMPLETE_OK = "completed"                 # 本次标注成功
COMPLETE_ALREADY = "already_completed"    # 之前就已完成，不重复登记、不写盘
COMPLETE_UNKNOWN_TASK = "unknown_task"    # ID 格式合法但文件里没有
COMPLETE_INVALID_TASK_ID = "invalid_task_id"   # ID 不是字符串或不符合格式
COMPLETE_INVALID_EVIDENCE = "invalid_evidence"  # 证据不是字符串

# 证据类型：默认空串表示旧记录；growth=附了凭证，self_report=自述完成。
TASK_EVIDENCE_KIND_GROWTH = "growth"
TASK_EVIDENCE_KIND_SELF_REPORT = "self_report"

# ── attach_evidence（任务完成后补凭证）──────────────────────────────────
ATTACH_OK = "attached"
ATTACH_ALREADY = "already_attached"       # 同样的凭证已经记过，不写盘
ATTACH_NOT_COMPLETED = "not_completed"    # 任务还没完成，不能补凭证

# ── reopen（撤回一条尚未计入成长历史的完成记录）──────────────────────────
REOPEN_OK = "reopened"
REOPEN_NOT_COMPLETED = "not_completed"    # 本来就是待完成，不写盘

# 任务 ID：r<轮次三位以上>-<类型字母><序号两位以上>，例如 r001-l01 / r012-p03。
# 类型字母从 _KIND_PREFIX 派生，避免第二处定义同一件事。
_KIND_LETTERS = "".join(sorted(set(_KIND_PREFIX.values())))
_TASK_ID_RE = re.compile(rf"^r(\d{{3,}})-([{_KIND_LETTERS}])(\d{{2,}})$")


class LoopStoreError(RuntimeError):
    """存储层不可恢复的错误（调用方传了结构不合法的数据）。"""


@dataclass
class LoadResult:
    """一次 ``load()`` 的结果。

    ``data`` 在损坏/不可读时是**全新的空结构**，只用于本次降级，
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


def _empty_data(user_id: str) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "user_id": user_id,
        "created_at": None,
        "updated_at": None,
        "rounds": [],
    }


def _stamp(now: datetime | None = None) -> str:
    """时间戳。``now`` 只接受 ``datetime`` 或 ``None``，其余是调用方 bug。"""
    if now is not None and not isinstance(now, datetime):
        raise LoopStoreError(f"now 必须是 datetime 或 None，收到 {type(now).__name__}")
    return (now or datetime.now()).isoformat(timespec="seconds")


def _deepcopy(value):
    """经 JSON 往返的深拷贝。入参与出参、返回给调用方的数据与待写盘的数据不共享引用。"""
    return json.loads(json.dumps(value, ensure_ascii=False))


def task_id_for(round_no: int, kind: str, index: int) -> str:
    """任务 ID 由「轮次 + 类型 + 序号」拼成，全局唯一且可读。

    因为 ID 里带了轮次号，**同一编号的轮次不允许重复写入**（见 :meth:`LoopStore.append_round`）——
    否则两轮的任务会撞 ID。
    """
    if kind not in _KIND_PREFIX:
        raise LoopStoreError(f"未知任务类型：{kind!r}（可选 {KINDS}）")
    return f"r{int(round_no):03d}-{_KIND_PREFIX[kind]}{int(index):02d}"


def _skills_of(item: dict, kind: str) -> list[str]:
    """从计划条目里取该任务覆盖的技能名（只做字符串清洗，不做归一/判定）。"""
    raw = item.get(_SKILLS_FIELD[kind]) or []
    if not isinstance(raw, list):
        return []
    return [str(s).strip() for s in raw if str(s or "").strip()]


def _round_no_error(round_no) -> str | None:
    """轮次号不合法时返回原因，合法返回 ``None``（``bool`` 不算正整数）。"""
    if isinstance(round_no, bool) or not isinstance(round_no, int) or round_no < 1:
        return f"轮次号必须是正整数，收到 {round_no!r}"
    return None


def _check_plan(kind: str, plan) -> None:
    """校验计划是不是"字典组成的列表"。不合法就抛，属于调用方 bug。"""
    items = plan or []
    if not isinstance(items, list):
        raise LoopStoreError(f"{kind} 计划必须是列表，收到 {type(items).__name__}")
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            raise LoopStoreError(f"{kind} 计划的第 {index} 项必须是字典，收到 {type(item).__name__}")


def build_tasks(
    round_no: int,
    learning_plan: list | None,
    project_plan: list | None,
    created_at: str,
) -> list[dict]:
    """把一轮的学习计划与项目计划展平成任务清单（纯函数，不读盘不写盘）。

    计划条目的其余字段（``goal`` / ``prerequisite`` / ``deliverable`` 等）**不复制进来**：
    它们是模型写的文字，原样躺在 ``learning_plan`` / ``project_plan`` 里，
    任务只是"可勾选的一行"。这样既有唯一真相，也不会出现两处文字不同步。
    """
    tasks: list[dict] = []
    for kind, plan in ((KIND_LEARNING, learning_plan), (KIND_PROJECT, project_plan)):
        _check_plan(kind, plan)
        for index, item in enumerate(plan or [], start=1):
            tasks.append({
                "task_id": task_id_for(round_no, kind, index),
                "kind": kind,
                "round": round_no,
                "index": index,
                "title": str(item.get(_TITLE_FIELD[kind]) or "").strip(),
                "skills": _skills_of(item, kind),
                "status": TASK_PENDING,
                "created_at": created_at,
                "completed_at": None,
                "evidence": "",
                "evidence_kind": "",
                "note": "",
            })
    return tasks


def _goal_record(goal: dict, round_no: int, target_job: str, stamp: str) -> dict:
    """顶层 ``latest_goal`` 的唯一形状；轮次快照与单独记录结局共用。"""
    return {
        "round": round_no,
        "status": goal.get("status"),
        "goal": _deepcopy(goal),
        "target_job": target_job,
        "evaluated_at": stamp,
    }


class LoopStore:
    """``runs/loop.json`` 的读写入口。目录可注入，默认项目下的 ``runs/``。

    无内存状态：每次调用都重新 ``load()`` → 改副本 → 原子写。因此"上一个调用失败"
    不可能污染下一个调用，磁盘也永远只有一份真相。
    """

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
        """读取闭环记录。任何异常都转成状态，不抛出。"""
        path = self.path
        if not path.exists():
            return LoadResult(data=_empty_data(self.user_id), status=STATUS_EMPTY, path=path)

        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            return LoadResult(
                data=_empty_data(self.user_id),
                status=STATUS_UNREADABLE,
                warning=f"闭环记录读取失败（{type(exc).__name__}: {exc}）；"
                        f"本次按空记录继续，**原文件未改动**",
                path=path,
            )

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            return LoadResult(
                data=_empty_data(self.user_id),
                status=STATUS_CORRUPT,
                warning=f"闭环记录不是合法 JSON（{exc}）；本次按空记录继续，"
                        f"**原文件未覆盖也未删除**，请人工处理后重跑",
                path=path,
            )

        if not isinstance(data, dict):
            return LoadResult(
                data=_empty_data(self.user_id),
                status=STATUS_CORRUPT,
                warning="闭环记录顶层不是对象；本次按空记录继续，**原文件未覆盖也未删除**",
                path=path,
            )

        problem = _structure_problem(data)
        if problem:
            return LoadResult(
                data=_empty_data(self.user_id),
                status=STATUS_CORRUPT,
                warning=f"闭环记录结构不合法（{problem}）；本次按空记录继续，"
                        f"**原文件未覆盖也未删除**",
                path=path,
            )

        version = data.get("schema_version")
        if version != SCHEMA_VERSION:
            return LoadResult(
                data=_empty_data(self.user_id),
                status=STATUS_SCHEMA_MISMATCH,
                warning=f"闭环记录 schema_version={version!r} 与本程序（{SCHEMA_VERSION}）不一致；"
                        f"不擅自迁移，本次按空记录继续，**原文件未改动**",
                path=path,
            )

        return LoadResult(data=data, status=STATUS_OK, path=path)

    # ── 查询 ────────────────────────────────────────────────────────────
    def rounds(self, data: dict | None = None) -> list[dict]:
        """全部轮次（深拷贝），按文件顺序。"""
        payload = data if data is not None else self.load().data
        return _deepcopy(payload.get("rounds") or [])

    def latest_round(self, data: dict | None = None) -> dict | None:
        """轮次号最大的那一轮；一轮都没有时返回 ``None``。"""
        rounds = self.rounds(data)
        if not rounds:
            return None
        return max(rounds, key=lambda item: item.get("round", 0))

    def latest_round_number(self, data: dict | None = None) -> int:
        """最新轮次号；没有轮次时返回 ``0``（因此首轮是 1）。"""
        latest = self.latest_round(data)
        return int(latest["round"]) if latest else 0

    def get_round(self, round_no: int, data: dict | None = None) -> dict | None:
        """按轮次号取一轮；不存在返回 ``None``。"""
        for item in self.rounds(data):
            if item.get("round") == round_no:
                return item
        return None

    def latest_goal(self, data: dict | None = None) -> dict | None:
        """最近一次持久化的结局快照；没有则 ``None``。

        顶层 ``latest_goal`` 与轮次里的快照是**两份**：轮次快照回答"这一轮计划
        当初基于什么"，``latest_goal`` 回答"当前计划最近一次判定成了什么"。
        刷新页面后前端靠它继续显示结局，不必要求用户重跑一次分析。
        """
        payload = data if data is not None else self.load().data
        goal = payload.get("latest_goal")
        return _deepcopy(goal) if isinstance(goal, dict) else None

    def pending_next_round(self, data: dict | None = None) -> dict | None:
        """上一次「生成下一轮」没把计划存下来时留下的恢复标记。"""
        payload = data if data is not None else self.load().data
        pending = payload.get("pending_next_round")
        return _deepcopy(pending) if isinstance(pending, dict) else None

    def tasks(
        self,
        round_no: int | None = None,
        status: str | None = None,
        data: dict | None = None,
    ) -> list[dict]:
        """按轮次、按状态筛选任务（两者都可省略）。"""
        if status is not None and status not in TASK_STATUSES:
            raise LoopStoreError(f"未知任务状态：{status!r}（可选 {TASK_STATUSES}）")
        result: list[dict] = []
        for item in self.rounds(data):
            if round_no is not None and item.get("round") != round_no:
                continue
            for task in item.get("tasks") or []:
                if status is None or task.get("status") == status:
                    result.append(_deepcopy(task))
        return result

    def find_task(self, task_id: str, data: dict | None = None) -> dict | None:
        """按任务 ID 找任务；找不到返回 ``None``（不校验 ID 格式）。"""
        found = self._locate(data if data is not None else self.load().data, task_id)
        return _deepcopy(found[1]) if found else None

    # ── 写 ──────────────────────────────────────────────────────────────
    def initialize(self, now: datetime | None = None) -> dict:
        """写出一个空记录（``rounds: []``），用于显式建立文件。

        已经有轮次时**拒绝**，避免把既有进度冲掉。
        """
        loaded = self.load()
        if not loaded.writable:
            return {"status": WRITE_REFUSED, "warning": loaded.warning, "path": str(self.path)}
        if loaded.data.get("rounds"):
            return {"status": WRITE_REFUSED, "warning": "闭环记录已存在轮次，拒绝覆盖",
                    "path": str(self.path)}

        stamp = _stamp(now)
        data = _empty_data(self.user_id)
        data["created_at"] = stamp
        data["updated_at"] = stamp
        try:
            self._write(data)
        except OSError as exc:
            return self._failed(exc)
        return {"status": WRITE_OK, "path": str(self.path), "data": data}

    def clear(self, now: datetime | None = None) -> dict:
        """把本用户的闭环记录**先备份、再重置为空**。

        这是本模块唯一会丢掉既有轮次的操作，所以三条不退让：

        1. 先把原文件完整复制成 ``loop.json.cleared-<时间戳>``，复制不成功就一个字节都不动；
        2. 损坏 / 读不到 / 版本不符的文件**拒绝**清空 —— 那种情况该走
           :meth:`backup_corrupt`，不能拿"清空"把问题掩盖掉；
        3. 只动 ``loop.json``。成长历史在 ``growth.json``，本模块从不读写它，
           所以清空不碰技能等级与已确认的成果。

        返回 ``{status, path, backup, rounds_removed, tasks_removed, warning}``；
        ``status`` 不是 :data:`WRITE_OK` 时磁盘未改动。
        """
        loaded = self.load()
        if loaded.status not in _SAFE_TO_WRITE:
            return {
                "status": WRITE_REFUSED,
                "warning": loaded.warning,
                "path": str(self.path),
                "backup": None,
                "rounds_removed": 0,
                "tasks_removed": 0,
            }

        rounds = loaded.data.get("rounds") or []
        task_count = sum(len(entry.get("tasks") or []) for entry in rounds)
        if not rounds:
            # 本来就没有轮次：不制造一个"备份了个空文件"的假动作。
            return {
                "status": WRITE_OK,
                "warning": None,
                "path": str(self.path),
                "backup": None,
                "rounds_removed": 0,
                "tasks_removed": 0,
            }

        moment = now or datetime.now()
        target = self.path.with_name(
            f"{self.path.name}.cleared-{moment.strftime('%Y%m%dT%H%M%S')}"
        )
        try:
            shutil.copy2(self.path, target)
        except OSError as exc:
            return {
                "status": WRITE_FAILED,
                "warning": f"备份失败，任务清单一个字节都没动（{type(exc).__name__}: {exc}）",
                "path": str(self.path),
                "backup": None,
                "rounds_removed": 0,
                "tasks_removed": 0,
            }

        data = _empty_data(self.user_id)
        data["created_at"] = _stamp(moment)
        data["updated_at"] = _stamp(moment)
        try:
            self._write(data)
        except OSError as exc:
            return {
                "status": WRITE_FAILED,
                "warning": f"原清单已备份到 {target.name}，但重建空记录失败，原文件未改动：{exc}",
                "path": str(self.path),
                "backup": str(target),
                "rounds_removed": 0,
                "tasks_removed": 0,
            }

        return {
            "status": WRITE_OK,
            "warning": None,
            "path": str(self.path),
            "backup": str(target),
            "rounds_removed": len(rounds),
            "tasks_removed": task_count,
        }

    def append_round(
        self,
        target_job: str,
        learning_plan: list | None = None,
        project_plan: list | None = None,
        round_no: int | None = None,
        now: datetime | None = None,
        snapshot: dict | None = None,
        source: str | None = None,
        latest_goal: dict | None = None,
        clear_pending: bool = False,
    ) -> dict:
        """追加一轮，并把两份计划展平成任务清单。

        - ``round_no`` 省略时自动取「当前最大轮次 + 1」。
        - 显式给轮次号时必须是正整数，且**严格大于**已有的最大轮次号：
          轮次号会进入任务 ID，重复就会撞 ID，所以宁可拒绝也不允许复用。
        - 计划条目**原样**存进 ``learning_plan`` / ``project_plan``，任务只是索引其上的
          一行"可勾选事项"，不复制模型写的文字。
        - ``snapshot`` 是这一轮计划的来源快照（目标、JD 原文、岗位要求与结局），
          下一轮据此沿用同一份岗位依据，不再让模型重新推断。旧文件没有这个键，
          读取方必须按"没有快照"处理。
        - ``latest_goal`` 给了就同时更新顶层结局；``clear_pending`` 为真时清掉
          「上一轮未保存」恢复标记（开始新计划会显式清掉它）。
        - 磁盘文件损坏/不可读/版本不符时**拒绝写入**，绝不覆盖；见 :meth:`backup_corrupt`。
        """
        if not isinstance(target_job, str):
            raise LoopStoreError(f"target_job 必须是字符串，收到 {type(target_job).__name__}")
        if snapshot is not None and not isinstance(snapshot, dict):
            raise LoopStoreError(f"snapshot 必须是字典，收到 {type(snapshot).__name__}")
        if latest_goal is not None and (
            not isinstance(latest_goal, dict) or not latest_goal.get("status")
        ):
            raise LoopStoreError("latest_goal 必须是一个带 status 的字典")

        # 计划结构不合法要**早于任何读盘/写盘**被发现，所以先做纯校验
        _check_plan(KIND_LEARNING, learning_plan)
        _check_plan(KIND_PROJECT, project_plan)

        stamp = _stamp(now)

        if round_no is not None:
            problem = _round_no_error(round_no)
            if problem:
                return {"status": ROUND_INVALID, "warning": problem, "path": str(self.path)}

        loaded = self.load()
        if not loaded.writable:
            return {"status": WRITE_REFUSED, "warning": loaded.warning or "闭环记录当前不可安全写入",
                    "path": str(self.path), "round_count": len(loaded.data.get("rounds") or [])}

        data = _deepcopy(loaded.data)
        rounds = data.setdefault("rounds", [])
        current_max = max((int(r.get("round") or 0) for r in rounds), default=0)

        if round_no is None:
            resolved = current_max + 1
        else:
            resolved = round_no
            if resolved <= current_max:
                return {
                    "status": ROUND_INVALID,
                    "warning": f"轮次号必须严格大于已有的最大轮次（{current_max}），"
                               f"收到 {resolved}；轮次号会进入任务 ID，不允许复用",
                    "path": str(self.path),
                }

        tasks = build_tasks(resolved, learning_plan, project_plan, stamp)

        entry = {
            "round": resolved,
            "created_at": stamp,
            "target_job": target_job,
            "learning_plan": _deepcopy(learning_plan or []),
            "project_plan": _deepcopy(project_plan or []),
            "tasks": tasks,
        }
        if source:
            entry["source"] = str(source)
        if snapshot is not None:
            entry["snapshot"] = _deepcopy(snapshot)
        rounds.append(entry)
        if latest_goal is not None:
            data["latest_goal"] = _goal_record(latest_goal, resolved, target_job, stamp)
        if clear_pending:
            data.pop("pending_next_round", None)
        data["updated_at"] = stamp
        if not data.get("created_at"):
            data["created_at"] = stamp

        try:
            self._write(data)
        except OSError as exc:
            return self._failed(exc)

        return {
            "status": WRITE_OK,
            "path": str(self.path),
            "round": resolved,
            "round_count": len(rounds),
            "tasks": _deepcopy(tasks),
            "latest_goal": _deepcopy(data.get("latest_goal")),
        }

    def record_goal(
        self,
        goal: dict,
        round_no: int | None = None,
        target_job: str = "",
        now: datetime | None = None,
    ) -> dict:
        """只更新顶层 ``latest_goal``，**不新增轮次、不改任务**。

        用在"结局已判定但不需要新计划"的场合（目标达成 / 依据不足）。
        ``round_no`` 省略时挂到当前最大轮次；没有轮次时拒绝写入 ——
        没有计划可归属的结局不持久化，避免刷新后凭空冒出一条"已达成"。
        """
        if not isinstance(goal, dict) or not goal.get("status"):
            raise LoopStoreError("goal 必须是一个带 status 的字典")

        loaded = self.load()
        if not loaded.writable:
            return {"status": WRITE_REFUSED, "warning": loaded.warning or "闭环记录当前不可安全写入",
                    "path": str(self.path)}

        rounds = loaded.data.get("rounds") or []
        if not rounds:
            return {"status": WRITE_REFUSED, "warning": "还没有任何轮次，结局无处归属，未写入",
                    "path": str(self.path)}

        if round_no is None:
            resolved = max(int(item.get("round") or 0) for item in rounds)
        else:
            problem = _round_no_error(round_no)
            if problem:
                return {"status": ROUND_INVALID, "warning": problem, "path": str(self.path)}
            resolved = round_no
            if not any(item.get("round") == resolved for item in rounds):
                return {"status": ROUND_UNKNOWN, "warning": f"第 {resolved} 轮不存在",
                        "path": str(self.path)}

        target = str(target_job or "").strip()
        if not target:
            target = next(
                (str(item.get("target_job") or "") for item in rounds
                 if item.get("round") == resolved),
                "",
            )

        data = _deepcopy(loaded.data)
        stamp = _stamp(now)
        data["latest_goal"] = _goal_record(goal, resolved, target, stamp)
        data["updated_at"] = stamp
        try:
            self._write(data)
        except OSError as exc:
            return self._failed(exc)
        return {
            "status": WRITE_OK,
            "path": str(self.path),
            "latest_goal": _deepcopy(data["latest_goal"]),
        }

    def set_pending_next_round(self, payload: dict, now: datetime | None = None) -> dict:
        """记下"这一轮还没存成计划"的恢复信息，供下次重试。

        调用方应在**工作流开始前**写一次（此时只有一个待办事件清单），
        工作流产完之后再把算好的计划补进同一个标记。任何失败都只返回状态，
        不抛 —— 恢复标记本身不能成为业务失败的放大器。
        """
        if not isinstance(payload, dict):
            raise LoopStoreError(f"pending_next_round 必须是字典，收到 {type(payload).__name__}")

        loaded = self.load()
        if not loaded.writable:
            return {"status": WRITE_REFUSED, "warning": loaded.warning or "闭环记录当前不可安全写入",
                    "path": str(self.path)}

        data = _deepcopy(loaded.data)
        stamp = _stamp(now)
        marker = _deepcopy(payload)
        marker["created_at"] = stamp
        data["pending_next_round"] = marker
        data["updated_at"] = stamp
        try:
            self._write(data)
        except OSError as exc:
            return self._failed(exc)
        return {"status": WRITE_OK, "path": str(self.path),
                "pending_next_round": _deepcopy(marker)}

    def clear_pending_next_round(self, now: datetime | None = None) -> dict:
        """清掉恢复标记；本来没有标记时**不写盘**。"""
        loaded = self.load()
        if not loaded.writable:
            return {"status": WRITE_REFUSED, "warning": loaded.warning or "闭环记录当前不可安全写入",
                    "path": str(self.path)}
        if not loaded.data.get("pending_next_round"):
            return {"status": "not_needed", "path": str(self.path)}

        data = _deepcopy(loaded.data)
        data.pop("pending_next_round", None)
        data["updated_at"] = _stamp(now)
        try:
            self._write(data)
        except OSError as exc:
            return self._failed(exc)
        return {"status": WRITE_OK, "path": str(self.path)}

    def mark_completed(
        self,
        task_id,
        evidence: str = "",
        round_no: int | None = None,
        now: datetime | None = None,
        evidence_kind: str = "",
        note: str = "",
    ) -> dict:
        """把一个任务标成已完成，记下完成时间、证据与心得。

        - **已完成的任务不会被重复登记**：再次调用直接返回
          :data:`COMPLETE_ALREADY`，**不写盘**，磁盘文件逐字节不变。
        - ``evidence`` 可以为空：空证据 + ``evidence_kind=self_report``
          表示"自述完成"，升级照规则走，但不算直接证据。
        - ``note`` 是用户心得，单独保存，**不并入 evidence**。
        - ``round_no`` 省略时在所有轮次里找；给了则只在该轮里找。
        - 返回 :data:`COMPLETE_OK` 之外的任何状态，磁盘文件都没有被改动。
        """
        if not isinstance(task_id, str) or not _TASK_ID_RE.match(task_id.strip()):
            return {
                "status": COMPLETE_INVALID_TASK_ID,
                "warning": f"任务 ID 不合法：{task_id!r}；"
                           f"格式应为 r<轮次三位以上>-<l|p><序号两位以上>，例如 r001-l01",
                "path": str(self.path),
            }
        task_id = task_id.strip()

        if evidence is None:
            evidence = ""
        if not isinstance(evidence, str):
            return {"status": COMPLETE_INVALID_EVIDENCE,
                    "warning": f"证据必须是字符串，收到 {type(evidence).__name__}",
                    "path": str(self.path)}

        stamp = _stamp(now)

        if round_no is not None:
            problem = _round_no_error(round_no)
            if problem:
                return {"status": ROUND_INVALID, "warning": problem, "path": str(self.path)}

        loaded = self.load()
        if not loaded.writable:
            return {"status": WRITE_REFUSED, "warning": loaded.warning or "闭环记录当前不可安全写入",
                    "path": str(self.path)}

        if round_no is not None and not any(
            r.get("round") == round_no for r in loaded.data.get("rounds") or []
        ):
            return {"status": ROUND_UNKNOWN,
                    "warning": f"第 {round_no} 轮不存在", "path": str(self.path)}

        data = _deepcopy(loaded.data)
        found = self._locate(data, task_id, round_no)
        if not found:
            return {"status": COMPLETE_UNKNOWN_TASK,
                    "warning": f"闭环记录里没有任务 {task_id}", "path": str(self.path)}

        _, task = found
        if task.get("status") == TASK_COMPLETED:
            return {
                "status": COMPLETE_ALREADY,
                "warning": f"任务 {task_id} 已于 {task.get('completed_at')} 完成，"
                           f"本次不重复登记（磁盘未改动）",
                "path": str(self.path),
                "task": _deepcopy(task),
                "written": False,
            }

        task["status"] = TASK_COMPLETED
        task["completed_at"] = stamp
        task["evidence"] = evidence
        kind_text = str(evidence_kind or "").strip()
        if kind_text not in (TASK_EVIDENCE_KIND_GROWTH, TASK_EVIDENCE_KIND_SELF_REPORT):
            kind_text = (TASK_EVIDENCE_KIND_GROWTH if evidence.strip()
                         else TASK_EVIDENCE_KIND_SELF_REPORT)
        task["evidence_kind"] = kind_text
        task["note"] = "" if note is None else str(note)
        data["updated_at"] = stamp

        try:
            self._write(data)
        except OSError as exc:
            return self._failed(exc)

        return {"status": COMPLETE_OK, "path": str(self.path), "round": task.get("round"),
                "task": _deepcopy(task), "written": True}

    def reopen(
        self,
        task_id,
        round_no: int | None = None,
        now: datetime | None = None,
    ) -> dict:
        """把一条**已完成**任务退回待完成，并清空完成时间与证据。

        只处理 ``loop.json``；是否已经计入成长历史由服务层先判定 —— 已入账的
        完成记录不能靠本方法撤回，否则成长历史会凭空多出一条无源记录。
        未完成的任务直接返回 :data:`REOPEN_NOT_COMPLETED`，不写盘。
        """
        if not isinstance(task_id, str) or not _TASK_ID_RE.match(task_id.strip()):
            return {
                "status": COMPLETE_INVALID_TASK_ID,
                "warning": f"任务 ID 不合法：{task_id!r}；"
                           f"格式应为 r<轮次三位以上>-<l|p><序号两位以上>，例如 r001-l01",
                "path": str(self.path),
            }
        task_id = task_id.strip()

        if round_no is not None:
            problem = _round_no_error(round_no)
            if problem:
                return {"status": ROUND_INVALID, "warning": problem, "path": str(self.path)}

        loaded = self.load()
        if not loaded.writable:
            return {"status": WRITE_REFUSED, "warning": loaded.warning or "闭环记录当前不可安全写入",
                    "path": str(self.path)}

        if round_no is not None and not any(
            r.get("round") == round_no for r in loaded.data.get("rounds") or []
        ):
            return {"status": ROUND_UNKNOWN,
                    "warning": f"第 {round_no} 轮不存在", "path": str(self.path)}

        data = _deepcopy(loaded.data)
        found = self._locate(data, task_id, round_no)
        if not found:
            return {"status": COMPLETE_UNKNOWN_TASK,
                    "warning": f"闭环记录里没有任务 {task_id}", "path": str(self.path)}

        _, task = found
        if task.get("status") != TASK_COMPLETED:
            return {
                "status": REOPEN_NOT_COMPLETED,
                "warning": f"任务 {task_id} 当前不是已完成状态，无需撤回",
                "path": str(self.path),
                "task": _deepcopy(task),
                "written": False,
            }

        stamp = _stamp(now)
        task["status"] = TASK_PENDING
        task["completed_at"] = None
        task["evidence"] = ""
        task["evidence_kind"] = ""
        task["note"] = ""
        data["updated_at"] = stamp

        try:
            self._write(data)
        except OSError as exc:
            return self._failed(exc)

        return {"status": REOPEN_OK, "path": str(self.path), "round": task.get("round"),
                "task": _deepcopy(task), "written": True}

    def attach_evidence(
        self,
        task_id,
        evidence: str,
        round_no: int | None = None,
        now: datetime | None = None,
        note: str | None = None,
    ) -> dict:
        """给一条**已完成**任务补上凭证，并把证据类型翻成 ``growth``。

        只改 ``loop.json``：成长历史里的补记由服务层在锁内另行追加，
        本方法绝不碰成长历史。任务尚未完成时返回
        :data:`ATTACH_NOT_COMPLETED`；同样的凭证已经记过时返回
        :data:`ATTACH_ALREADY`，两种情况都不写盘。
        """
        if not isinstance(task_id, str) or not _TASK_ID_RE.match(task_id.strip()):
            return {
                "status": COMPLETE_INVALID_TASK_ID,
                "warning": f"任务 ID 不合法：{task_id!r}；"
                           f"格式应为 r<轮次三位以上>-<l|p><序号两位以上>，例如 r001-l01",
                "path": str(self.path),
            }
        task_id = task_id.strip()

        if not isinstance(evidence, str):
            return {"status": COMPLETE_INVALID_EVIDENCE,
                    "warning": f"证据必须是字符串，收到 {type(evidence).__name__}",
                    "path": str(self.path)}
        evidence = evidence.strip()
        if not evidence:
            return {"status": COMPLETE_INVALID_EVIDENCE,
                    "warning": "补凭证不能为空", "path": str(self.path)}

        if round_no is not None:
            problem = _round_no_error(round_no)
            if problem:
                return {"status": ROUND_INVALID, "warning": problem, "path": str(self.path)}

        loaded = self.load()
        if not loaded.writable:
            return {"status": WRITE_REFUSED, "warning": loaded.warning or "闭环记录当前不可安全写入",
                    "path": str(self.path)}

        if round_no is not None and not any(
            r.get("round") == round_no for r in loaded.data.get("rounds") or []
        ):
            return {"status": ROUND_UNKNOWN,
                    "warning": f"第 {round_no} 轮不存在", "path": str(self.path)}

        data = _deepcopy(loaded.data)
        found = self._locate(data, task_id, round_no)
        if not found:
            return {"status": COMPLETE_UNKNOWN_TASK,
                    "warning": f"闭环记录里没有任务 {task_id}", "path": str(self.path)}

        _, task = found
        if task.get("status") != TASK_COMPLETED:
            return {
                "status": ATTACH_NOT_COMPLETED,
                "warning": f"任务 {task_id} 还没完成，不能补凭证",
                "path": str(self.path),
                "task": _deepcopy(task),
                "written": False,
            }

        already = (
            task.get("evidence_kind") == TASK_EVIDENCE_KIND_GROWTH
            and str(task.get("evidence") or "") == evidence
        )
        if already:
            return {
                "status": ATTACH_ALREADY,
                "warning": "这条凭证已经记过，不重复写入",
                "path": str(self.path),
                "task": _deepcopy(task),
                "written": False,
            }

        stamp = _stamp(now)
        task["evidence"] = evidence
        task["evidence_kind"] = TASK_EVIDENCE_KIND_GROWTH
        if note is not None:
            task["note"] = str(note)
        data["updated_at"] = stamp

        try:
            self._write(data)
        except OSError as exc:
            return self._failed(exc)

        return {"status": ATTACH_OK, "path": str(self.path), "round": task.get("round"),
                "task": _deepcopy(task), "written": True}

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
            try:
                self._write(_empty_data(self.user_id))
            except OSError as exc:
                result["status"] = "backed_up"
                result["warning"] = f"原文件已备份到 {target}，但重建空记录失败：{exc}"
                return result
            result["status"] = "reset_after_backup"
            result["warning"] = (f"原文件已备份到 {target}，随后重建了空的闭环记录；"
                                 f"旧进度请从备份文件中恢复")
        return result

    # ── 内部 ────────────────────────────────────────────────────────────
    def _locate(self, data: dict, task_id: str, round_no: int | None = None):
        """在 data 里找任务，返回 ``(轮次条目, 任务条目)``；找不到返回 ``None``。

        直接返回 data 内部的引用 —— 只有 :meth:`mark_completed` 用它，那里操作的是深拷贝。
        """
        for entry in data.get("rounds") or []:
            if round_no is not None and entry.get("round") != round_no:
                continue
            for task in entry.get("tasks") or []:
                if task.get("task_id") == task_id:
                    return entry, task
        return None

    def _failed(self, exc: OSError) -> dict:
        return {
            "status": WRITE_FAILED,
            "warning": f"闭环记录写入失败（{type(exc).__name__}: {exc}）；"
                       f"**原文件未改动**，本次登记未生效",
            "path": str(self.path),
            "written": False,
        }

    def _write(self, data: dict) -> None:
        """原子写：同目录临时文件 → fsync → ``os.replace``。

        失败时删掉临时文件并重新抛出，**不碰**目标文件。
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


def _structure_problem(data: dict) -> str | None:
    """检查 ``rounds`` 的结构。返回问题描述，没问题返回 ``None``。

    结构读不懂时**宁可整份判为损坏**：反正后面要拒绝写入，说清楚比猜要安全。
    """
    rounds = data.get("rounds")
    if not isinstance(rounds, list):
        return "缺 rounds 列表"
    for position, entry in enumerate(rounds, start=1):
        if not isinstance(entry, dict):
            return f"第 {position} 个轮次不是对象"
        number = entry.get("round")
        if isinstance(number, bool) or not isinstance(number, int):
            return f"第 {position} 个轮次的 round 不是整数"
        tasks = entry.get("tasks")
        if not isinstance(tasks, list):
            return f"第 {position} 个轮次缺 tasks 列表"
        for index, task in enumerate(tasks, start=1):
            if not isinstance(task, dict):
                return f"第 {position} 轮第 {index} 个任务不是对象"
    return None
