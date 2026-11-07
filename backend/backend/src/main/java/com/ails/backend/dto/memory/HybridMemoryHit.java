package com.ails.backend.dto.memory;

/**
 * 混合检索的一条结果。
 *
 * @param memory      MySQL 回源后的权威内容
 * @param rrfScore    RRF 融合分
 * @param lexicalRank 词法排名（1-based）；未命中词法路为 null
 * @param vectorRank  向量排名（1-based）；未命中向量路为 null
 * @param vectorScore 向量余弦相似度；未命中向量路为 null
 */
public record HybridMemoryHit(
        MemoryResponse memory,
        double rrfScore,
        Integer lexicalRank,
        Integer vectorRank,
        Double vectorScore) {
}
