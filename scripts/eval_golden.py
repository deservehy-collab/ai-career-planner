"""跑黄金评测集：同一批人工标注输入 → 工作流 → 确定性计分。

用法
----
    python scripts/eval_golden.py                     # 跑全部用例
    python scripts/eval_golden.py --limit 5           # 先跑 5 条（冒烟）
    python scripts/eval_golden.py --ids ai-jd-rag,acct-self-report-only
    python scripts/eval_golden.py --json-out runs/eval/golden-2026-10-23.json
    python scripts/eval_golden.py --baseline <上一份 JSON>   # 对比变好/变坏
    python scripts/eval_golden.py --with-profile      # 打开内置岗位画像与资料摘要再跑一次
    python scripts/eval_golden.py --dry-run           # 只校验用例文件，不调模型

设计约束
--------
- **不写真实业务数据**：成长历史基线来自临时目录，工作流内的写入随临时目录删除。
- **不调模型时也能跑**：--dry-run 只读取 YAML 并校验形状。
- 通过率不是唯一信号：--baseline 会单独列出"之前通过、现在失败"的回归用例，
  只要有回归，即使通过率达标也以非零退出码结束。
- 结果 JSON 是给人对比用的：每次跑完保留一份，换 prompt/模型后再跑一次。

退出码：0 = 达标且无回归；1 = 低于 --min-pass 或存在回归；2 = 用例/参数错误。
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from backend import config as backend_config  # noqa: E402,F401 - 先加载 .env，云端 key 才可见
from agents.growth_store import GrowthStore  # noqa: E402
from golden.scoring import (  # noqa: E402
    DEFAULT_CASES_PATH,
    EvalContext,
    CaseReport,
    evaluate_case,
    load_cases,
    regression_diff,
    summarize,
)


def _select_cases(cases: list[dict], ids: str | None, limit: int | None) -> list[dict]:
    if ids:
        wanted = [part.strip() for part in ids.split(",") if part.strip()]
        known = {str(case.get("id")) for case in cases}
        unknown = [cid for cid in wanted if cid not in known]
        if unknown:
            raise ValueError(f"未知用例 ID：{'、'.join(unknown)}")
        cases = [case for case in cases if str(case.get("id")) in wanted]
    if limit is not None:
        if limit < 1:
            raise ValueError("--limit 必须 ≥ 1")
        cases = cases[:limit]
    if not cases:
        raise ValueError("筛选后没有可运行的用例")
    return cases


def _model_meta() -> dict:
    try:
        from agents import llm

        return {"channel": llm.get_channel(), "model": llm.active_model_name()}
    except Exception as exc:  # noqa: BLE001 - 只是报告元信息，不影响评测
        return {"channel": None, "model": None, "error": f"{type(exc).__name__}: {exc}"}


def _run_case(case: dict, store: GrowthStore, enable_job_profile: bool = False) -> dict:
    from backend.analyze import run_workflow
    from backend.schemas import AnalyzeRequest, UserProfileRequest

    profile = case.get("profile") or {}
    payload = AnalyzeRequest(
        user_profile=UserProfileRequest(
            major=str(profile.get("major") or ""),
            grade=str(profile.get("grade") or ""),
            skills=[str(item) for item in profile.get("skills") or []],
            projects=[str(item) for item in profile.get("projects") or []],
        ),
        target_job=str(case.get("target_job") or ""),
        jd_text=case.get("jd_text") or None,
    )
    return run_workflow(payload, baseline_store=store, enable_job_profile=enable_job_profile)


def _print_report(report: CaseReport, elapsed: float, verbose: bool) -> None:
    status = "PASS" if report.passed else "FAIL"
    checks = len(report.checks)
    print(f"[{status}] {report.case_id}  {elapsed:.1f}s  ({checks} 项检查)", flush=True)
    if report.passed and not verbose:
        return
    for check in report.checks:
        if check.ok and not verbose:
            continue
        mark = "✓" if check.ok else "✗"
        print(f"    {mark} {check.name}: {check.detail}", flush=True)


def _configure_stdio() -> None:
    """Windows 控制台默认 GBK，报告里的 ✓/✗ 会直接抛 UnicodeEncodeError。

    实测：一条用例失败 + 中文明细 → 打印崩在 ✗ 上，而落盘排在打印之后，
    整份 JSON 报告跟着丢。先把输出流锁成 UTF-8，容错用 replace 而不是抛。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def main(argv: list[str] | None = None) -> int:
    _configure_stdio()
    parser = argparse.ArgumentParser(description="跑黄金评测集并给出确定性计分")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES_PATH,
                        help=f"用例文件（默认 {DEFAULT_CASES_PATH}）")
    parser.add_argument("--ids", type=str, default=None, help="只跑这些 ID（逗号分隔）")
    parser.add_argument("--limit", type=int, default=None, help="只跑前 N 条")
    parser.add_argument("--json-out", type=Path, default=None, help="把完整报告写到 JSON")
    parser.add_argument("--baseline", type=Path, default=None,
                        help="与上一份 JSON 报告对比，列出回归与修复")
    parser.add_argument("--min-pass", type=float, default=0.8,
                        help="通过率下限，低于则以退出码 1 结束（默认 0.8）")
    parser.add_argument("--dry-run", action="store_true", help="只校验用例文件，不调用模型")
    parser.add_argument("--channel", choices=("local", "cloud"), default=None,
                        help="本次评测使用的模型通道（只影响本进程，不改系统设置）")
    parser.add_argument("--with-profile", action="store_true",
                        help="打开内置岗位画像与资料摘要（默认关闭，好让结果和历史基线可比）")
    parser.add_argument("--verbose", action="store_true", help="通过用例也打印每条检查")
    args = parser.parse_args(argv)

    try:
        cases = _select_cases(load_cases(args.cases), args.ids, args.limit)
    except (ValueError, OSError, yaml.YAMLError) as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 2

    ctx = EvalContext.build()
    if args.dry_run:
        print(f"用例文件可读：{args.cases}（{len(cases)} 条，未调用模型）")
        return 0

    if args.channel:
        from agents import llm

        if args.channel == "cloud" and not llm.cloud_configured():
            print("[错误] 云端通道未配置 CLOUD_API_KEY", file=sys.stderr)
            return 2
        llm.set_channel(args.channel)

    meta = _model_meta()
    meta["enable_job_profile"] = bool(args.with_profile)
    print(
        f"模型通道：{meta.get('channel') or '—'} / {meta.get('model') or '—'}"
        f"｜内置岗位画像：{'开' if args.with_profile else '关'}",
        flush=True,
    )

    baseline = None
    if args.baseline:
        try:
            baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"[错误] 基线报告读不出来：{exc}", file=sys.stderr)
            return 2

    reports: list[CaseReport] = []
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="golden-eval-") as tmp:
        store = GrowthStore(directory=Path(tmp), user_id="golden")
        for index, case in enumerate(cases, start=1):
            case_id = str(case.get("id"))
            print(f"── [{index}/{len(cases)}] {case_id} " + "─" * 30, flush=True)
            case_started = time.perf_counter()
            try:
                result = _run_case(case, store, enable_job_profile=args.with_profile)
            except Exception as exc:  # noqa: BLE001 - 单条失败不打断整批
                report = CaseReport(case_id=case_id)
                report.add(
                    "run.error",
                    False,
                    f"工作流异常：{type(exc).__name__}: {exc}",
                )
            else:
                report = evaluate_case(case, result, ctx)
                report.meta["requirement_count"] = len(result.get("job_requirements") or [])
                report.meta["gap_count"] = len(result.get("skill_gaps") or [])
                report.meta["stage_count"] = len(result.get("learning_plan") or [])
                report.meta["project_count"] = len(result.get("project_plan") or [])
            elapsed = time.perf_counter() - case_started
            reports.append(report)
            _print_report(report, elapsed, args.verbose)

    summary = summarize(reports)
    diff = regression_diff(reports, baseline) if baseline else None
    total_elapsed = time.perf_counter() - started

    # 先落盘再打印：打印属于展示，出问题时不能把唯一一份可核对的产物一起拖掉。
    json_out_path: Path | None = None
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "cases_file": str(args.cases),
            "model": meta,
            "selection": {"ids": args.ids, "limit": args.limit},
            "summary": summary,
            "baseline_diff": diff,
            "cases": [item.to_dict() for item in reports],
        }
        args.json_out.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        json_out_path = args.json_out

    print("\n" + "=" * 60)
    print(
        f"结果：{summary['passed']}/{summary['total']} 通过"
        f"（{summary['pass_rate'] * 100:.1f}%），耗时 {total_elapsed:.1f}s"
    )
    if summary["failed_ids"]:
        print("未通过：" + "、".join(summary["failed_ids"]))
    if diff:
        print("与基线对比：")
        print("  回归（之前过、现在失败）：" + ("、".join(diff["regressions"]) or "无"))
        print("  修复（之前失败、现在通过）：" + ("、".join(diff["fixes"]) or "无"))
        if diff["new"]:
            print("  新增用例：" + "、".join(diff["new"]))
        if diff["missing"]:
            print("  基线里有、本次未跑：" + "、".join(diff["missing"]))

    if json_out_path:
        print(f"报告已写入：{json_out_path}")

    if diff and diff["regressions"]:
        return 1
    if summary["pass_rate"] < args.min_pass:
        print(
            f"通过率 {summary['pass_rate'] * 100:.1f}% 低于下限 "
            f"{args.min_pass * 100:.1f}%",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
