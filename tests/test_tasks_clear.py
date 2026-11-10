"""「清空任务清单」的隔离测试：``LoopStore.clear()`` 与 ``services.clear_tasks()``。

覆盖
----
- ``clear()`` **先备份再重置**：备份文件与原清单逐字节相同，重置后 ``rounds`` 为空。
- 本来就没有轮次时不做"备份了一个空文件"的假动作（``backup=None``、不新造文件）。
- 损坏 / 版本不符的文件**拒绝**清空：磁盘字节不变、一个备份都不产生。
- 备份失败 → ``write_failed`` 且原文件未动；备份成功但重写失败 → 内容仍完整地留在备份里。
- ``clear_tasks``：有**还没进成长历史**的成果时默认 ``needs_confirmation`` 且什么都不删；
  只有显式 ``drop_pending_events=True`` 才真清；两种情况下 ``growth.json`` 都字节不变。

隔离
----
全部用 ``tempfile`` 临时目录构造 store，不触碰项目 ``runs/``，不调用 Ollama，
也不经过 ``backend.deps.create_stores`` 的真实用户目录。
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agents.growth_store import GrowthStore  # noqa: E402
from agents.loop_store import (  # noqa: E402
    WRITE_FAILED,
    WRITE_OK,
    WRITE_REFUSED,
    LoopStore,
)
from agents import loop_store as loop_store_module  # noqa: E402
from backend.services import (  # noqa: E402
    CLEAR_NEEDS_CONFIRM,
    CLEAR_OK,
    CLEAR_REFUSED,
    Stores,
    clear_tasks,
)


def _seed_rounds(loop: LoopStore, rounds: int = 2) -> list[dict]:
    """写 ``rounds`` 轮、每轮 2 条任务，返回所有任务 dict。"""
    tasks: list[dict] = []
    for round_no in range(1, rounds + 1):
        learning = [{
            "topic": f"红黑树第{round_no}轮", "covers": ["Python"], "description": "d",
            "stage": 1, "duration": "1周", "prerequisites": [],
            "method": "学习", "assign": "自己", "assessment": "可实现", "goal": "掌握",
            "status_v2": "pending", "resources": [], "priority": "高",
            "milestones": [], "deliverables": [],
        }]
        project = [{
            "name": f"任务系统第{round_no}轮", "covers_gaps": ["Python"],
            "description": "做一个任务闭环", "deliverables": ["可运行的仓库"],
        }]
        res = loop.append_round(target_job="软件工程师", learning_plan=learning,
                                project_plan=project, round_no=round_no)
        assert res["status"] == "written", res
        tasks.extend(loop.load().data["rounds"][-1]["tasks"])
    return tasks


def _complete(loop: LoopStore, task: dict, evidence: str = "已完成并跑通测试") -> None:
    res = loop.mark_completed(task["task_id"], evidence=evidence)
    assert res["status"] == "completed", res


class LoopStoreClearTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.loop = LoopStore(directory=self._tmp.name, user_id="user_1")
        self.path = self.loop.path

    def tearDown(self):
        self._tmp.cleanup()

    def test_clear_backups_then_resets(self):
        _seed_rounds(self.loop, rounds=2)
        original = self.path.read_bytes()

        result = self.loop.clear()

        self.assertEqual(result["status"], WRITE_OK)
        self.assertEqual(result["rounds_removed"], 2)
        self.assertEqual(result["tasks_removed"], 4)
        self.assertIsNotNone(result["backup"])

        backup = Path(result["backup"])
        self.assertTrue(backup.exists(), "清空必须先留下备份")
        self.assertEqual(backup.read_bytes(), original, "备份要与原清单逐字节相同")
        self.assertTrue(backup.name.startswith("loop.json.cleared-"))

        data = self.loop.load().data
        self.assertEqual(data["rounds"], [])
        self.assertEqual(self.loop.load().status, "ok")
        self.assertEqual(data["user_id"], "user_1")
        self.assertTrue(data["created_at"] and data["updated_at"])

    def test_backup_keeps_completed_evidence_readable(self):
        """备份不是半成品：把它读回来，轮次、任务与完成证据都还在。"""
        tasks = _seed_rounds(self.loop)
        _complete(self.loop, tasks[0])

        result = self.loop.clear()

        backup = LoopStore(directory=self._tmp.name, user_id="user_1",
                           filename=Path(result["backup"]).name)
        loaded = backup.load()
        self.assertEqual(loaded.status, "ok")
        self.assertEqual(len(loaded.data["rounds"]), 2)
        completed = [t for r in loaded.data["rounds"] for t in r["tasks"]
                     if t["status"] == "completed"]
        self.assertEqual(len(completed), 1)
        self.assertTrue(completed[0]["evidence"])

    def test_clear_without_rounds_makes_no_files(self):
        result = self.loop.clear()

        self.assertEqual(result["status"], WRITE_OK)
        self.assertEqual(result["rounds_removed"], 0)
        self.assertIsNone(result["backup"])
        self.assertFalse(self.path.exists(), "空清单不该被清出一个新文件")
        self.assertEqual(list(Path(self._tmp.name).iterdir()), [])

    def test_clear_on_rounds_free_file_makes_no_backup(self):
        self.path.write_text(json.dumps({"schema_version": 1, "user_id": "user_1",
                                         "rounds": []}), encoding="utf-8")
        before = self.path.read_bytes()

        result = self.loop.clear()

        self.assertEqual(result["status"], WRITE_OK)
        self.assertIsNone(result["backup"])
        self.assertEqual(self.path.read_bytes(), before)

    def _assert_refused(self, payload: str):
        self.path.write_text(payload, encoding="utf-8")
        before = self.path.read_bytes()

        result = self.loop.clear()

        self.assertEqual(result["status"], WRITE_REFUSED)
        self.assertTrue(result["warning"])
        self.assertEqual(self.path.read_bytes(), before, "拒绝清空时一个字节都不许动")
        self.assertEqual([p.name for p in Path(self._tmp.name).iterdir()], ["loop.json"],
                         "拒绝时不该留下备份")

    def test_clear_refuses_corrupt(self):
        self._assert_refused("{这不是 JSON")

    def test_clear_refuses_schema_mismatch(self):
        self._assert_refused(json.dumps({"schema_version": 99, "user_id": "user_1", "rounds": []}))

    def test_backup_failure_leaves_store_untouched(self):
        _seed_rounds(self.loop)
        before = self.path.read_bytes()

        with mock.patch.object(loop_store_module.shutil, "copy2",
                               side_effect=OSError("目标盘满了")):
            result = self.loop.clear()

        self.assertEqual(result["status"], WRITE_FAILED)
        self.assertIsNone(result["backup"])
        self.assertEqual(result["rounds_removed"], 0)
        self.assertIn("备份失败", result["warning"])
        self.assertEqual(self.path.read_bytes(), before, "备份失败就不该继续清空")
        self.assertEqual(len(self.loop.load().data["rounds"]), 2)

    def test_rewrite_failure_still_has_backup(self):
        _seed_rounds(self.loop)
        before = self.path.read_bytes()

        with mock.patch.object(LoopStore, "_write", side_effect=OSError("写不进去")):
            result = self.loop.clear()

        self.assertEqual(result["status"], WRITE_FAILED)
        backup = Path(result["backup"])
        self.assertTrue(backup.exists())
        self.assertEqual(backup.read_bytes(), before,
                         "重写失败时内容必须完整地留在备份里")
        self.assertEqual(self.path.read_bytes(), before)


class ClearTasksServiceTest(unittest.TestCase):
    """服务层：未记账成果的闸门 + 成长历史绝不受影响。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        directory = self._tmp.name
        self.loop = LoopStore(directory=directory, user_id="user_1")
        self.growth = GrowthStore(directory=directory, user_id="user_1")
        self.stores = Stores(growth=self.growth, loop=self.loop)

    def tearDown(self):
        self._tmp.cleanup()

    def test_no_pending_events_clears_directly(self):
        _seed_rounds(self.loop)

        result = clear_tasks(self.stores)

        self.assertEqual(result["status"], CLEAR_OK)
        self.assertEqual(result["rounds_removed"], 2)
        self.assertEqual(result["tasks_removed"], 4)
        self.assertEqual(result["pending_events"], 0)
        self.assertEqual(self.loop.load().data["rounds"], [])

    def test_completed_tasks_require_confirmation(self):
        tasks = _seed_rounds(self.loop)
        _complete(self.loop, tasks[0])
        _complete(self.loop, tasks[2])
        loop_before = self.loop.path.read_bytes()

        result = clear_tasks(self.stores)

        self.assertEqual(result["status"], CLEAR_NEEDS_CONFIRM)
        self.assertEqual(result["pending_events"], 2)
        self.assertEqual(result["rounds_removed"], 0)
        self.assertIsNone(result["backup"])
        self.assertEqual(self.loop.path.read_bytes(), loop_before,
                         "需要确认时必须什么都没删")
        self.assertFalse(self.growth.path.exists())

    def test_confirmed_drop_clears_and_leaves_growth_history(self):
        tasks = _seed_rounds(self.loop)
        _complete(self.loop, tasks[0])
        # 已经正式记账的那条：写进成长历史，清空清单不该动它
        self.growth.append({
            "version": "v1",
            "events": [{"type": "learning", "title": "已入账", "skills": ["Python"],
                        "evidence": "历史证据"}],
            "user_skills_after": [{"skill": "Python", "level": "中级"}],
        })
        growth_before = self.growth.path.read_bytes()

        result = clear_tasks(self.stores, drop_pending_events=True)

        self.assertEqual(result["status"], CLEAR_OK)
        self.assertEqual(result["rounds_removed"], 2)
        self.assertEqual(result["pending_events"], 1)
        self.assertEqual(self.loop.load().data["rounds"], [])
        self.assertEqual(self.growth.path.read_bytes(), growth_before,
                         "清空任务清单不许碰成长历史")
        self.assertTrue(Path(result["backup"]).exists())

    def test_refused_when_loop_store_corrupt(self):
        self.loop.path.write_text("{坏了", encoding="utf-8")

        result = clear_tasks(self.stores, drop_pending_events=True)

        self.assertEqual(result["status"], CLEAR_REFUSED)
        self.assertEqual(result["rounds_removed"], 0)
        self.assertTrue(result["warning"])
        self.assertEqual(self.loop.path.read_text(encoding="utf-8"), "{坏了")


if __name__ == "__main__":
    unittest.main()
