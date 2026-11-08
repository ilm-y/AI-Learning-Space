"""命令行入口：python -m tests.eval.run_eval --repeats 3

输出一张三模式对照表，并把完整 JSON 写到
`tests/eval/reports/<UTC 时间戳>.json`。退出码非 0 表示有用例断言失败。
"""

from __future__ import annotations

import argparse
import sys

from tests.eval.runner import MODES, run_eval


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="E2 Concept Resolver 离线评测")
    parser.add_argument("--repeats", type=int, default=3, help="每个用例的重复次数")
    args = parser.parse_args(argv)
    repeats = max(1, args.repeats)

    report = run_eval(repeats=repeats, write_report=True)
    modes = report["modes"]

    header = (
        f"{'mode':32} {'recall':>7} {'prec':>7} {'miss':>7} "
        f"{'p95ms':>8} {'avgTok':>8} {'callRate':>8} {'pass':>5}"
    )
    print(header)
    print("-" * len(header))
    for mode in MODES:
        result = modes[mode]
        metrics = result["metrics"]
        print(
            f"{mode:32} {metrics['recall']:>7.3f} {metrics['precision']:>7.3f} "
            f"{metrics['misrecallRate']:>7.3f} {metrics['p95PrepareLatencyMs']:>8.2f} "
            f"{metrics['avgResolverTokens']:>8.1f} {metrics['resolverCallRate']:>8.3f} "
            f"{str(result['passed']):>5}"
        )
    print()
    print(f"报告：{report.get('reportPath')}")
    print(
        "注意：Resolver 输出为脚本化预置 JSON，Token 为 estimate_tokens 估算值；"
        "此表验证管线接线与校验，不代表真实模型准确率。"
    )
    return 0 if report["summary"]["allPassed"] else 1


if __name__ == "__main__":
    sys.exit(main())
