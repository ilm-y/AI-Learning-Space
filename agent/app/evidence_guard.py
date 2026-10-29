"""非成功知识检索状态的运行时兜底。

为什么需要这一层：Prompt 已经要求模型在无资料时说明依据不足，但真实模型在
D01 / F02 / S04 这类“问题本身可以用通用知识回答”的场景里，仍会直接给出完整答案。
只在 system prompt 里重复规则，无法把“这次没有可靠资料”变成运行时事实。

这里做的是一个**保守、可观测、不改写模型输出**的兜底：

- 只处理四种明确状态：没有可用文档、没有命中、阈值过滤、检索降级；
- 只在问题看起来是知识请求时触发，纯寒暄 / 确认 / 继续这类普通聊天不打扰；
- 只有模型自己没有说明时才追加一条固定说明；
- 说明不含引用，不删除、不覆盖、不截断模型已经产出的内容；
- 由 Runtime 在流结束前作为一个普通 delta 发出，SSE 事件序列不变。

这不是语义判分，也不是“自动承认资料不足”的万能规则。它只能保证：当运行时
知道本轮没有可靠资料时，最终回答不会完全不提这一点。问题是否真的依赖上传资料，
仍然无法从当前请求结构中可靠判断；触发条件因此保持窄，且全部写入 trace。
"""

from __future__ import annotations

import re

# 这四种状态表示“本轮没有可用的资料依据”。SUCCESS / NOT_RETRIEVED 不处理：
# 前者正常使用现有上下文；后者是 plain 评测模式，根本没有检索。
GUARD_STATUSES = frozenset(
    {
        "NO_VALID_DOCUMENTS",
        "NO_MATCH",
        "THRESHOLD_FILTERED",
        "DEGRADED",
    }
)

# 说明必须同时做到两件事：承认本轮没有资料依据，且不把“没检索到”说成
# “资料或世界上不存在”。因此每条都以“本轮”限定，并明确“以上回答不基于上传资料”。
_STATUS_NOTICE = {
    "NO_VALID_DOCUMENTS": (
        "（本轮没有可用且已就绪的上传文档，以上回答不基于上传资料。）"
    ),
    "NO_MATCH": (
        "（本轮上传资料中没有检索到与本问题直接相关的依据，以上回答不基于上传资料。）"
    ),
    "THRESHOLD_FILTERED": (
        "（本轮检索到的文档片段相关度不足，没有检索到可确认的资料依据；"
        "以上回答不基于上传资料。）"
    ),
    "DEGRADED": (
        "（本轮知识检索不可用，无法确认上传资料是否包含答案；"
        "以上回答不基于上传资料。）"
    ),
}

# 只用于判断“模型是否已经自己说过”。这些模式要求否定词和对象同时出现，
# 不能因为回答里随便出现一个“没有”就跳过兜底。
_DISCLOSURE_PATTERNS = {
    "NO_VALID_DOCUMENTS": (
        r"(?:没有|未)(?:上传|提供|可用)[^。！？\n]{0,12}(?:资料|文档)",
        r"(?:资料|文档)[^。！？\n]{0,12}(?:没有|未)(?:上传|提供|可用)",
        r"(?:资料库|文档库)[^。！？\n]{0,8}(?:为空|没有)",
    ),
    "NO_MATCH": (
        r"(?:资料|文档|上传)[^。！？\n]{0,15}(?:没有|未|不)(?:直接|相关|命中|检索到|找到|包含|提供|提及|覆盖)",
        r"(?:没有|未)(?:找到|检索到|命中|检索出)[^。！？\n]{0,15}(?:资料|文档|内容|相关|依据)",
        r"没有直接答案",
    ),
    "THRESHOLD_FILTERED": (
        r"(?:资料|文档|上传)[^。！？\n]{0,15}(?:没有|未|不)(?:直接|相关|命中|检索到|找到|包含|提供|提及|覆盖)",
        r"(?:没有|未)(?:找到|检索到|命中|检索出)[^。！？\n]{0,15}(?:资料|文档|内容|相关|依据)",
        r"(?:相似度|相关度)[^。！？\n]{0,6}(?:低于|不足|未达到|不够)",
        r"没有直接答案",
    ),
    "DEGRADED": (
        r"(?:检索|知识库|向量|嵌入|Embedding|知识检索)[^。！？\n]{0,12}(?:失败|不可用|降级|异常|未启用)",
        r"(?:无法|不能)(?:检索|访问|连接)[^。！？\n]{0,10}(?:知识库|资料|文档|向量)",
        r"本次(?:知识)?检索(?:失败|不可用)",
    ),
}

# 这是“问题形态”启发式，不是主题词表：只在用户明确要求解释、比较、查询
# 某个事实/配置/协议/数量时生效。故意不包含“怎么 / 为什么 / 如何”这类
# 日常对话也大量出现的泛疑问词，避免把普通聊天也加上资料状态说明。
_KNOWLEDGE_REQUEST_MARKERS = (
    "解释",
    "说明",
    "介绍",
    "定义",
    "比较",
    "区别",
    "差异",
    "原理",
    "机制",
    "流程",
    "阶段",
    "步骤",
    "架构",
    "协议",
    "配置",
    "参数",
    "版本",
    "技术",
    "支持",
    "实现",
    "价格",
    "费用",
    "许可证",
    "许可",
    "CEO",
    "创始人",
    "是什么",
    "什么是",
    "哪些",
    "哪种",
    "多少",
    "是否",
    "能否",
    "列出",
    "总结",
    "概括",
    "分析",
    "资料",
    "文档",
    "依据",
    "情况",
)

# 纯寒暄 / 确认 / 继续：这些请求不因为检索没命中而被塞一条资料说明。
# 允许组合，例如“好的，谢谢”“嗯嗯，继续”。
_CASUAL_RE = re.compile(
    r"^(?:(?:你好|您好|hi|hello|嗨|在吗|谢谢|多谢|感谢|好的|好吧|好|嗯|哦|"
    r"再见|拜拜|收到|明白|懂了|继续|然后呢|随便聊聊|讲个笑话|你是谁|"
    r"你叫什么(?:名字)?|你能做什么)[\s，,。！!？?~啊呀吧呢哦]*)+$",
    re.IGNORECASE,
)


def status_notice(status: str) -> str | None:
    """返回该状态的固定说明文本；用于评测判断是否重复追加，不用于绕过校验。"""
    return _STATUS_NOTICE.get(str(status or ""))


def evidence_status_notice(status: str, question: str, answer: str) -> str | None:
    """返回应追加的状态说明；不应追加时返回 None。

    调用方只应在没有知识片段、状态属于 `GUARD_STATUSES` 时使用。
    """
    normalized = str(status or "")
    if normalized not in GUARD_STATUSES:
        return None
    if not is_knowledge_request(question):
        return None
    if already_discloses(normalized, answer):
        return None
    return _STATUS_NOTICE[normalized]


def is_knowledge_request(question: str) -> bool:
    """保守判断这句话是不是在问知识/资料，而不是寒暄或日常闲聊。"""
    text = (question or "").strip()
    if not text or _CASUAL_RE.match(text):
        return False
    # “为什么”本身是泛疑问词，先去掉再匹配“什么”，避免把
    # “为什么天空是蓝的”这类普通知识聊天误判成资料请求。
    probe = text.replace("为什么", "").casefold()
    return any(marker.casefold() in probe for marker in _KNOWLEDGE_REQUEST_MARKERS)


def already_discloses(status: str, answer: str) -> bool:
    """模型是否已经用“否定 + 对象”的方式说明了本轮资料状态。"""
    patterns = _DISCLOSURE_PATTERNS.get(status, ())
    return any(re.search(pattern, answer or "") for pattern in patterns)
