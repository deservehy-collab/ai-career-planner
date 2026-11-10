"""旧 JSON 数据的只读盘点与显式导入。

应用启动和注册**不再**调用任何自动迁移：旧 ``loop.json`` / ``growth.json``
只能通过 ``scripts/migrate_legacy_plans.py``，在人工确认后导入为一份
``legacy_readonly`` 档案。导入不改写、不删除原文件，也不把旧历史拆成
多份新档案。

归属规则
--------
- 文件里的 ``user_id`` 若能解析成 ``user_<数字>``，作为归属线索；
- 命令行显式 ``--user`` 优先，但与线索冲突时拒绝，避免串档；
- 两者都没有、或线索只是 ``default`` 时拒绝猜测，要求人工指定账号。
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from backend.db import User
from backend.models import PLAN_STATE_LEGACY, CareerPlan, UserProfile

__all__ = [
    "MigrationError",
    "LEGACY_FILES",
    "inventory",
    "resolve_owner",
    "choose_owner",
    "prepare_backup",
    "import_legacy_plan",
    "receipt_status",
]

LEGACY_FILES = ("loop.json", "growth.json")
_SUPPORTED_SCHEMA = 1


class MigrationError(RuntimeError):
    """迁移前置条件不成立；调用方应停止而不是猜。"""


def _parse_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MigrationError(f"{path} 不是可读的 JSON：{type(exc).__name__}: {exc}") from exc
    if not isinstance(data, dict):
        raise MigrationError(f"{path} 的顶层结构不是对象，拒绝导入")
    return data


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_sha256(path: Path) -> str:
    try:
        return _sha256(path.read_bytes())
    except OSError as exc:
        raise MigrationError(f"读不了 {path}：{exc}") from exc


def _canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _owner_hint(value) -> int | None:
    if isinstance(value, int) and value > 0:
        return value
    match = re.fullmatch(r"user_(\d+)", str(value or "").strip())
    return int(match.group(1)) if match else None


def _paths(source_dir, loop_path, growth_path) -> dict[str, Path | None]:
    base = Path(source_dir) if source_dir else None
    return {
        "loop": Path(loop_path) if loop_path else (base / "loop.json" if base else None),
        "growth": Path(growth_path) if growth_path else (base / "growth.json" if base else None),
    }


def inventory(source_dir=None, loop_path=None, growth_path=None) -> dict:
    """只读盘点旧文件，不写数据库、不改磁盘。"""
    paths = _paths(source_dir, loop_path, growth_path)
    present: dict[str, str] = {}
    payloads: dict[str, dict | None] = {"loop": None, "growth": None}
    raw_hashes: dict[str, str] = {}
    warnings: list[str] = []

    for key, path in paths.items():
        if path is None or not path.exists():
            continue
        payload = _parse_json(path)
        present[key] = str(path)
        payloads[key] = payload
        raw_hashes[key] = _file_sha256(path)
        schema = payload.get("schema_version")
        if schema is not None and schema != _SUPPORTED_SCHEMA:
            warnings.append(f"{path.name} 的 schema_version={schema!r}，将按原文只读保留")

    if not present:
        raise MigrationError("没有找到 loop.json 或 growth.json；请用 --source-dir/--loop/--growth 指定")

    hints = {
        key: _owner_hint(payload.get("user_id"))
        for key, payload in payloads.items()
        if payload is not None
    }
    distinct = {value for value in hints.values() if value is not None}
    if len(distinct) > 1:
        raise MigrationError(f"两个旧文件标注了不同账号 {sorted(distinct)}，拒绝猜测归属")
    owner_hint = next(iter(distinct)) if distinct else None
    if owner_hint is None:
        warnings.append("旧文件没有可识别的账号归属；导入时必须用 --user 明确指定")

    rounds = payloads["loop"].get("rounds") if payloads["loop"] else []
    rounds = rounds if isinstance(rounds, list) else []
    target_jobs: list[str] = []
    latest_job = ""
    latest_no = -1
    task_count = 0
    latest_created = None
    for entry in rounds:
        if not isinstance(entry, dict):
            continue
        job = str(entry.get("target_job") or "").strip()
        if job and job not in target_jobs:
            target_jobs.append(job)
        tasks = entry.get("tasks") if isinstance(entry.get("tasks"), list) else []
        task_count += len(tasks)
        try:
            no = int(entry.get("round") or 0)
        except (TypeError, ValueError):
            no = 0
        if job and no >= latest_no:
            latest_job, latest_no = job, no
            latest_created = entry.get("created_at")

    growth = payloads["growth"] if payloads["growth"] else {}
    history = growth.get("history") if isinstance(growth.get("history"), list) else []

    return {
        "paths": present,
        "payloads": payloads,
        "file_sha256": raw_hashes,
        "fingerprint": _sha256(_canonical(payloads).encode("utf-8")),
        "owner_hint": owner_hint,
        "owner_values": {
            key: (payload or {}).get("user_id")
            for key, payload in payloads.items()
            if payload is not None
        },
        "target_jobs": target_jobs,
        "latest_target_job": latest_job,
        "latest_round_created_at": latest_created,
        "counts": {
            "rounds": len(rounds),
            "tasks": task_count,
            "growth": len(history),
        },
        "warnings": warnings,
    }


def resolve_owner(factory: sessionmaker, user_ref) -> int:
    """把用户名或数字 id 解析成账号 id；找不到就报错。"""
    text = str(user_ref or "").strip()
    if not text:
        raise MigrationError("缺少 --user")
    with factory() as session:
        user = (
            session.get(User, int(text))
            if text.isdigit()
            else session.scalar(select(User).where(User.username == text))
        )
    if user is None:
        raise MigrationError(f"找不到账号 {text!r}；请先注册或换一个 --user")
    return int(user.id)


def choose_owner(factory: sessionmaker, user_ref, inv: dict) -> int:
    """显式账号优先；与文件线索冲突时拒绝。"""
    hint = inv.get("owner_hint")
    if user_ref:
        owner = resolve_owner(factory, user_ref)
        if hint is not None and hint != owner:
            raise MigrationError(
                f"旧文件标注归属 user_{hint}，与 --user 指定的账号 {owner} 不一致；拒绝串档导入"
            )
        return owner
    if hint is None:
        raise MigrationError("旧文件没有可识别的账号归属；请用 --user <用户名或 id> 明确指定，程序不猜")
    return resolve_owner(factory, str(hint))


def prepare_backup(inv: dict, backup_dir) -> dict:
    """复制旧文件到备份目录，并用 SHA-256 校验副本与原件一致。"""
    target = Path(backup_dir)
    try:
        target.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise MigrationError(f"备份目录已存在，拒绝覆盖：{target}") from exc
    except OSError as exc:
        raise MigrationError(f"创建备份目录失败：{exc}") from exc

    files: dict[str, dict] = {}
    for key, source in inv["paths"].items():
        source_path = Path(source)
        copy_path = target / source_path.name
        try:
            shutil.copy2(source_path, copy_path)
        except OSError as exc:
            raise MigrationError(f"备份 {source_path} 失败：{exc}") from exc
        original = _file_sha256(source_path)
        copied = _file_sha256(copy_path)
        if original != copied:
            raise MigrationError(f"备份校验失败：{source_path} 与副本 SHA-256 不一致")
        files[key] = {"source": str(source_path), "backup": str(copy_path), "sha256": original}
    return {"backup_dir": str(target), "files": files}


def _legacy_title(inv: dict) -> str:
    jobs = inv.get("target_jobs") or []
    if not jobs:
        return "旧数据（岗位未标注）"
    if len(jobs) == 1:
        return f"旧数据 · {jobs[0]}"
    return f"旧数据（{len(jobs)} 个岗位）"


def _next_round_no(inv: dict) -> int:
    rounds = (inv["payloads"]["loop"] or {}).get("rounds") or []
    highest = 0
    for entry in rounds:
        if not isinstance(entry, dict):
            continue
        try:
            highest = max(highest, int(entry.get("round") or 0))
        except (TypeError, ValueError):
            continue
    return highest + 1


def import_legacy_plan(
    factory: sessionmaker, owner_id: int, inv: dict, *, reimport: bool = False
) -> dict:
    """把一次盘点结果导入为一份只读档案；幂等，不复活已删除的档案。"""
    fingerprint = inv["fingerprint"]
    request_key = f"legacy:{fingerprint[:57]}"
    counts = dict(inv["counts"])

    def _response(status: str, plan_id: str | None, **extra) -> dict:
        return {"status": status, "plan_id": plan_id, "counts": counts, **extra}

    try:
        with factory.begin() as session:
            if session.get(User, int(owner_id)) is None:
                raise MigrationError(f"账号 {owner_id} 不存在")

            existing = session.scalar(
                select(CareerPlan).where(
                    CareerPlan.user_id == int(owner_id),
                    CareerPlan.request_key == request_key,
                )
            )
            profile = session.get(UserProfile, int(owner_id))
            receipt = dict(profile.legacy_receipt_json or {}) if profile else {}
            imports = receipt.get("imports")
            imports = dict(imports) if isinstance(imports, dict) else {}
            prior = imports.get(fingerprint)

            if existing is not None:
                return _response("already_imported", existing.id)
            if isinstance(prior, dict) and prior.get("status") == "imported" and not reimport:
                return _response(
                    "previously_imported",
                    prior.get("plan_id"),
                    message="这份旧数据以前导入过，档案已被删除；如需重新导入请显式 --reimport",
                )

            now = datetime.now(timezone.utc)
            plan = CareerPlan(
                id=uuid.uuid4().hex,
                user_id=int(owner_id),
                request_key=request_key,
                title=_legacy_title(inv)[:200],
                target_job=(inv.get("latest_target_job") or "")[:200],
                jd_text="",
                profile_snapshot_json={},
                initial_skills_json=None,
                current_skills_json=[],
                job_snapshot_json={},
                goal_json=None,
                pending_next_round_json=None,
                version=0,
                next_round_no=_next_round_no(inv),
                state=PLAN_STATE_LEGACY,
                legacy_payload_json={
                    "loop": inv["payloads"]["loop"],
                    "growth": inv["payloads"]["growth"],
                    "source": {
                        "paths": dict(inv["paths"]),
                        "file_sha256": dict(inv["file_sha256"]),
                        "target_jobs": list(inv["target_jobs"]),
                    },
                },
                created_at=now,
                updated_at=now,
            )
            session.add(plan)
            session.flush()

            if profile is None:
                profile = UserProfile(user_id=int(owner_id), version=1)
                session.add(profile)
            imports[fingerprint] = {
                "status": "imported",
                "plan_id": plan.id,
                "imported_at": now.isoformat(),
                "sources": dict(inv["paths"]),
                "file_sha256": dict(inv["file_sha256"]),
                "counts": counts,
                "target_jobs": list(inv["target_jobs"]),
                "reimported": bool(reimport and isinstance(prior, dict)),
            }
            receipt["imports"] = imports
            profile.legacy_receipt_json = receipt
            return _response("imported", plan.id, title=plan.title, target_job=plan.target_job)
    except IntegrityError:
        with factory() as session:
            existing = session.scalar(
                select(CareerPlan).where(
                    CareerPlan.user_id == int(owner_id),
                    CareerPlan.request_key == request_key,
                )
            )
            if existing is None:
                raise
            return _response("already_imported", existing.id)


def receipt_status(factory: sessionmaker, owner_id: int, fingerprint: str) -> dict | None:
    """只读查询：给 CLI 在预演时展示“以前导入过没有”。"""
    with factory() as session:
        profile = session.get(UserProfile, int(owner_id))
        receipt = profile.legacy_receipt_json if profile else None
    imports = (receipt or {}).get("imports") if isinstance(receipt, dict) else None
    if not isinstance(imports, dict):
        return None
    entry = imports.get(fingerprint)
    return dict(entry) if isinstance(entry, dict) else None
