package com.ails.backend.dto.document;

import java.util.List;

/**
 * 诊断端点返回的 legacy READY 文档报告。
 *
 * <p>这是<b>诊断数据</b>，不是检索白名单：Agent 的正常检索流程只消费
 * {@code ready-ids}，不会读取该报告。设计上把两者分开，避免“诊断查询”在严格模式下
 * 被误当成资格判定来源。</p>
 *
 * @param legacyReadyDocs    符合 READY/未软删/索引完整且 fingerprint 为空的文档数
 * @param legacyModels       这些文档的 embedding_model 去重列表（已排序）
 * @param legacyCollections  这些文档的 vector_collection 去重列表（NULL 以 {@code "<null>"} 表示）
 * @param items              逐文档明细
 */
public record LegacyReadyReport(
        int legacyReadyDocs,
        List<String> legacyModels,
        List<String> legacyCollections,
        List<LegacyReadyDocument> items) {
}
