"""评测执行器。

支持两种入口（结果完全一致）：

    python -m evaluation              # 在仓库根目录
    cd evaluation && python -m runner # 在 evaluation 目录

两种入口都会先把 `agent/` 加进 sys.path。runner 内部一律使用相对导入，
所以它既可以是顶层包 `runner`，也可以是 `evaluation.runner`。
"""

from __future__ import annotations

import sys
from pathlib import Path

# runner/ → evaluation/ → 仓库根
EVALUATION_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = EVALUATION_DIR.parent
AGENT_DIR = REPO_ROOT / "agent"

if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))
