"""用 Pilot 的隔离启动逻辑拉起 Spring + **带只读上下文哈希诊断的 Agent**。

不复制、不修改 ``scripts/phase5/pilot/start_services.py``：只把它启动 Agent
子进程时使用的 ``run_agent.py`` 替换为本目录的 ``run_agent_prestep3.py``，
其余隔离校验、端口、数据库、Qdrant 前缀、Memory 前缀与内部凭据逻辑逐字复用。

用法（仓库根目录）：
    python scripts/phase5/pre-step3/start_prestep3_services.py

固定使用 C 策略；输出最后一行 ``STATE_FILE=<path>``。
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

PILOT_DIR = Path(__file__).resolve().parent.parent / "pilot"
START_SERVICES = PILOT_DIR / "start_services.py"


def _load_start_services():
    spec = importlib.util.spec_from_file_location("pilot_start_services", START_SERVICES)
    if spec is None or spec.loader is None:
        raise SystemExit(f"无法加载 {START_SERVICES}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    os.environ["AILS_CONTEXT_STRATEGY"] = "C"
    module = _load_start_services()

    original_popen = module.subprocess.Popen
    instrumented = str(Path(__file__).with_name("run_agent_prestep3.py"))

    def popen(command, *args, **kwargs):
        if (
            isinstance(command, (list, tuple))
            and len(command) >= 2
            and str(command[-1]).endswith("run_agent.py")
        ):
            command = [*command[:-1], instrumented]
        return original_popen(command, *args, **kwargs)

    # 只在本进程内替换 Agent 启动命令；Spring 启动不受影响。
    module.subprocess.Popen = popen
    try:
        return int(module.main())
    finally:
        module.subprocess.Popen = original_popen


if __name__ == "__main__":
    sys.exit(main())
