package com.ails.backend.service;

import com.ails.backend.entity.EvidenceSource;
import com.ails.backend.entity.LearningEvidence;
import com.ails.backend.enums.EvidenceCorrectness;
import com.ails.backend.enums.EvidenceSourceType;
import com.ails.backend.enums.EvidenceType;
import com.ails.backend.mapper.EvidenceSourceMapper;
import com.ails.backend.mapper.LearningEvidenceMapper;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.springframework.dao.DuplicateKeyException;

import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.when;

/**
 * 并发补挂来源时唯一键冲突必须被吞掉。
 * <p>
 * 这里模拟的是：Evidence 行已经存在，另一轮分析同时插入了同一个
 * (evidence_id, source_type, source_id)。若把 DuplicateKeyException 抛出去，
 * 整份分析事务会回滚 —— 包括本轮刚写入的 Evidence / Understanding。
 */
@ExtendWith(MockitoExtension.class)
class EvidenceConcurrencyTest {

    @Mock
    private LearningEvidenceMapper evidenceMapper;
    @Mock
    private EvidenceSourceMapper evidenceSourceMapper;

    @Test
    void duplicateSourceFromConcurrentRunDoesNotFailTheAnalysis() {
        EvidenceService service = new EvidenceService(evidenceMapper, evidenceSourceMapper);

        LearningEvidence existing = new LearningEvidence();
        existing.setId(9L);
        existing.setLastSeenRunId(1L);
        when(evidenceMapper.selectOne(any())).thenReturn(existing);
        // 第一次查：来源还不存在；插入撞唯一键后再查：并发事务已经把它插进去了
        when(evidenceSourceMapper.selectCount(any())).thenReturn(0L, 1L);
        when(evidenceSourceMapper.insert(any(EvidenceSource.class)))
                .thenThrow(new DuplicateKeyException("dup"));

        EvidenceService.AppendResult result = service.append(draft());

        assertThat(result.created()).isFalse();
        assertThat(result.evidence().getId()).isEqualTo(9L);
    }

    @Test
    void duplicateKeyFromAnotherConstraintIsNotSwallowed() {
        EvidenceService service = new EvidenceService(evidenceMapper, evidenceSourceMapper);

        LearningEvidence existing = new LearningEvidence();
        existing.setId(9L);
        existing.setLastSeenRunId(1L);
        when(evidenceMapper.selectOne(any())).thenReturn(existing);
        // 冲突发生后重新查不到这条来源：说明冲突来自别的完整性约束
        when(evidenceSourceMapper.selectCount(any())).thenReturn(0L);
        when(evidenceSourceMapper.insert(any(EvidenceSource.class)))
                .thenThrow(new DuplicateKeyException("foreign key"));

        assertThatThrownBy(() -> service.append(draft()))
                .isInstanceOf(DuplicateKeyException.class);
    }

    private static EvidenceService.Draft draft() {
        return new EvidenceService.Draft(
                1L, 2L, null, EvidenceType.USER_EXPLANATION, "我理解闭包会捕获变量",
                EvidenceCorrectness.UNKNOWN, null, null, null, false,
                1L, "rules-v1",
                List.of(new EvidenceService.SourceRef(EvidenceSourceType.MESSAGE, 100L)));
    }
}
