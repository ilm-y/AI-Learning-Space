"""Agent 回归评测。

这个包只做一件事：把「Agent 的行为是否符合契约」变成**可重复执行**的用例，
而不是每次改完 prompt / 解析逻辑后靠手工点一遍。

布局（对应 docs/analysis.md §三）：

```text
cases/      用例（offline = 进程内真实 Agent 代码；live = 真后端 + 真 Agent）
datasets/   live 用例的种子数据（当前只有说明，fixture 复用 agent/fixtures）
runner/     执行器（--offline / --live）
metrics/    把原始结果汇总成通过率
reports/    生成物（latest.md / latest.json）
```

运行方式见 `evaluation/README.md`。
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
AGENT_DIR = REPO_ROOT / "agent"
EVALUATION_DIR = Path(__file__).resolve().parent
CASES_DIR = EVALUATION_DIR / "cases"
REPORTS_DIR = EVALUATION_DIR / "reports"


def ensure_agent_importable() -> None:
    """把 `agent/` 放进 sys.path，让 runner 能 import `app.*`。

    Agent 是一个独立应用（`cd agent && python -m app.main`），没有安装成包。
    评测必须在**同一份代码**上跑，不能复制一份 SDK 出来 —— 复制出来的副本
    迟早会和真实实现漂移，评测就失去意义。因此这里只调整 import 路径。
    """
    agent = str(AGENT_DIR)
    if agent not in sys.path:
        sys.path.insert(0, agent)
