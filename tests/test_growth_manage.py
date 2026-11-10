"""阶段 5.4：用户管理自己的成长记录。

覆盖两条**不删除原始数据**的更正路径：

1. 撤回待处理成果（``POST /api/growth/pending/withdraw``）
   - 任务退回未完成、完成证据清空；
   - 已入账的成果拒绝撤回（必须走作废）；
   - 未完成 / 不存在的任务给出可读拒绝，不写盘。

2. 作废已入账成果（``POST /api/growth/history/void``）
   - 追加 ``kind="adjustment"`` 调整记录，原条目一个字段都不改；
   - 按剩余成果重放，能力等级正确回退；
   - 非最新条目也能作废，后续条目继续生效；
   - 轮次之间的画像外部变化（非成果带来）不会因作废丢失；
   - 重复作废 / 作废调整记录本身被拒绝；
   - 作废后的指纹仍算"已处理"，下一轮不会自动重新排队。

隔离：每个用例用临时目录构造 LoopStore / GrowthStore，不碰项目 ``runs/``，
不调用任何模型。技能选「木工 / 编织 / 插花」这类知识库未收录项，让升级规则
只走确定性路径（未收录 → 跳过前置门禁、照常升一档）。
"""

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from agents.growth_adjust import GrowthAdjustError, void_event  # noqa: E402
from agents.growth_loop import pending_growth_events  # noqa: E402
from agents.growth_rules import event_fingerprint, normalize_skills  # noqa: E402
from agents.growth_store import ENTRY_KIND_ADJUSTMENT, GrowthStore, WRITE_OK  # noqa: E402
from agents.loop_store import (  # noqa: E402
    REOPEN_NOT_COMPLETED,
    REOPEN_OK,
    LoopStore,
)
from agents.skill_dict import SkillVocabulary  # noqa: E402
from backend.app import app  # noqa: E402
from backend.deps import create_stores  # noqa: E402
from backend.services import Stores  # noqa: E402

_VOCAB = SkillVocabulary()


def _event(title, skill, kind="learning", evidence="完成了这条成果"):
    canonical, _ = normalize_skills([skill], _VOCAB)
    return {
        "type": kind,
        "title": title,
        "skills": canonical,
        "evidence": evidence,
        "fingerprint": event_fingerprint(kind, title, canonical),
    }


def _entry(before, after, events, target_job="测试岗位"):
    return {
        "version": "v1",
        "target_job": target_job,
        "user_skills_before": before,
        "user_skills_after": after,
        "events": events,
        "level_changes": [],
        "blocked_upgrades": [],
        "knowledge_base_status": "ok",
    }


def _levels(skills):
    return {item["skill"]: item.get("level") for item in skills or []}


def _seed_loop(loop, topic="木工", skill="木工", target_job="测试岗位"):
    learning = [{
        "topic": topic, "covers": [skill], "description": "d",
        "stage": 1, "duration": "1周", "prerequisites": [],
        "method": "学习", "assign": "自己", "assessment": "可实现", "goal": "掌握",
        "status_v2": "pending",
        "resources": [], "priority": "高", "milestones": [], "deliverables": [],
    }]
    res = loop.append_round(target_job=target_job, learning_plan=learning,
                            project_plan=[], round_no=1)
    assert res["status"] == "written"
    return loop.load().data["rounds"][0]["tasks"][0]


class GrowthVoidUnitTest(unittest.TestCase):
    """作废的存储与重算语义（不经 HTTP）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = GrowthStore(directory=self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _append(self, entry):
        res = self.store.append(entry)
        self.assertEqual(res["status"], WRITE_OK)

    def test_void_keeps_original_entry_and_appends_adjustment(self):
        event = _event("木工入门", "木工")
        self._append(_entry([], [{"skill": "木工", "level": "入门"}], [event]))
        before = copy.deepcopy(self.store.load().data["history"][0])

        result = void_event(self.store, 0, 0, "当时证据填错了")

        self.assertTrue(result["voided"])
        history = self.store.load().data["history"]
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0], before, "原条目必须一个字段都不改")
        adjustment = history[1]
        self.assertEqual(adjustment["kind"], ENTRY_KIND_ADJUSTMENT)
        self.assertEqual(adjustment["action"], "void")
        self.assertEqual(adjustment["reason"], "当时证据填错了")
        self.assertEqual(adjustment["voided"][0]["title"], "木工入门")
        self.assertEqual(adjustment["voided"][0]["entry_index"], 0)
        self.assertEqual(adjustment["voided"][0]["event_index"], 0)
        self.assertEqual(_levels(adjustment["user_skills_after"]), {})

        change = next(c for c in result["level_changes"] if c["skill"] == "木工")
        self.assertEqual(change["from"], "入门")
        self.assertIsNone(change["to"])

    def test_void_one_of_two_events_keeps_the_other(self):
        events = [_event("木工入门", "木工"), _event("编织入门", "编织")]
        self._append(_entry(
            [],
            [{"skill": "木工", "level": "入门"}, {"skill": "编织", "level": "入门"}],
            events,
        ))

        result = void_event(self.store, 0, 0, "木工这条不成立")

        self.assertEqual(_levels(result["latest_skills"]), {"编织": "入门"})
        self.assertEqual(result["recomputed"]["events_replayed"], 1)
        changed = {c["skill"] for c in result["level_changes"]}
        self.assertEqual(changed, {"木工"})

    def test_void_older_entry_replays_later_entries(self):
        first = _entry([], [{"skill": "木工", "level": "入门"}], [_event("木工入门", "木工")])
        second = _entry(
            [{"skill": "木工", "level": "入门"}],
            [{"skill": "木工", "level": "入门"}, {"skill": "编织", "level": "入门"}],
            [_event("编织入门", "编织")],
        )
        self._append(first)
        self._append(second)

        result = void_event(self.store, 0, 0, "第一条作废")

        # 从第一条之前的状态重放：木工消失，第二条的编织照常恢复
        self.assertEqual(_levels(result["latest_skills"]), {"编织": "入门"})

    def test_profile_delta_between_rounds_survives_void(self):
        first = _entry([], [{"skill": "木工", "level": "入门"}], [_event("木工入门", "木工")])
        # 插花是两轮之间由画像更新带来的能力（没有任何成长事件），第二轮 before 才有
        second = _entry(
            [{"skill": "木工", "level": "入门"}, {"skill": "插花", "level": "入门"}],
            [
                {"skill": "木工", "level": "入门"},
                {"skill": "插花", "level": "入门"},
                {"skill": "编织", "level": "入门"},
            ],
            [_event("编织入门", "编织")],
        )
        self._append(first)
        self._append(second)

        result = void_event(self.store, 0, 0, "木工作废")

        self.assertEqual(
            _levels(result["latest_skills"]),
            {"插花": "入门", "编织": "入门"},
            "画像带来、没有成长事件的能力不能因作废而丢失",
        )

    def test_double_void_and_adjustment_void_refused_without_writing(self):
        self._append(_entry([], [{"skill": "木工", "level": "入门"}], [_event("木工入门", "木工")]))
        void_event(self.store, 0, 0, "第一次作废")
        history_before = copy.deepcopy(self.store.load().data["history"])

        with self.assertRaises(GrowthAdjustError):
            void_event(self.store, 0, 0, "重复作废")
        with self.assertRaises(GrowthAdjustError):
            void_event(self.store, 1, 0, "调整记录不能再作废")

        self.assertEqual(self.store.load().data["history"], history_before,
                         "失败的作废不得写盘")

    def test_missing_snapshot_refused_without_writing(self):
        broken = _entry([], [{"skill": "木工", "level": "入门"}], [_event("木工入门", "木工")])
        broken.pop("user_skills_after")
        self._append(broken)
        raw_before = self.store.path.read_text(encoding="utf-8")

        with self.assertRaises(GrowthAdjustError):
            void_event(self.store, 0, 0, "应当拒绝")

        self.assertEqual(self.store.path.read_text(encoding="utf-8"), raw_before)

    def test_voided_fingerprint_still_seen_and_not_pending(self):
        event = _event("木工", "木工")
        self._append(_entry([], [{"skill": "木工", "level": "入门"}], [event]))
        loop = LoopStore(directory=self._tmp.name)
        task = _seed_loop(loop, topic="木工", skill="木工")
        loop.mark_completed(task["task_id"], evidence="完成了这条成果")

        # 这条成果已经入账，所以作废前后都不在待处理里；关键是作废后**仍然不在**，
        # 否则用户作废完一生成下一轮，同一条成果会自己排队又升回去。
        before = pending_growth_events(loop_store=loop, growth_store=self.store)
        self.assertEqual(before["events"], [])

        void_event(self.store, 0, 0, "作废")

        self.assertIn(event["fingerprint"], self.store.seen_fingerprints())
        after = pending_growth_events(loop_store=loop, growth_store=self.store)
        self.assertEqual(after["events"], [],
                         "作废后的成果不能被下一轮自动重新入账")

    def test_corrupt_store_refused(self):
        path = Path(self._tmp.name) / "growth.json"
        path.write_text("{ broken json", encoding="utf-8")
        with self.assertRaises(GrowthAdjustError):
            void_event(self.store, 0, 0, "应当拒绝")
        self.assertEqual(path.read_text(encoding="utf-8"), "{ broken json")


class GrowthVoidApiTest(unittest.TestCase):
    """HTTP 层：作废接口与历史标注。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._loop = LoopStore(directory=self._tmp.name)
        self._growth = GrowthStore(directory=self._tmp.name)
        self._stores = Stores(growth=self._growth, loop=self._loop)
        app.dependency_overrides[create_stores] = lambda: self._stores
        self.client = TestClient(app)
        self._growth.append(_entry(
            [], [{"skill": "木工", "level": "入门"}], [_event("木工入门", "木工")]
        ))

    def tearDown(self):
        app.dependency_overrides.pop(create_stores, None)
        self._tmp.cleanup()

    def test_void_endpoint_and_history_annotation(self):
        resp = self.client.post("/api/growth/history/void", json={
            "entry_index": 0,
            "event_index": 0,
            "reason": "证据填错了",
        })
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["voided"])
        self.assertEqual(body["title"], "木工入门")

        history = self.client.get("/api/growth/history").json()
        self.assertEqual(history["confirmed_count"], 1, "调整记录不算一次确认")
        self.assertEqual(history["voided_count"], 1)
        self.assertEqual(history["history"][0]["voided_event_indices"], [0])
        self.assertEqual(history["history"][1]["kind"], "adjustment")
        self.assertEqual(history["history"][1]["reason"], "证据填错了")
        self.assertEqual(history["latest_skills"], [])

    def test_void_invalid_target_returns_422_with_reason(self):
        cases = [
            {"entry_index": 9, "event_index": 0, "reason": "没有这条记录"},
            {"entry_index": 0, "event_index": 9, "reason": "没有这条成果"},
        ]
        for payload in cases:
            resp = self.client.post("/api/growth/history/void", json=payload)
            self.assertEqual(resp.status_code, 422)
            self.assertFalse(resp.json()["detail"]["voided"])
            self.assertTrue(resp.json()["detail"]["reason"])

    def test_void_short_reason_rejected_by_schema(self):
        resp = self.client.post("/api/growth/history/void", json={
            "entry_index": 0, "event_index": 0, "reason": "错",
        })
        self.assertEqual(resp.status_code, 422)
        self.assertEqual(len(self._growth.load().data["history"]), 1, "校验失败不得写盘")


class ReopenUnitTest(unittest.TestCase):
    """LoopStore.reopen 的边界。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.loop = LoopStore(directory=self._tmp.name)
        self.task = _seed_loop(self.loop)

    def tearDown(self):
        self._tmp.cleanup()

    def test_reopen_completed_task_clears_evidence(self):
        self.loop.mark_completed(self.task["task_id"], evidence="原证据")
        result = self.loop.reopen(self.task["task_id"])
        self.assertEqual(result["status"], REOPEN_OK)
        task = self.loop.find_task(self.task["task_id"])
        self.assertEqual(task["status"], "pending")
        self.assertIsNone(task["completed_at"])
        self.assertEqual(task["evidence"], "")

    def test_reopen_pending_task_does_not_write(self):
        before = self.loop.path.read_bytes()
        result = self.loop.reopen(self.task["task_id"])
        self.assertEqual(result["status"], REOPEN_NOT_COMPLETED)
        self.assertEqual(self.loop.path.read_bytes(), before)

    def test_reopen_unknown_task_refused(self):
        result = self.loop.reopen("r001-l99")
        self.assertNotEqual(result["status"], REOPEN_OK)
        self.assertIn("没有任务", result["warning"])


class WithdrawPendingApiTest(unittest.TestCase):
    """HTTP 层：撤回待处理成果。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._loop = LoopStore(directory=self._tmp.name)
        self._growth = GrowthStore(directory=self._tmp.name)
        self._stores = Stores(growth=self._growth, loop=self._loop)
        app.dependency_overrides[create_stores] = lambda: self._stores
        self.client = TestClient(app)
        self.task = _seed_loop(self._loop, topic="红黑树", skill="Python")

    def tearDown(self):
        app.dependency_overrides.pop(create_stores, None)
        self._tmp.cleanup()

    def test_withdraw_reopens_task_and_removes_pending(self):
        self._loop.mark_completed(self.task["task_id"], evidence="完成证据")
        self.assertEqual(len(self.client.get("/api/growth/pending").json()["events"]), 1)

        resp = self.client.post("/api/growth/pending/withdraw",
                                json={"task_id": self.task["task_id"]})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["withdrawn"])

        task = self._loop.find_task(self.task["task_id"])
        self.assertEqual(task["status"], "pending")
        self.assertIsNone(task["completed_at"])
        self.assertEqual(task["evidence"], "")
        self.assertEqual(self.client.get("/api/growth/pending").json()["events"], [])

    def test_withdraw_recorded_event_refused(self):
        self._loop.mark_completed(self.task["task_id"], evidence="完成证据")
        canonical, _ = normalize_skills(["Python"], _VOCAB)
        fingerprint = event_fingerprint("learning", "红黑树", canonical)
        self._growth.append(_entry(
            [], [{"skill": "Python", "level": "入门"}],
            [{
                "type": "learning", "title": "红黑树", "skills": canonical,
                "evidence": "完成证据", "fingerprint": fingerprint,
            }],
        ))

        resp = self.client.post("/api/growth/pending/withdraw",
                                json={"task_id": self.task["task_id"]})
        self.assertEqual(resp.status_code, 422)
        detail = resp.json()["detail"]
        self.assertFalse(detail["withdrawn"])
        self.assertIn("作废", detail["reason"], "已入账的成果应引导用户去历史里作废")
        self.assertEqual(self._loop.find_task(self.task["task_id"])["status"], "completed")

    def test_withdraw_not_completed_task_refused(self):
        resp = self.client.post("/api/growth/pending/withdraw",
                                json={"task_id": self.task["task_id"]})
        self.assertEqual(resp.status_code, 422)
        self.assertFalse(resp.json()["detail"]["withdrawn"])

    def test_withdraw_unknown_task_refused(self):
        resp = self.client.post("/api/growth/pending/withdraw",
                                json={"task_id": "r001-l99"})
        self.assertEqual(resp.status_code, 422)
        self.assertFalse(resp.json()["detail"]["withdrawn"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
