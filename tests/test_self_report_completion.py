"""一键完成：自述完成 vs 附凭证完成。

规则：
- 点提交即可完成，不再强制输入证据；
- 没附凭证 → evidence_kind=self_report，技能照规则升级，但评估只算"未验证"；
- 附了凭证 → evidence_kind=growth，才算直接证据；
- 心得单独存，不并入证据，也不当凭证；
- 完成后再补凭证，追加一条 verification 记录覆盖证据类型，原历史一条不改。

隔离：全部使用临时目录 + dependency_overrides，不碰项目 runs/，不调用模型。
"""

import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from agents.growth_rules import apply_upgrades, event_fingerprint, validate_event  # noqa: E402
from agents.growth_store import GrowthStore  # noqa: E402
from agents.loop_store import LoopStore  # noqa: E402
from backend.app import app  # noqa: E402
from backend.deps import create_stores  # noqa: E402
from backend.services import Stores  # noqa: E402


def _seed_loop(loop: LoopStore, title: str = "数据结构") -> str:
    learning = [
        {"topic": title, "covers": ["Python"], "stage": 1,
         "goal": "掌握", "completion_criteria": "能独立实现"},
    ]
    res = loop.append_round(target_job="软件工程师", learning_plan=learning,
                            project_plan=[], round_no=1)
    assert res["status"] == "written"
    return loop.load().data["rounds"][0]["tasks"][0]["task_id"]


def _task(loop: LoopStore, task_id: str) -> dict:
    return next(
        t for r in loop.load().data["rounds"] for t in r["tasks"]
        if t["task_id"] == task_id
    )


def _append_self_report_history(growth: GrowthStore, title: str = "数据结构") -> str:
    """模拟下一轮 growth_node 已经把自述完成记进成长历史。"""
    fingerprint = event_fingerprint("learning", title, ["Python"])
    entry = {
        "events": [{
            "type": "learning",
            "title": title,
            "skills": ["Python"],
            "evidence": "",
            "evidence_kind": "self_report",
            "fingerprint": fingerprint,
        }],
        "user_skills_before": [],
        "user_skills_after": [{
            "skill": "Python", "level": "初级", "evidence": "",
            "evidence_kind": "self_report", "evidence_ref": title,
        }],
        "level_changes": [{
            "skill": "Python", "from": None, "to": "初级",
            "source": title, "evidence": "", "evidence_kind": "self_report",
        }],
        "target_job": "软件工程师",
    }
    written = growth.append(entry, baseline_skills=[])
    assert written["status"] == "written"
    return fingerprint


class SelfReportCompletionApiTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._loop = LoopStore(directory=self._tmp.name)
        self._growth = GrowthStore(directory=self._tmp.name)
        self._stores = Stores(growth=self._growth, loop=self._loop)
        app.dependency_overrides[create_stores] = lambda: self._stores
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.pop(create_stores, None)
        self._tmp.cleanup()

    def test_one_click_completion_without_evidence_is_self_report(self):
        tid = _seed_loop(self._loop)
        resp = self.client.post(f"/api/tasks/{tid}/complete", json={})
        self.assertEqual(resp.status_code, 200)
        body = resp.json()

        self.assertTrue(body["completion_written"])
        self.assertEqual(body["growth_event_status"], "ok")
        self.assertEqual(body["growth_event"]["evidence_kind"], "self_report")
        self.assertEqual(body["growth_event"]["evidence"], "")
        self.assertTrue(any(
            update["skill"] == "Python"
            and update["evidence_kind"] == "self_report"
            for update in body["updates"]
        ))

        task = _task(self._loop, tid)
        self.assertEqual(task["status"], "completed")
        self.assertEqual(task["evidence"], "")
        self.assertEqual(task["evidence_kind"], "self_report")
        self.assertEqual(task["note"], "")

    def test_note_is_kept_separate_and_is_not_proof(self):
        tid = _seed_loop(self._loop)
        note = "删除旋转卡了两天，最后照可视化推通了"
        resp = self.client.post(f"/api/tasks/{tid}/complete", json={"note": note})
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["growth_event"]["evidence_kind"], "self_report")

        task = _task(self._loop, tid)
        self.assertEqual(task["evidence"], "")
        self.assertEqual(task["evidence_kind"], "self_report")
        self.assertEqual(task["note"], note)

    def test_evidence_attached_at_completion_counts_as_proof(self):
        tid = _seed_loop(self._loop)
        proof = "https://github.com/demo/tree/main/data-structures"
        resp = self.client.post(f"/api/tasks/{tid}/complete", json={"evidence": proof})
        self.assertEqual(resp.status_code, 200)
        body = resp.json()

        self.assertEqual(body["growth_event"]["evidence_kind"], "growth")
        self.assertTrue(any(
            update["skill"] == "Python"
            and update["evidence_kind"] == "growth"
            for update in body["updates"]
        ))
        task = _task(self._loop, tid)
        self.assertEqual(task["evidence"], proof)
        self.assertEqual(task["evidence_kind"], "growth")

    def test_manual_event_without_evidence_is_self_report(self):
        resp = self.client.post("/api/growth/manual-event", json={
            "kind": "learning",
            "title": "看完数据结构课程",
            "skills": ["Python"],
        })
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["added"])
        task = _task(self._loop, body["task_id"])
        self.assertEqual(task["evidence"], "")
        self.assertEqual(task["evidence_kind"], "self_report")


class AttachProofApiTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._loop = LoopStore(directory=self._tmp.name)
        self._growth = GrowthStore(directory=self._tmp.name)
        self._stores = Stores(growth=self._growth, loop=self._loop)
        app.dependency_overrides[create_stores] = lambda: self._stores
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.pop(create_stores, None)
        self._tmp.cleanup()

    def _completed_self_report(self) -> str:
        tid = _seed_loop(self._loop)
        resp = self.client.post(f"/api/tasks/{tid}/complete", json={})
        self.assertEqual(resp.status_code, 200)
        return tid

    def test_attach_proof_flips_latest_skills_to_growth(self):
        tid = self._completed_self_report()
        _append_self_report_history(self._growth)
        proof = "https://github.com/demo/tree/main/data-structures"

        resp = self.client.post(f"/api/tasks/{tid}/evidence", json={"evidence": proof})
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["verified"])
        self.assertTrue(body["verification_written"])
        self.assertEqual(body["evidence_kind"], "growth")

        task = _task(self._loop, tid)
        self.assertEqual(task["evidence"], proof)
        self.assertEqual(task["evidence_kind"], "growth")

        growth = self._growth.load().data
        self.assertEqual(len(growth["history"]), 2)
        verification = growth["history"][-1]
        self.assertEqual(verification["kind"], "verification")
        self.assertEqual(verification["task_id"], tid)
        skills = {item["skill"]: item for item in growth["history"][-1]["user_skills_after"]}
        self.assertEqual(skills["Python"]["evidence_kind"], "growth")

        history_resp = self.client.get("/api/growth/history")
        self.assertEqual(history_resp.status_code, 200)
        history_body = history_resp.json()
        self.assertEqual(history_body["confirmed_count"], 1)
        latest = {item["skill"]: item for item in history_body["latest_skills"]}
        self.assertEqual(latest["Python"]["evidence_kind"], "growth")

    def test_attach_proof_before_next_round_waits_for_history(self):
        tid = self._completed_self_report()
        resp = self.client.post(f"/api/tasks/{tid}/evidence",
                                json={"evidence": "https://example.com/output"})
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertFalse(body["verified"])
        self.assertFalse(body["verification_written"])
        self.assertTrue(body["pending_next_round"])
        self.assertEqual(_task(self._loop, tid)["evidence_kind"], "growth")

    def test_blank_proof_rejected(self):
        tid = self._completed_self_report()
        for bad in ["", "   ", "\n\t"]:
            resp = self.client.post(f"/api/tasks/{tid}/evidence", json={"evidence": bad})
            self.assertEqual(resp.status_code, 422)
        self.assertEqual(_task(self._loop, tid)["evidence_kind"], "self_report")

    def test_cannot_attach_proof_to_pending_task(self):
        tid = _seed_loop(self._loop)
        resp = self.client.post(f"/api/tasks/{tid}/evidence",
                                json={"evidence": "https://example.com/output"})
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(_task(self._loop, tid)["status"], "pending")


class ReopenClearsProofStateTest(unittest.TestCase):
    def test_reopen_clears_kind_and_note(self):
        with tempfile.TemporaryDirectory() as tmp:
            loop = LoopStore(directory=tmp)
            tid = _seed_loop(loop)
            done = loop.mark_completed(tid, evidence="", evidence_kind="self_report", note="复盘")
            self.assertEqual(done["status"], "completed")
            self.assertEqual(loop.find_task(tid)["evidence_kind"], "self_report")

            reopened = loop.reopen(tid)
            self.assertEqual(reopened["status"], "reopened")
            task = loop.find_task(tid)
            self.assertEqual(task["status"], "pending")
            self.assertEqual(task["evidence"], "")
            self.assertEqual(task["evidence_kind"], "")
            self.assertEqual(task["note"], "")


class GrowthRulesEvidenceKindTest(unittest.TestCase):
    def test_self_report_event_allows_empty_evidence(self):
        cleaned, reason = validate_event({
            "type": "learning", "title": "数据结构", "skills": ["Python"],
            "evidence": "", "evidence_kind": "self_report",
        })
        self.assertIsNone(reason)
        self.assertEqual(cleaned["evidence_kind"], "self_report")

    def test_growth_event_still_requires_evidence(self):
        cleaned, reason = validate_event({
            "type": "learning", "title": "数据结构", "skills": ["Python"],
            "evidence": "", "evidence_kind": "growth",
        })
        self.assertIsNone(cleaned)
        self.assertIn("evidence", reason)

    def test_apply_upgrades_keeps_self_report_kind(self):
        result = apply_upgrades([], [{
            "skill": "Python", "to": "初级", "source": "数据结构",
            "evidence": "", "evidence_kind": "self_report",
        }])
        self.assertEqual(result[0]["evidence_kind"], "self_report")


if __name__ == "__main__":
    unittest.main(verbosity=2)
