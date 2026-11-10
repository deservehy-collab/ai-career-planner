"""旧 JSON 数据迁移：只读盘点、显式导入、幂等与不复活。

全程使用临时 SQLite 与临时 JSON 文件，不读项目 ``runs/``，不调用模型。
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest
from sqlalchemy import select

from backend import db, plan_repository
from backend.db import User
from backend.migration import (
    MigrationError,
    inventory,
    prepare_backup,
)
from backend.models import CareerPlan, UserProfile
from backend.plan_stores import PlanGone, SqlGrowthStore, SqlLoopStore


@pytest.fixture()
def db_url(tmp_path):
    return f"sqlite:///{tmp_path / 'legacy.db'}"


@pytest.fixture()
def factory(db_url, monkeypatch):
    monkeypatch.setenv("CAREER_DB_URL", db_url)
    db.init_db(db_url)
    return db.session_factory(db_url)


@pytest.fixture()
def cli():
    path = Path(__file__).resolve().parents[2] / "scripts" / "migrate_legacy_plans.py"
    spec = importlib.util.spec_from_file_location("migrate_legacy_plans", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_user(factory, username: str = "alice") -> int:
    with factory.begin() as session:
        user = User(username=username, password_hash="x")
        session.add(user)
        session.flush()
        return user.id


def _round(no: int, job: str, tasks: int = 0) -> dict:
    return {
        "round": no,
        "target_job": job,
        "created_at": f"2026-10-0{no}T08:00:00+00:00",
        "learning_plan": [],
        "project_plan": [],
        "tasks": [
            {
                "task_id": f"t-{no}-{index}",
                "index": index,
                "kind": "learning",
                "title": f"任务 {no}-{index}",
                "skills": ["Python"],
                "status": "pending",
                "evidence": "",
                "created_at": f"2026-10-0{no}T08:00:00+00:00",
                "completed_at": None,
            }
            for index in range(1, tasks + 1)
        ],
    }


def _write_legacy(tmp_path: Path, *, user_id="default", rounds=None, history=None) -> None:
    loop = {
        "schema_version": 1,
        "user_id": user_id,
        "created_at": "2026-10-01T00:00:00+00:00",
        "updated_at": "2026-10-02T00:00:00+00:00",
        "rounds": rounds if rounds is not None else [_round(1, "Python 后端开发", 2)],
    }
    growth = {
        "schema_version": 1,
        "user_id": user_id,
        "created_at": "2026-10-01T00:00:00+00:00",
        "updated_at": "2026-10-02T00:00:00+00:00",
        "baseline": {"source": "user_profile", "user_skills": ["Python"]},
        "history": history if history is not None else [{"kind": "normal", "at": "2026-10-02T00:00:00+00:00"}],
    }
    (tmp_path / "loop.json").write_text(json.dumps(loop, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "growth.json").write_text(json.dumps(growth, ensure_ascii=False), encoding="utf-8")


def _apply(cli, tmp_path: Path, *, backup: str = "backup", extra: tuple[str, ...] = ()) -> int:
    return cli.main([
        "--source-dir", str(tmp_path),
        "--user", "alice",
        "--apply", "--confirm",
        "--backup-dir", str(tmp_path / backup),
        *extra,
    ])


def _plans(factory) -> list[CareerPlan]:
    with factory() as session:
        return list(session.scalars(select(CareerPlan)).all())


def test_dry_run_reports_counts_and_writes_nothing(tmp_path, factory, cli, capsys):
    uid = _make_user(factory)
    _write_legacy(tmp_path, rounds=[_round(1, "Python 后端开发", 3), _round(2, "Python 后端开发", 0)])

    code = cli.main(["--source-dir", str(tmp_path), "--user", "alice"])

    out = capsys.readouterr().out
    assert code == 0
    assert "轮次 2" in out and "任务 3" in out and "成长记录 1" in out
    assert "预演" in out
    assert "没有写数据库" in out
    assert _plans(factory) == []
    assert (tmp_path / "backup").exists() is False


def test_apply_imports_one_readonly_plan_with_payload_and_backup(tmp_path, factory, cli, capsys):
    uid = _make_user(factory)
    _write_legacy(tmp_path)

    code = _apply(cli, tmp_path)

    assert code == 0
    plans = _plans(factory)
    assert len(plans) == 1
    plan = plans[0]
    assert plan.state == "legacy_readonly"
    assert plan.user_id == uid
    assert plan.target_job == "Python 后端开发"
    assert plan.legacy_payload_json["loop"]["rounds"][0]["tasks"][0]["task_id"] == "t-1-1"
    assert plan.legacy_payload_json["growth"]["history"][0]["kind"] == "normal"
    assert plan.next_round_no == 2

    with factory() as session:
        receipt = session.get(UserProfile, uid).legacy_receipt_json
    entry = next(iter(receipt["imports"].values()))
    assert entry["status"] == "imported"
    assert entry["plan_id"] == plan.id
    assert entry["counts"] == {"rounds": 1, "tasks": 2, "growth": 1}
    # 回执只记摘要与来源，不复制旧事件原文
    assert "baseline" not in json.dumps(receipt)

    assert (tmp_path / "loop.json").exists(), "导入不改写、不删除原文件"
    backup = tmp_path / "backup"
    assert (backup / "loop.json").exists()
    for name in ("loop.json", "growth.json"):
        original = hashlib.sha256((tmp_path / name).read_bytes()).hexdigest()
        copied = hashlib.sha256((backup / name).read_bytes()).hexdigest()
        assert original == copied


def test_multiple_jobs_in_legacy_files_are_not_split(tmp_path, factory, cli):
    _make_user(factory)
    _write_legacy(
        tmp_path,
        rounds=[
            _round(1, "Python 后端开发", 1),
            _round(2, "跨境电商运营", 1),
            _round(3, "跨境电商运营", 0),
        ],
    )

    assert _apply(cli, tmp_path) == 0

    plans = _plans(factory)
    assert len(plans) == 1, "同一份旧数据只导入一份档案，不按岗位拆分"
    assert plans[0].title == "旧数据（2 个岗位）"
    assert plans[0].target_job == "跨境电商运营"
    assert plans[0].legacy_payload_json["source"]["target_jobs"] == ["Python 后端开发", "跨境电商运营"]


def test_missing_owner_is_refused_without_guessing(tmp_path, factory, cli, capsys):
    _make_user(factory)
    _write_legacy(tmp_path, user_id="default")

    code = cli.main(["--source-dir", str(tmp_path), "--apply", "--confirm"])

    assert code == 2
    assert "没有可识别的账号归属" in capsys.readouterr().err
    assert _plans(factory) == []


def test_owner_mismatch_is_refused(tmp_path, factory, cli, capsys):
    _make_user(factory, "alice")
    _write_legacy(tmp_path, user_id="user_999")

    code = cli.main([
        "--source-dir", str(tmp_path), "--user", "alice", "--apply", "--confirm",
        "--backup-dir", str(tmp_path / "backup"),
    ])

    assert code == 2
    assert "不一致" in capsys.readouterr().err
    assert _plans(factory) == []


def test_repeat_apply_is_idempotent(tmp_path, factory, cli, capsys):
    _make_user(factory)
    _write_legacy(tmp_path)

    assert _apply(cli, tmp_path) == 0
    capsys.readouterr()
    second = cli.main([
        "--source-dir", str(tmp_path), "--user", "alice",
        "--apply", "--confirm", "--backup-dir", str(tmp_path / "backup-2"),
    ])

    assert second == 0
    assert "没有重复导入" in capsys.readouterr().out
    assert len(_plans(factory)) == 1


def test_deleted_legacy_plan_is_not_revived_unless_reimport(tmp_path, factory, cli, capsys):
    uid = _make_user(factory)
    _write_legacy(tmp_path)
    assert _apply(cli, tmp_path) == 0
    plan_id = _plans(factory)[0].id

    plan_repository.delete_plan(factory, uid, plan_id)
    assert _plans(factory) == []

    capsys.readouterr()
    again = cli.main([
        "--source-dir", str(tmp_path), "--user", "alice",
        "--apply", "--confirm", "--backup-dir", str(tmp_path / "backup-2"),
    ])
    assert again == 0
    assert "以前导入过" in capsys.readouterr().out
    assert _plans(factory) == [], "删除后再次执行不能复活旧档案"

    reimport = _apply(cli, tmp_path, backup="backup-3", extra=("--reimport",))
    assert reimport == 0
    plans = _plans(factory)
    assert len(plans) == 1
    assert plans[0].id != plan_id


def test_invalid_json_is_refused(tmp_path, factory, cli, capsys):
    _make_user(factory)
    (tmp_path / "loop.json").write_text("{not json", encoding="utf-8")

    code = cli.main(["--source-dir", str(tmp_path), "--user", "alice", "--apply", "--confirm"])

    assert code == 2
    assert "JSON" in capsys.readouterr().err
    assert _plans(factory) == []


def test_apply_without_confirm_is_refused(tmp_path, factory, cli, capsys):
    _make_user(factory)
    _write_legacy(tmp_path)

    code = cli.main(["--source-dir", str(tmp_path), "--user", "alice", "--apply"])

    assert code == 2
    assert "--confirm" in capsys.readouterr().err
    assert _plans(factory) == []


def test_backup_hash_mismatch_aborts_before_write(tmp_path, factory, cli, monkeypatch, capsys):
    _make_user(factory)
    _write_legacy(tmp_path)

    def corrupt_copy(source, target):
        Path(target).write_text("corrupted", encoding="utf-8")

    monkeypatch.setattr("backend.migration.shutil.copy2", corrupt_copy)

    code = _apply(cli, tmp_path)

    assert code == 2
    assert "备份校验失败" in capsys.readouterr().err
    assert _plans(factory) == []


def test_legacy_summary_and_stores_are_read_only(tmp_path, factory, cli):
    uid = _make_user(factory)
    _write_legacy(
        tmp_path,
        rounds=[_round(1, "Python 后端开发", 2), _round(2, "Python 后端开发", 1)],
        history=[{"kind": "normal"}, {"kind": "adjustment"}],
    )
    assert _apply(cli, tmp_path) == 0
    plan = _plans(factory)[0]

    listing = plan_repository.list_plans(factory, uid)
    summary = listing["items"][0]
    assert summary["is_legacy"] is True
    assert summary["counts"] == {"rounds": 2, "tasks": 3, "growth": 2}
    assert summary["latest_round"]["round_no"] == 2

    loop_store = SqlLoopStore(factory, uid, plan.id)
    assert len(loop_store.load().data["rounds"]) == 2
    growth_store = SqlGrowthStore(factory, uid, plan.id)
    assert len(growth_store.load().data["history"]) == 2
    with pytest.raises(PlanGone):
        loop_store.initialize()
    with pytest.raises(PlanGone):
        growth_store._write({"history": []})


def test_inventory_never_needs_a_database(tmp_path):
    _write_legacy(tmp_path)
    inv = inventory(source_dir=tmp_path)
    assert inv["counts"] == {"rounds": 1, "tasks": 2, "growth": 1}
    assert inv["owner_hint"] is None
    assert "没有可识别的账号归属" in " ".join(inv["warnings"])
    assert len(inv["fingerprint"]) == 64


def test_prepare_backup_refuses_existing_directory(tmp_path):
    _write_legacy(tmp_path)
    inv = inventory(source_dir=tmp_path)
    target = tmp_path / "backup"
    target.mkdir()
    with pytest.raises(MigrationError, match="已存在"):
        prepare_backup(inv, target)
