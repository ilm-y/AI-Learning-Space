"""`python -m evaluation` 的入口，等价于 `python -m evaluation.runner`。"""

from __future__ import annotations

from .runner.__main__ import main

if __name__ == "__main__":
    raise SystemExit(main())
