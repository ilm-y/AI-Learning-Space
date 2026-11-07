package com.ails.backend.service;

import com.ails.backend.dto.memory.agent.MemoryVectorHit;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

/**
 * Reciprocal Rank Fusion（RRF）纯函数实现，便于单测，不依赖数据库或线程池。
 *
 * <p>公式：</p>
 * <pre>
 *   rrf(d) = Σ_i w_i / (k + rank_i(d))
 *   k = 60（默认），rank 从 1 开始，未命中某一路不贡献分数
 * </pre>
 *
 * <p>只使用排名而不是原始分数：关键词相关性无上界、余弦相似度在 [-1,1]，
 * 两者量纲不同，直接加权必须先做分布敏感的归一化；RRF 天然跨量纲，
 * 且缺一路时只加一路，降级不需要特殊分支。</p>
 */
public final class RrfFuser {

    /** 设计文档固定的 RRF 常量。 */
    public static final double DEFAULT_K = 60.0;

    private RrfFuser() {
    }

    /**
     * 融合两路排名。
     *
     * <p>同一 id 在单路内重复出现时只按第一次出现的排名计分，避免重复项虚增分数。</p>
     *
     * @return 按 {@code rrf DESC, memoryId ASC} 排序的结果；最终展示排序由调用方
     *         结合 importance / updatedAt 再做确定性 tie-break
     */
    public static List<Fused> fuse(List<Long> lexicalIds,
                                   List<MemoryVectorHit> vectorHits,
                                   double k,
                                   double lexicalWeight,
                                   double vectorWeight) {
        double effectiveK = k > 0 ? k : DEFAULT_K;
        Map<Long, Mutable> acc = new HashMap<>();

        if (lexicalIds != null) {
            Set<Long> seen = new HashSet<>();
            int rank = 0;
            for (Long id : lexicalIds) {
                if (id == null || !seen.add(id)) {
                    continue;
                }
                rank++;
                Mutable m = acc.computeIfAbsent(id, ignored -> new Mutable());
                m.score += lexicalWeight / (effectiveK + rank);
                m.lexicalRank = rank;
            }
        }

        if (vectorHits != null) {
            Set<Long> seen = new HashSet<>();
            int rank = 0;
            for (MemoryVectorHit hit : vectorHits) {
                if (hit == null || hit.memoryId() == null || !seen.add(hit.memoryId())) {
                    continue;
                }
                rank++;
                Mutable m = acc.computeIfAbsent(hit.memoryId(), ignored -> new Mutable());
                m.score += vectorWeight / (effectiveK + rank);
                m.vectorRank = rank;
                m.vectorScore = hit.score();
            }
        }

        List<Fused> fused = new ArrayList<>(acc.size());
        acc.forEach((id, m) -> fused.add(new Fused(
                id, m.score, m.lexicalRank, m.vectorRank, m.vectorScore)));
        fused.sort((a, b) -> {
            int byScore = Double.compare(b.score(), a.score());
            return byScore != 0 ? byScore : Long.compare(a.memoryId(), b.memoryId());
        });
        return fused;
    }

    /**
     * 一条融合结果。
     *
     * @param memoryId   记忆 id
     * @param score      RRF 分数
     * @param lexicalRank 词法排名（1-based，未命中为 null）
     * @param vectorRank  向量排名（1-based，未命中为 null）
     * @param vectorScore 向量相似度（未命中为 null）
     */
    public record Fused(long memoryId,
                        double score,
                        Integer lexicalRank,
                        Integer vectorRank,
                        Double vectorScore) {
    }

    private static final class Mutable {
        private double score;
        private Integer lexicalRank;
        private Integer vectorRank;
        private Double vectorScore;
    }
}
