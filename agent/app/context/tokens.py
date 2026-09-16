"""Token 估算。

**这不是精确计数，也不打算是。** 它只服务一个目的：Context Builder 要能判断
「这一段还塞不塞得下」，而不是把上下文堆到模型报错为止。

之所以不引 tiktoken：不同厂商的分词器不一样（DeepSeek / Qwen / GPT 各不相同），
引一个特定厂商的分词器会让估算对别的模型不准。宁可要一个与厂商无关、
误差方向已知的粗估 —— 中文 1 字 ≈ 1 token，非中文 4 字符 ≈ 1 token，
对绝大多数中英混排文本偏高一点，也就是**宁可早一点开始裁剪**。
"""

from __future__ import annotations


def _is_cjk(char: str) -> bool:
    code = ord(char)
    return (
        0x4E00 <= code <= 0x9FFF  # CJK 统一汉字
        or 0x3400 <= code <= 0x4DBF  # 扩展 A
        or 0x3000 <= code <= 0x303F  # CJK 标点
        or 0xFF00 <= code <= 0xFFEF  # 全角字符
    )


def estimate_tokens(text: str | None) -> int:
    if not text:
        return 0
    cjk = sum(1 for char in text if _is_cjk(char))
    other = len(text) - cjk
    return cjk + (other + 3) // 4


def truncate_to_tokens(text: str, max_tokens: int) -> str:
    """按估算值截断，尽量落在行边界上，读起来不会断在半句话里。"""
    if max_tokens <= 0 or not text:
        return ""
    if estimate_tokens(text) <= max_tokens:
        return text

    kept: list[str] = []
    used = 0
    for line in text.splitlines():
        cost = estimate_tokens(line) + 1
        if used + cost > max_tokens:
            break
        kept.append(line)
        used += cost
    if kept:
        return "\n".join(kept)
    # 单行就超预算：按字符硬截
    return text[: max(1, max_tokens)]
