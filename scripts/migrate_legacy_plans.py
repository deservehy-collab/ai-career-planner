#!/usr/bin/env python
"""把旧 ``loop.json`` / ``growth.json`` 显式导入为一份只读档案。

默认只盘点（dry-run）；真正写入必须同时给 ``--apply --confirm``。导入前会
把原文件复制到备份目录并校验 SHA-256，导入本身幂等，档案被删除后不会因为
再次运行而复活（除非显式 ``--reimport``）。

示例::

    python scripts/migrate_legacy_plans.py --source-dir runs/users/user_2 --user alice
    python scripts/migrate_legacy_plans.py --source-dir runs/users/user_2 --user alice --apply --confirm
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy.exc import SQLAlchemyError  # noqa: E402

from backend import config, db  # noqa: E402
from backend.migration import (  # noqa: E402
    MigrationError,
    choose_owner,
    import_legacy_plan,
    inventory,
    prepare_backup,
    receipt_status,
)


def _default_backup_dir() -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return Path(config.AUTH_DB_PATH).parent / "legacy-backups" / stamp


def _print_inventory(inv: dict, owner_id: int | None, prior: dict | None) -> None:
    counts = inv["counts"]
    print("旧数据盘点（只读，未写入数据库）")
    for key, path in inv["paths"].items():
        print(f"  {key:<6} {path}")
        print(f"         SHA-256 {inv['file_sha256'][key]}")
    print(f"  轮次 {counts['rounds']} · 任务 {counts['tasks']} · 成长记录 {counts['growth']}")
    jobs = "、".join(inv["target_jobs"]) or "未标注"
    print(f"  目标岗位：{jobs}")
    hint = f"user_{inv['owner_hint']}" if inv["owner_hint"] is not None else "无（需 --user 指定）"
    print(f"  归属线索：{hint}")
    print(f"  内容指纹：{inv['fingerprint']}")
    if owner_id is not None:
        print(f"  导入账号：id={owner_id}")
    if prior:
        print(f"  历史回执：{prior.get('status')} · 档案 {prior.get('plan_id')}")
    for warning in inv["warnings"]:
        print(f"  注意：{warning}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="把旧 loop.json / growth.json 导入为一份 legacy_readonly 档案（默认 dry-run）",
    )
    parser.add_argument("--source-dir", help="包含 loop.json / growth.json 的目录")
    parser.add_argument("--loop", help="直接指定 loop.json 路径")
    parser.add_argument("--growth", help="直接指定 growth.json 路径")
    parser.add_argument("--user", help="导入到哪个账号（用户名或数字 id）")
    parser.add_argument("--apply", action="store_true", help="真正写入数据库（默认只盘点）")
    parser.add_argument("--confirm", action="store_true", help="与 --apply 一起使用，确认写入")
    parser.add_argument("--reimport", action="store_true", help="以前导入过且档案已删除时，允许重新导入")
    parser.add_argument("--backup-dir", help="备份目录；默认 runs/legacy-backups/<UTC 时间>")
    return parser


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    try:
        inv = inventory(
            source_dir=args.source_dir,
            loop_path=args.loop,
            growth_path=args.growth,
        )
        factory = db.session_factory(db.database_url())
        owner_id = choose_owner(factory, args.user, inv)
        prior = receipt_status(factory, owner_id, inv["fingerprint"])
        _print_inventory(inv, owner_id, prior)

        if not args.apply:
            print("\n这是预演：没有写数据库、没有动原文件。")
            print("确认无误后追加 --apply --confirm 执行导入。")
            return 0

        if not args.confirm:
            print("\n拒绝写入：--apply 必须同时带 --confirm。", file=sys.stderr)
            return 2

        backup = prepare_backup(inv, args.backup_dir or _default_backup_dir())
        print(f"\n备份已校验：{backup['backup_dir']}")
        result = import_legacy_plan(factory, owner_id, inv, reimport=args.reimport)
        status = result["status"]
        if status == "imported":
            print(f"已导入为只读档案 {result['plan_id']}：{result.get('title')}")
        elif status == "already_imported":
            print(f"没有重复导入：这份旧数据已对应档案 {result['plan_id']}")
        else:
            print(f"没有写入：{result.get('message') or status}")
        print("原文件未删除、未改名；仍需保留时请自行留存。")
        return 0
    except MigrationError as exc:
        print(f"迁移中止：{exc}", file=sys.stderr)
        return 2
    except SQLAlchemyError as exc:
        print(f"数据库不可用：{type(exc).__name__}；请先运行 python -m backend.db --init", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
