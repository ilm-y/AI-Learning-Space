package com.ails.backend.service;

import com.ails.backend.dto.memory.agent.MemoryVectorHit;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.within;

/**
 * RRF 公式与排序的纯函数测试（不依赖 Spring / 数据库）。
 */
class RrfFuserTest {

    private static final double K = 60.0;

    @Test
    @DisplayName("两路无交集：分数符合 1/(k+rank)，按分数降序")
    void fuseTwoDisjointLists() {
        // 词法：A(1) B(2) C(3)；向量：B(1) D(2) A(3)
        List<Long> lexical = List.of(1L, 2L, 3L);
        List<MemoryVectorHit> vector = List.of(
                new MemoryVectorHit(2L, 0.9, "b"),
                new MemoryVectorHit(4L, 0.8, "d"),
                new MemoryVectorHit(1L, 0.7, "a"));

        List<RrfFuser.Fused> fused = RrfFuser.fuse(lexical, vector, K, 1.0, 1.0);

        assertThat(fused).extracting(RrfFuser.Fused::memoryId)
                .containsExactly(2L, 1L, 4L, 3L);
        assertThat(fused.get(0).score()).isCloseTo(1.0 / 62 + 1.0 / 61, within(1e-12));
        assertThat(fused.get(1).score()).isCloseTo(1.0 / 61 + 1.0 / 63, within(1e-12));
        assertThat(fused.get(2).score()).isCloseTo(1.0 / 62, within(1e-12));
        assertThat(fused.get(3).score()).isCloseTo(1.0 / 63, within(1e-12));
    }

    @Test
    @DisplayName("同一 id 双路命中：分数为两路之和，两个排名都非空")
    void fuseSameIdFromBothChannels() {
        List<RrfFuser.Fused> fused = RrfFuser.fuse(
                List.of(42L),
                List.of(new MemoryVectorHit(42L, 0.95, "hash")),
                K, 1.0, 1.0);

        assertThat(fused).hasSize(1);
        RrfFuser.Fused hit = fused.get(0);
        assertThat(hit.score()).isCloseTo(2.0 / 61, within(1e-12));
        assertThat(hit.lexicalRank()).isEqualTo(1);
        assertThat(hit.vectorRank()).isEqualTo(1);
        assertThat(hit.vectorScore()).isEqualTo(0.95);
    }

    @Test
    @DisplayName("空列表：不抛异常，返回空")
    void fuseEmptyLists() {
        assertThat(RrfFuser.fuse(List.of(), List.of(), K, 1.0, 1.0)).isEmpty();
        assertThat(RrfFuser.fuse(null, null, K, 1.0, 1.0)).isEmpty();
    }

    @Test
    @DisplayName("单路重复 id：只按第一次出现计分，不重复累加")
    void duplicateIdInOneChannelCountsOnce() {
        List<RrfFuser.Fused> fused = RrfFuser.fuse(
                List.of(7L, 7L), List.of(), K, 1.0, 1.0);

        assertThat(fused).hasSize(1);
        assertThat(fused.get(0).score()).isCloseTo(1.0 / 61, within(1e-12));
        assertThat(fused.get(0).lexicalRank()).isEqualTo(1);
    }

    @Test
    @DisplayName("权重：词法权重为 2 时只影响词法贡献")
    void appliesChannelWeights() {
        List<RrfFuser.Fused> fused = RrfFuser.fuse(
                List.of(1L), List.of(new MemoryVectorHit(2L, 0.5, "h")),
                K, 2.0, 1.0);

        assertThat(fused.get(0).memoryId()).isEqualTo(1L);
        assertThat(fused.get(0).score()).isCloseTo(2.0 / 61, within(1e-12));
        assertThat(fused.get(1).score()).isCloseTo(1.0 / 61, within(1e-12));
    }
}
