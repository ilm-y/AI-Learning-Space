"""用例断言。

用例文件只描述「期望看到什么」，解释执行放在这里。这样做的好处是：
新增用例不需要写 Python；而断言语义集中在一处，改动不会让历史用例悄悄失效。

期望写法两种：

```json
{"evidence.count": 2}
{"evidence.conceptIds": {"op": "contains", "value": 10}}
```

第一种等价于 `eq`。支持的 op 见 `_apply`。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


class _Missing:
    """路径不存在。与「路径存在但值为 None」区分开 —— 后者是一个有效断言对象。"""

    def __repr__(self) -> str:  # pragma: no cover - 只用于报告
        return "<missing>"


MISSING = _Missing()


@dataclass(frozen=True)
class CheckOutcome:
    path: str
    op: str
    expected: Any
    actual: Any
    passed: bool
    message: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "op": self.op,
            "expected": _render(self.expected),
            "actual": _render(self.actual),
            "passed": self.passed,
            "message": self.message,
        }


def evaluate(expect: dict[str, Any], actual: dict[str, Any]) -> list[CheckOutcome]:
    """逐条执行期望，返回**全部**结果（不短路）。

    不短路是刻意的：一次运行里把所有不符都列出来，比「看到第一条失败就停」
    更容易判断是单点回归还是整类行为变了。
    """
    outcomes: list[CheckOutcome] = []
    for path, spec in expect.items():
        op, expected = _normalize(spec)
        found = resolve(actual, path)
        passed, message = _apply(op, expected, found)
        outcomes.append(CheckOutcome(path, op, expected, found, passed, message))
    return outcomes


def resolve(root: Any, path: str) -> Any:
    """按 `a.b[0].c` 取值；任何一段缺失都返回 MISSING。"""
    current = root
    for segment in path.split("."):
        match = re.fullmatch(r"([^\[\]]+)((?:\[\d+\])*)", segment)
        if match is None:
            return MISSING
        name, indexes = match.groups()
        if isinstance(current, dict) and name in current:
            current = current[name]
        else:
            return MISSING
        for raw_index in re.findall(r"\[(\d+)\]", indexes):
            index = int(raw_index)
            if not isinstance(current, (list, tuple)) or index >= len(current):
                return MISSING
            current = current[index]
    return current


def _normalize(spec: Any) -> tuple[str, Any]:
    if isinstance(spec, dict) and "op" in spec:
        return str(spec["op"]), spec.get("value")
    return "eq", spec


def _apply(op: str, expected: Any, actual: Any) -> tuple[bool, str]:
    if actual is MISSING:
        # 只有断言「不存在」时才允许路径缺失
        if op in {"missing", "falsy"}:
            return True, ""
        return False, "路径不存在"

    try:
        if op == "eq":
            return actual == expected, ""
        if op == "ne":
            return actual != expected, ""
        if op == "contains":
            return _contains(actual, expected), ""
        if op == "not_contains":
            return not _contains(actual, expected), ""
        if op == "all":
            return isinstance(actual, list) and all(item == expected for item in actual), ""
        if op == "any":
            return isinstance(actual, list) and any(item == expected for item in actual), ""
        if op == "subset":
            return isinstance(actual, list) and set(expected).issubset(set(actual)), ""
        if op == "length":
            return _length(actual) == expected, ""
        if op == "length_gte":
            return _length(actual) >= expected, ""
        if op == "length_lte":
            return _length(actual) <= expected, ""
        if op == "gte":
            return actual >= expected, ""
        if op == "lte":
            return actual <= expected, ""
        if op == "truthy":
            return bool(actual) is True, ""
        if op == "falsy":
            return bool(actual) is False, ""
        if op == "matches":
            return isinstance(actual, str) and re.search(str(expected), actual) is not None, ""
        if op == "startswith":
            return isinstance(actual, str) and actual.startswith(str(expected)), ""
        if op == "endswith":
            return isinstance(actual, str) and actual.endswith(str(expected)), ""
    except TypeError as exc:  # 类型不匹配（例如对 None 做 contains）
        return False, f"断言类型不匹配：{exc}"
    return False, f"未知的 op：{op}"


def _contains(actual: Any, expected: Any) -> bool:
    if isinstance(actual, str):
        return str(expected) in actual
    if isinstance(actual, (list, tuple, set)):
        return expected in actual
    return False


def _length(actual: Any) -> int:
    if actual is MISSING or actual is None:
        return 0
    try:
        return len(actual)
    except TypeError:
        return 0


def _render(value: Any, limit: int = 160) -> Any:
    """报告里不塞整段 prompt / 回答，超过长度就截断。"""
    if value is MISSING:
        return "<missing>"
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + "…"
    if isinstance(value, list) and len(value) > 8:
        return [*(_render(item, 40) for item in value[:8]), f"…共 {len(value)} 项"]
    if isinstance(value, dict):
        return {key: _render(item, 60) for key, item in list(value.items())[:10]}
    return value
