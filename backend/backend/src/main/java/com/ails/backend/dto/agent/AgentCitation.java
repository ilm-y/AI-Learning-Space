package com.ails.backend.dto.agent;

/**
 * Agent 在 {@code done} 事件里下发的结构化来源引用。
 * <p>
 * 对应 Python 侧 {@code app.citations.structured_citations()} 的 camelCase 输出。
 * 它只描述「这条来源指向哪个 chunk」，不携带正文：正文仍以 Markdown 文本里的
 * {@code [资料《标题》#序号]} 为准，避免把整段资料复制进消息表。
 * <p>
 * {@code chunkIndex} 用于把正文标签映射回结构化来源；{@code documentId} /
 * {@code chunkId} 才是历史读取时重新校验可用性、以及打开原文的稳定身份。
 *
 * @param documentId 文档主键，必须为正数
 * @param chunkId    文档切片主键，必须为正数
 * @param chunkIndex 切片在文档内的序号；可空以兼容不提供该字段的旧 Agent
 * @param sourceTitle 引用时展示的来源标题（模型看到的文件/文档名）
 */
public record AgentCitation(
        Long documentId,
        Long chunkId,
        Integer chunkIndex,
        String sourceTitle) {
}
