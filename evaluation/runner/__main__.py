"""命令行入口。

```bash
python -m evaluation                          # 只跑离线用例（默认）
python -m evaluation --mode all               # 离线 + 在线
python -m evaluation --mode live --backend http://127.0.0.1:8080
python -m evaluation --category verification  # 只跑一类
python -m evaluation --case dedup-total-cap   # 只跑一条
```

退出码：有失败 → 1；全部通过或只有 skipped → 0（CI 里环境不可用时不会红）。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime

from . import AGENT_DIR, EVALUATION_DIR, REPO_ROOT
from .live import run_live_cases
from .models import STATUS_FAILED, STATUS_PASSED, STATUS_SKIPPED, CaseResult
from .offline import run_offline_cases
from .report import write_reports

CASES_DIR = EVALUATION_DIR / "cases"
DATASETS_DIR = EVALUATION_DIR / "datasets"
FIXTURES_DIR = AGENT_DIR / "fixtures"
REPORTS_DIR = EVALUATION_DIR / "reports"


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    started_at = datetime.now()

    offline_cases = _filter(_load_offline(), args)
    live_cases = _filter(_load_live(), args)

    results: list[CaseResult] = []
    if args.mode in {"offline", "all"}:
        results += asyncio.run(
            run_offline_cases(
                offline_cases,
                fixtures_dir=FIXTURES_DIR,
                datasets_dir=DATASETS_DIR,
                timeout=args.timeout,
            )
        )
    if args.mode in {"live", "all"}:
        results += run_live_cases(live_cases, backend=args.backend, agent=args.agent)

    if not results:
        print("没有匹配的用例（检查 --category / --case）。", file=sys.stderr)
        return 1

    _print_summary(results, verbose=args.verbose)
    if not args.no_report:
        md_path, json_path = write_reports(
            results,
            REPORTS_DIR,
            mode=args.mode,
            backend=args.backend if args.mode in {"live", "all"} else None,
            agent=args.agent if args.mode in {"live", "all"} else None,
            started_at=started_at,
        )
        print(f"\n报告：{md_path.relative_to(REPO_ROOT)} | {json_path.relative_to(REPO_ROOT)}")

    return 1 if any(result.status == STATUS_FAILED for result in results) else 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="AI Learning Space — Agent 回归评测")
    parser.add_argument(
        "--mode",
        choices=("offline", "live", "all"),
        default="offline",
        help="offline=进程内真实 Agent（默认）；live=真后端；all=两者",
    )
    parser.add_argument("--category", action="append", help="只跑指定类别，可重复")
    parser.add_argument("--case", action="append", help="只跑指定用例 id，可重复")
    parser.add_argument("--backend", default="http://127.0.0.1:8080")
    parser.add_argument("--agent", default="http://127.0.0.1:8000")
    parser.add_argument("--timeout", type=float, default=15.0, help="单条离线用例超时（秒）")
    parser.add_argument("--no-report", action="store_true", help="不写 reports/")
    parser.add_argument("-v", "--verbose", action="store_true", help="打印每条用例的结果")
    return parser.parse_args(argv)


def _load_offline():
    from .models import load_offline_cases

    return load_offline_cases(CASES_DIR)


def _load_live():
    from .models import load_live_cases

    return load_live_cases(CASES_DIR)


def _filter(cases, args):
    selected = cases
    if args.category:
        selected = [case for case in selected if case.category in set(args.category)]
    if args.case:
        wanted = set(args.case)
        selected = [case for case in selected if case.id in wanted]
    return selected


def _print_summary(results: list[CaseResult], *, verbose: bool) -> None:
    if verbose:
        for result in results:
            mark = {
                STATUS_PASSED: "PASS",
                STATUS_FAILED: "FAIL",
                STATUS_SKIPPED: "SKIP",
            }.get(result.status, "????")
            print(f"  [{mark}] {result.case_id:<34} {result.category:<24} {result.duration_ms:>5}ms")
            if result.status == STATUS_FAILED:
                if result.error:
                    print(f"         ! {result.error}")
                for check in result.checks:
                    if not check.passed:
                        print(
                            f"         ! {check.path}: 期望 {check.op} {check.expected!r}, "
                            f"实际 {check.actual!r}"
                        )

    for mode in ("offline", "live"):
        items = [result for result in results if result.mode == mode]
        if not items:
            continue
        passed = sum(1 for item in items if item.status == STATUS_PASSED)
        failed = sum(1 for item in items if item.status == STATUS_FAILED)
        skipped = sum(1 for item in items if item.status == STATUS_SKIPPED)
        label = "离线" if mode == "offline" else "在线"
        executed = passed + failed
        rate = f"{passed / executed * 100:.1f}%" if executed else "—"
        print(f"{label}：{len(items)} 条，通过 {passed}，失败 {failed}，跳过 {skipped}，通过率 {rate}")

    failed_cases = [result for result in results if result.status == STATUS_FAILED]
    if failed_cases:
        print("\n失败用例：")
        for result in failed_cases:
            print(f"  - {result.case_id} ({result.category})" + (f"：{result.error}" if result.error else ""))


if __name__ == "__main__":
    raise SystemExit(main())
