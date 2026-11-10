"""后台任务管理器：把长耗时的模型调用移出 HTTP 请求线程。

为什么默认必须是单工作线程
--------------------------
``runs/growth.json`` 与 ``runs/loop.json`` 的读-改-写只在**进程内**串行安全
（tests/test_readonly_boundary.py 明确写着"不承诺跨进程"）。而「下一轮规划」
会经由 ``growth_node`` 正式写入成长历史，两个此类任务并发就会互相覆盖；
分析链路还会临时改写 ``agents.growth_agent`` 的**进程级全局 store**。
因此 ``max_workers=1``，把所有重活串成一条队列 —— 队列是特性，不是缺陷。

两条上限（都默认关闭，由 backend.config 在生产入口注入）
--------------------------------------------------------
- ``max_records``：记录只存在内存里，一次分析产出的完整 state 不小。超上限时
  从最旧的开始丢，**只丢已终态**的记录；``queued`` / ``running`` 永不丢，
  否则 ``_execute`` 查不到自己的记录、前端也会拿着一个永远查不到的 run_id。
- ``max_pending``：同时在跑或排队的任务数上限。单 worker + 一次几分钟的推理，
  没有上限时双击"开始分析"会排出一串重复任务。满了就抛 ``QueueFull``，
  由 ``backend.app`` 的异常处理器翻成 HTTP 503。
"""

from __future__ import annotations

import contextvars
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Callable

__all__ = [
    "STATUS_QUEUED", "STATUS_RUNNING", "STATUS_SUCCEEDED", "STATUS_FAILED",
    "NODE_RUNNING", "NODE_DONE", "NODE_FAILED", "NODE_SKIPPED", "NODE_CANCELED",
    "TERMINAL_STATUSES", "TaskRecord", "TaskManager", "QueueFull",
    "current_run_canceled", "inside_task_run", "report_node_started",
    "report_node_finished", "report_node_skipped", "report_node_interrupted",
]

#: 当前线程正在执行的 (manager, run_id)。业务层在写盘前用它判断"这次运行
#: 是否已被用户停止" —— 停止不是杀掉线程，而是让迟到的结果一个字节都不落。
_CURRENT_RUN: contextvars.ContextVar[tuple["TaskManager", str] | None] = (
    contextvars.ContextVar("career_current_run", default=None)
)


def current_run_canceled() -> bool:
    """当前这一步是否属于一个已被用户停止的运行；不在后台任务里恒为 False。"""
    current = _CURRENT_RUN.get()
    return bool(current and current[0].is_canceled(current[1]))


def inside_task_run() -> bool:
    """当前线程是否正在执行一个后台任务（决定要不要回报节点进度）。"""
    return _CURRENT_RUN.get() is not None


def report_node_started(node: str) -> None:
    """回报"开始执行某个节点"；不在后台任务里是空操作（CLI / 单测用）。"""
    current = _CURRENT_RUN.get()
    if current is not None:
        current[0].node_started(current[1], node)


def report_node_finished(node: str, *, error: str | None = None) -> None:
    """回报"某个节点结束"；``error`` 非空表示该节点失败。"""
    current = _CURRENT_RUN.get()
    if current is not None:
        current[0].node_finished(current[1], node, error=error)


def report_node_skipped(node: str) -> None:
    """回报"某个节点这一步不会执行"（条件分支没走到）。"""
    current = _CURRENT_RUN.get()
    if current is not None:
        current[0].node_skipped(current[1], node)


def report_node_interrupted(reason: str) -> None:
    """把当前在跑的节点记为失败（流式图在没给结束事件就退出时用）。"""
    current = _CURRENT_RUN.get()
    if current is not None:
        current[0].node_interrupted(current[1], reason)

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"

#: 终态集合：查询接口据此判断"跑完了没有"。
TERMINAL_STATUSES = (STATUS_SUCCEEDED, STATUS_FAILED)

#: 节点级进度（``TaskRecord.progress`` 每一项的 status）。
NODE_RUNNING = "running"
NODE_DONE = "done"
NODE_FAILED = "failed"
NODE_SKIPPED = "skipped"
NODE_CANCELED = "canceled"


class QueueFull(RuntimeError):
    """在跑/排队的任务数已达上限，本次提交被拒绝（HTTP 层翻成 503）。"""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


@dataclass
class TaskRecord:
    """一次后台运行的记录（``POST`` 返回 run_id，``GET`` 用它查状态）。"""

    run_id: str
    kind: str
    status: str = STATUS_QUEUED
    #: 提交者的用户 id。``None`` 表示内部/测试直接提交的记录，不做归属过滤；
    #: HTTP 层提交的一定带值 —— 别的用户拿着这个 run_id 查询会得到 404。
    owner: int | None = None
    #: 该运行所属的求职档案。删除档案后记录一并作废；迟到结果不得再写回。
    plan_id: str | None = None
    #: 用户主动停止过。置位后执行体跑完也不回写成功，业务层写盘前同样要查。
    canceled: bool = False
    result: Any = None
    error: str | None = None
    #: 节点级进度，按节点首次出现的顺序追加。每项形如
    #: ``{"node", "status", "started_at", "finished_at", "duration_ms", "error"}``。
    #: 这是执行体自己回报的真实状态，不是按时间推算的假进度。
    progress: list[dict] = field(default_factory=list)
    #: 当前正在执行的节点名；没有在跑的节点时为 ``None``。
    current_node: str | None = None
    created_at: str = field(default_factory=_now)
    started_at: str | None = None
    finished_at: str | None = None

    @property
    def done(self) -> bool:
        return self.status in TERMINAL_STATUSES


class TaskManager:
    """极简后台任务表：提交 → 排队 → 串行执行 → 记录结果 / 错误。

    失败**不抛给调用方**（提交是异步的），而是把异常消息写进
    ``record.error``，由 ``GET /api/runs/{run_id}`` 如实回报。
    """

    def __init__(
        self,
        max_workers: int = 1,
        max_records: int | None = None,
        max_pending: int | None = None,
    ) -> None:
        """``max_records`` / ``max_pending`` 传 ``None`` 表示不限（单测与 CLI 用），
        生产入口由 ``backend.config`` 注入具体数值。"""
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="career-job"
        )
        self._lock = threading.Lock()
        self._records: dict[str, TaskRecord] = {}
        # 取消标记必须比记录活得久：记录可能因容量被清理，而执行体还在跑，
        # 写盘前那一问必须仍然得到 True。
        self._canceled: set[str] = set()
        self._max_records = max_records
        self._max_pending = max_pending

    # ── 提交 ────────────────────────────────────────────────────────────
    def submit(
        self, fn: Callable[..., Any], *args: Any, kind: str = "job",
        owner: int | None = None, plan_id: str | None = None, **kwargs: Any,
    ) -> str:
        """把 ``fn(*args, **kwargs)`` 排进队列，立刻返回 ``run_id``。

        ``owner``：提交者的用户 id（HTTP 层必传）；查询接口按它做归属过滤。
        ``plan_id``：档案归属；删除档案时用 :meth:`drop_plan` 让迟到结果失效。
        队列已满时抛 :class:`QueueFull`，**不会**创建记录、也不会占用线程池。
        """
        run_id = uuid.uuid4().hex
        with self._lock:
            self._reject_when_queue_full_locked()
            self._drop_oldest_finished_locked()
            self._records[run_id] = TaskRecord(
                run_id=run_id, kind=kind, owner=owner, plan_id=plan_id
            )
        self._executor.submit(self._execute, run_id, fn, args, kwargs)
        return run_id

    def _pending_count_locked(self) -> int:
        return sum(1 for record in self._records.values() if not record.done)

    def _reject_when_queue_full_locked(self) -> None:
        if self._max_pending is None:
            return
        pending = self._pending_count_locked()
        if pending >= self._max_pending:
            raise QueueFull(f"后台队列已满（{pending} 个任务在跑或排队），请稍后再试")

    def _drop_oldest_finished_locked(self) -> None:
        """超过记录上限时，按创建顺序从最旧开始丢**已终态**的记录。

        ``dict`` 保持插入顺序，所以"最旧"就是迭代顺序的前端。
        """
        if self._max_records is None:
            return
        for run_id, record in list(self._records.items()):
            if len(self._records) < self._max_records:
                break
            # canceled 的记录虽然已是终态，但执行体可能还在跑（取消不掐线程），
            # 记录一丢，写盘前的 is_canceled 就查不到了 —— 这类先留着。
            if record.done and run_id not in self._canceled:
                self._records.pop(run_id)

    def _execute(self, run_id: str, fn: Callable[..., Any], args: tuple, kwargs: dict) -> None:
        with self._lock:
            record = self._records.get(run_id)
            if record is None:  # 已被清理，不再执行
                self._canceled.discard(run_id)
                return
            if record.canceled:
                # 排队期间被停止：一行业务代码都不跑。
                self._canceled.discard(run_id)
                return
            record.status = STATUS_RUNNING
            record.started_at = _now()

        token = _CURRENT_RUN.set((self, run_id))
        try:
            result = fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - 业务失败不抛给调用方，写进 record 由查询接口回报
            with self._lock:
                record = self._records.get(run_id)
                if record is None or record.canceled:  # 已删除/已停止：丢弃迟到失败
                    return
                record.status = STATUS_FAILED
                record.error = f"{type(exc).__name__}: {exc}"
                record.finished_at = _now()
                self._close_all_running_locked(record, NODE_FAILED, record.error)
                record.current_node = None
            return
        except BaseException as exc:
            # SystemExit / KeyboardInterrupt / MemoryError 一类进程级信号：
            # 先如实落账（否则记录会永远停在 running），再原样抛出，不在这里吞掉。
            with self._lock:
                record = self._records.get(run_id)
                if record is None or record.canceled:
                    raise
                record.status = STATUS_FAILED
                record.error = f"进程级异常 {type(exc).__name__}: {exc}"
                record.finished_at = _now()
                self._close_all_running_locked(record, NODE_FAILED, record.error)
                record.current_node = None
            raise
        finally:
            _CURRENT_RUN.reset(token)
            with self._lock:
                self._canceled.discard(run_id)

        with self._lock:
            record = self._records.get(run_id)
            if record is None or record.canceled:  # 删除/停止竞态：结果只能丢弃
                return
            # 正常返回却还有节点停在 running，说明执行体漏报结束；不能让它
            # 一直显示"进行中"，但也绝不冒充完成，如实记为失败。
            self._close_all_running_locked(record, NODE_FAILED, "节点没有回报结束状态")
            record.current_node = None
            record.status = STATUS_SUCCEEDED
            record.result = result
            record.error = None
            record.finished_at = _now()

    # ── 查询 ────────────────────────────────────────────────────────────
    def get(self, run_id: str) -> TaskRecord | None:
        """取记录对象本身；不存在返回 ``None``（测试直接读 ``.status``）。"""
        with self._lock:
            return self._records.get(run_id)

    def snapshot(self, run_id: str, owner: int | None = None) -> dict | None:
        """取一份脱离锁的记录快照，供 HTTP 序列化。

        ``owner`` 传用户 id 时做归属过滤：**别人的 run_id 视同不存在**（返回
        ``None`` → 404），不泄露"存在与否"。``owner=None``（内部/测试直连）
        不过滤。``owner`` 为 ``None`` 的记录（内部提交）对任何人都可见。
        """
        with self._lock:
            record = self._records.get(run_id)
            if record is None:
                return None
            if owner is not None and record.owner is not None and record.owner != owner:
                return None
            return asdict(record)

    def is_canceled(self, run_id: str) -> bool:
        """该运行是否被用户停止过（执行体写盘前据此拒绝落盘）。"""
        with self._lock:
            return run_id in self._canceled

    # ── 节点级进度 ──────────────────────────────────────────────────────
    def node_started(self, run_id: str, node: str) -> None:
        """记下"开始执行 node"。记录已终态（停止/删除竞态）时直接忽略。"""
        with self._lock:
            record = self._records.get(run_id)
            if record is None or record.done:
                return
            record.progress.append({
                "node": node,
                "status": NODE_RUNNING,
                "started_at": _now(),
                "finished_at": None,
                "duration_ms": None,
                "error": None,
            })
            record.current_node = node

    def node_finished(self, run_id: str, node: str, *, error: str | None = None) -> None:
        """记下 node 结束；``error`` 非空记为 failed。"""
        with self._lock:
            record = self._records.get(run_id)
            if record is None or record.done:
                return
            entry = self._open_entry_locked(record, node)
            self._close_entry_locked(
                entry, NODE_FAILED if error else NODE_DONE, error
            )
            self._refresh_current_node_locked(record)

    def node_skipped(self, run_id: str, node: str) -> None:
        """记下 node 这次不会执行（条件分支没走到）。"""
        with self._lock:
            record = self._records.get(run_id)
            if record is None or record.done:
                return
            record.progress.append({
                "node": node,
                "status": NODE_SKIPPED,
                "started_at": None,
                "finished_at": _now(),
                "duration_ms": None,
                "error": None,
            })

    def node_interrupted(self, run_id: str, reason: str) -> None:
        """把该运行里所有还在 running 的节点记为 failed（流式图异常退出的兜底）。"""
        with self._lock:
            record = self._records.get(run_id)
            if record is None or record.done:
                return
            self._close_all_running_locked(record, NODE_FAILED, reason)
            record.current_node = None

    @staticmethod
    def _open_entry_locked(record: TaskRecord, node: str) -> dict:
        for entry in reversed(record.progress):
            if entry["node"] == node and entry["status"] == NODE_RUNNING:
                return entry
        # 没收到 started 就收到 finished：补一条，started_at 留空，不假装有耗时。
        entry = {
            "node": node,
            "status": NODE_RUNNING,
            "started_at": None,
            "finished_at": None,
            "duration_ms": None,
            "error": None,
        }
        record.progress.append(entry)
        return entry

    @staticmethod
    def _close_entry_locked(entry: dict, status: str, error: str | None) -> None:
        entry["status"] = status
        entry["finished_at"] = _now()
        entry["error"] = error
        if entry["started_at"]:
            try:
                started = datetime.fromisoformat(entry["started_at"])
                finished = datetime.fromisoformat(entry["finished_at"])
                entry["duration_ms"] = max(
                    0, int((finished - started).total_seconds() * 1000)
                )
            except ValueError:
                entry["duration_ms"] = None

    @staticmethod
    def _refresh_current_node_locked(record: TaskRecord) -> None:
        running = [e for e in record.progress if e["status"] == NODE_RUNNING]
        record.current_node = running[-1]["node"] if running else None

    def _close_all_running_locked(
        self, record: TaskRecord, status: str, error: str | None
    ) -> None:
        for entry in record.progress:
            if entry["status"] == NODE_RUNNING:
                self._close_entry_locked(entry, status, error)

    def cancel_run(
        self, run_id: str, *, plan_id: str | None = None, owner: int | None = None,
    ) -> dict:
        """停止一次尚未结束的运行。**不删任何业务数据。**

        排队中的任务直接作废、不再执行；正在跑的任务无法掐断模型调用，但记录
        立刻变终态，且业务层每次写盘前都会 :func:`current_run_canceled` 复核，
        结果只会被丢弃。返回::

            {"canceled": bool, "status": 原状态或当前终态, "reason":
             "ok" | "finished" | "not_found"}

        归属过滤与查询接口一致：不是这份档案、或不是本人的运行，一律 not_found。
        """
        with self._lock:
            record = self._records.get(run_id)
            if record is None:
                return {"canceled": False, "status": None, "reason": "not_found"}
            if plan_id is not None and record.plan_id != plan_id:
                return {"canceled": False, "status": None, "reason": "not_found"}
            if owner is not None and record.owner is not None and record.owner != owner:
                return {"canceled": False, "status": None, "reason": "not_found"}
            if record.done:
                return {"canceled": False, "status": record.status, "reason": "finished"}
            previous = record.status
            record.canceled = True
            record.status = STATUS_FAILED
            record.error = "分析已被用户停止：本次结果没有保存"
            record.finished_at = _now()
            self._close_all_running_locked(record, NODE_CANCELED, None)
            record.current_node = None
            self._canceled.add(run_id)
            return {"canceled": True, "status": previous, "reason": "ok"}

    def drop_plan(self, plan_id: str) -> int:
        """删除某份档案的全部运行记录，返回删除条数。

        正在执行的任务不会被强制终止（模型调用无法安全中断），但
        :meth:`_execute` 在每个落账点都会重新查记录；记录已不在就丢弃结果。
        业务层写入前还会再校验档案存在性，形成双重防护。
        """
        with self._lock:
            doomed = [
                run_id for run_id, record in self._records.items()
                if record.plan_id == plan_id
            ]
            for run_id in doomed:
                # 不清理 _canceled：执行体可能仍在跑，它的写盘复核还要用。
                self._records.pop(run_id, None)
            return len(doomed)

    def shutdown(self, wait: bool = True, cancel_futures: bool = False) -> None:
        """关闭线程池。

        ``cancel_futures=True`` 时排队中的任务不再执行。先关线程池、后落账：
        正在跑的任务只可能变成终态，标 ``STATUS_FAILED`` 只命中仍停在
        ``queued`` 的那些，不会覆盖真实结果。
        """
        self._executor.shutdown(wait=wait, cancel_futures=cancel_futures)
        if cancel_futures:
            with self._lock:
                for record in self._records.values():
                    if record.status == STATUS_QUEUED:
                        record.status = STATUS_FAILED
                        record.error = "服务已停止，任务未开始执行"
                        record.finished_at = _now()
