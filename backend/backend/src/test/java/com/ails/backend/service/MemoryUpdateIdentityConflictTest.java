package com.ails.backend.service;

import com.ails.backend.common.BusinessException;
import com.ails.backend.dto.memory.MemoryRequest;
import com.ails.backend.dto.memory.MemoryResponse;
import com.ails.backend.entity.Memory;
import com.ails.backend.enums.MemoryScopeType;
import com.ails.backend.enums.MemoryStatus;
import com.ails.backend.enums.MemoryType;
import com.ails.backend.mapper.MemoryMapper;
import com.ails.backend.mapper.MemorySourceMapper;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.springframework.dao.DuplicateKeyException;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.lenient;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

/**
 * MEM-01：更新路径的 {@link DuplicateKeyException} 分类逻辑。
 *
 * <p>真实并发场景在 {@code MemoryStateMachineConcurrencyIT} 中用真实 MySQL 验证；
 * 这里固定的是「哪些异常可以翻译成 409、哪些必须原样抛出」的判定规则。它不替代
 * 并发测试，也不能声称验证了数据库行为。</p>
 */
@ExtendWith(MockitoExtension.class)
class MemoryUpdateIdentityConflictTest {

    private static final String IDENTITY_555 = "Duplicate entry '1-GLOBAL-shared' "
            + "for key 'memory.uk_memory_identity'";

    @Mock
    private MemoryMapper memoryMapper;
    @Mock
    private MemorySourceMapper memorySourceMapper;
    @Mock
    private LearningSpaceService spaceService;
    @Mock
    private ConceptService conceptService;
    @Mock
    private MemoryOutboxPublisher outboxPublisher;
    @Mock
    private HybridSearchService hybridSearchService;

    private MemoryService service;

    @BeforeEach
    void setUp() {
        service = new MemoryService(memoryMapper, memorySourceMapper, spaceService,
                conceptService, outboxPublisher, hybridSearchService);
        lenient().when(memoryMapper.selectCount(any())).thenReturn(0L);
        lenient().when(memoryMapper.updateById(any(Memory.class))).thenReturn(1);
    }

    @Test
    @DisplayName("update 命中 uk_memory_identity：翻译为 409，并带可定位的 scope_key / memory_key")
    void identityUniqueKeyConflictOnUpdateBecomes409() {
        when(memoryMapper.selectById(42L)).thenReturn(memory(42L, "old-key", "old"));
        when(memoryMapper.updateById(any(Memory.class)))
                .thenThrow(new DuplicateKeyException(IDENTITY_555));

        assertThatThrownBy(() -> service.update(1L, 42L, request("shared", "new")))
                .isInstanceOfSatisfying(BusinessException.class, ex -> {
                    assertThat(ex.getCode()).isEqualTo(409);
                    // 只暴露定位信息，不暴露任何用户 id。
                    assertThat(ex.getMessage())
                            .contains("并发")
                            .contains("GLOBAL")
                            .contains("shared");
                });
    }

    @Test
    @DisplayName("update 命中其他唯一键：保持原有语义（DuplicateKeyException 原样抛出）")
    void otherUniqueKeyConflictOnUpdateIsNotConverted() {
        when(memoryMapper.selectById(42L)).thenReturn(memory(42L, "old-key", "old"));
        when(memoryMapper.updateById(any(Memory.class)))
                .thenThrow(new DuplicateKeyException(
                        "Duplicate entry 'x' for key 'memory.uk_some_future_key'"));

        assertThatThrownBy(() -> service.update(1L, 42L, request("shared", "new")))
                .isInstanceOf(DuplicateKeyException.class)
                .isNotInstanceOf(BusinessException.class);
    }

    @Test
    @DisplayName("update 命中其他完整性错误（无索引名）：保持原有语义，不被误报 409")
    void otherIntegrityErrorOnUpdateIsNotConverted() {
        when(memoryMapper.selectById(42L)).thenReturn(memory(42L, "old-key", "old"));
        when(memoryMapper.updateById(any(Memory.class)))
                .thenThrow(new DuplicateKeyException("foreign key constraint fails"));

        assertThatThrownBy(() -> service.update(1L, 42L, request("shared", "new")))
                .isInstanceOf(DuplicateKeyException.class)
                .isNotInstanceOf(BusinessException.class);
    }

    @Test
    @DisplayName("MySQL 5.7 风格 message（不带库名前缀）同样识别为身份冲突")
    void identityKeyWithoutSchemaPrefixIsStillRecognized() {
        when(memoryMapper.selectById(42L)).thenReturn(memory(42L, "old-key", "old"));
        when(memoryMapper.updateById(any(Memory.class)))
                .thenThrow(new DuplicateKeyException(
                        "Duplicate entry '1-GLOBAL-shared' for key 'uk_memory_identity'"));

        assertThatThrownBy(() -> service.update(1L, 42L, request("shared", "new")))
                .isInstanceOfSatisfying(BusinessException.class,
                        ex -> assertThat(ex.getCode()).isEqualTo(409));
    }

    @Test
    @DisplayName("更新到空闲身份：正常成功，修复没有误伤正常路径")
    void updateToFreeIdentityStillSucceeds() {
        when(memoryMapper.selectById(42L)).thenReturn(memory(42L, "old-key", "old"));

        MemoryResponse response = service.update(1L, 42L, request("fresh-key", "new"));

        assertThat(response.memoryKey()).isEqualTo("fresh-key");
        assertThat(response.content()).isEqualTo("new");
        verify(memoryMapper).updateById(any(Memory.class));
    }

    @Test
    @DisplayName("persist 创建路径的 DuplicateKeyException 处理未被破坏：仍然是 409")
    void createPathStillMapsIdentityConflictTo409() {
        when(memoryMapper.insert(any(Memory.class)))
                .thenThrow(new DuplicateKeyException(IDENTITY_555));

        assertThatThrownBy(() -> service.create(1L, request("shared", "new")))
                .isInstanceOfSatisfying(BusinessException.class,
                        ex -> assertThat(ex.getCode()).isEqualTo(409));
        verify(memoryMapper).insert(any(Memory.class));
        verify(memoryMapper, never()).updateById(any(Memory.class));
    }

    private static Memory memory(long id, String memoryKey, String content) {
        Memory memory = new Memory();
        memory.setId(id);
        memory.setUserId(1L);
        memory.setScopeType(MemoryScopeType.GLOBAL);
        memory.setScopeKey("GLOBAL");
        memory.setMemoryKey(memoryKey);
        memory.setMemoryType(MemoryType.LEARNING);
        memory.setContent(content);
        memory.setImportance(0.5f);
        memory.setStatus(MemoryStatus.ACTIVE);
        memory.setVersion(0);
        return memory;
    }

    private static MemoryRequest request(String memoryKey, String content) {
        return new MemoryRequest(null, null, MemoryType.LEARNING, memoryKey, content, 0.6f, null);
    }
}
