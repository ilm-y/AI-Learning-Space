package com.ails.backend.service;

import com.ails.backend.dto.document.VersionCollection;
import com.ails.backend.entity.Document;
import com.ails.backend.mapper.DocumentIndexVersionMapper;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;

import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatIllegalStateException;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyInt;
import static org.mockito.ArgumentMatchers.anyLong;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.doThrow;
import static org.mockito.Mockito.inOrder;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.verifyNoInteractions;
import static org.mockito.Mockito.when;

/**
 * RAG-02：旧版本清理必须“按每个版本自己的 collection”删除。
 *
 * <p>核心断言不是“新 collection 里没有旧点”，而是旧点确实从旧 collection 被删除。</p>
 */
@ExtendWith(MockitoExtension.class)
class DocumentIndexVersionServiceTest {

    @Mock
    private DocumentIndexVersionMapper versionMapper;

    @Mock
    private DocumentChunkService chunkService;

    @Mock
    private AgentDocumentClient agentClient;

    private DocumentIndexVersionService service;

    @BeforeEach
    void setUp() {
        service = new DocumentIndexVersionService(versionMapper, chunkService, agentClient);
    }

    @Test
    void deletesEachStaleVersionFromItsOwnCollection() {
        Document document = document(3);
        when(versionMapper.selectByVersions(42L, List.of(1, 2))).thenReturn(List.of(
                new VersionCollection(1, "collection-a"),
                new VersionCollection(2, "collection-b")));

        List<Integer> cleaned = service.purgeStaleVersions(document, List.of(1, 2));

        // 旧版本 1 与 2 分别来自两个不同 collection，必须各删一次，且不能使用
        // 活跃版本的 collection（document.vector_collection = "collection-active"）。
        verify(agentClient).deleteVersions(document, List.of(1), false, "collection-a");
        verify(agentClient).deleteVersions(document, List.of(2), false, "collection-b");
        verify(agentClient, never()).deleteVersions(any(), any(), eq(false), eq("collection-active"));
        assertThat(cleaned).containsExactlyInAnyOrder(1, 2);
    }

    @Test
    void groupsVersionsSharingOneCollectionIntoASingleDelete() {
        Document document = document(3);
        when(versionMapper.selectByVersions(42L, List.of(1, 2))).thenReturn(List.of(
                new VersionCollection(1, "same-collection"),
                new VersionCollection(2, "same-collection")));

        service.purgeStaleVersions(document, List.of(1, 2));

        verify(agentClient).deleteVersions(document, List.of(1, 2), false, "same-collection");
    }

    @Test
    void versionZeroUsesLegacyFlagWhenCollectionWasRecorded() {
        Document document = document(1);
        when(versionMapper.selectByVersions(42L, List.of(0))).thenReturn(List.of(
                new VersionCollection(0, "legacy-collection")));

        service.purgeStaleVersions(document, List.of(0));

        // 版本 0 的 points 没有 index_version 字段，删除时必须带 deleteLegacy=true。
        verify(agentClient).deleteVersions(document, List.of(0), true, "legacy-collection");
    }

    @Test
    void legacyVersionWithoutRecordIsNeverGuessed() {
        Document document = document(1);
        // 迁移前写入、且没有版本记录：collection 不可定位。
        when(versionMapper.selectByVersions(42L, List.of(0))).thenReturn(List.of());

        List<Integer> cleaned = service.purgeStaleVersions(document, List.of(0));

        // 不猜测 collection，也不调用 Agent；MySQL 记录删除，Qdrant 残留由日志告警。
        verifyNoInteractions(agentClient);
        verify(chunkService).deleteVersions(42L, List.of(0));
        verify(versionMapper).deleteVersions(42L, List.of(0));
        assertThat(cleaned).containsExactly(0);
    }

    @Test
    void failureInOneCollectionKeepsItsRecordsButCleansTheOthers() {
        Document document = document(3);
        when(versionMapper.selectByVersions(42L, List.of(1, 2))).thenReturn(List.of(
                new VersionCollection(1, "collection-a"),
                new VersionCollection(2, "collection-b")));
        doThrow(new DocumentIndexException(503, "qdrant down"))
                .when(agentClient).deleteVersions(document, List.of(1), false, "collection-a");

        assertThatThrownBy(() -> service.purgeStaleVersions(document, List.of(1, 2)))
                .isInstanceOf(DocumentIndexException.class);

        // 成功的组照常清理；失败组保留 chunk 与版本记录，下一轮扫描还能重试。
        verify(chunkService).deleteVersions(42L, List.of(2));
        verify(versionMapper).deleteVersions(42L, List.of(2));
        verify(chunkService, never()).deleteVersions(42L, List.of(1));
        verify(versionMapper, never()).deleteVersions(42L, List.of(1));
    }

    @Test
    void allGroupsFailuresRetainAllLocationRecords() {
        Document document = document(3);
        when(versionMapper.selectByVersions(42L, List.of(1))).thenReturn(List.of(
                new VersionCollection(1, "collection-a")));
        doThrow(new DocumentIndexException(503, "qdrant down"))
                .when(agentClient).deleteVersions(document, List.of(1), false, "collection-a");

        assertThatThrownBy(() -> service.purgeStaleVersions(document, List.of(1)))
                .isInstanceOf(DocumentIndexException.class);

        verifyNoInteractions(chunkService);
        verify(versionMapper, never()).deleteVersions(anyLong(), any());
    }

    @Test
    void recordPersistsCollectionForEachVersion() {
        service.record(42L, 1, "collection-a");

        verify(versionMapper).upsert(42L, 1, "collection-a");
    }

    @Test
    void recordRejectsMissingLocation() {
        assertThatIllegalStateException().isThrownBy(() -> service.record(42L, 1, " "));
        assertThatIllegalStateException().isThrownBy(() -> service.record(null, 1, "c"));
        verify(versionMapper, never()).upsert(anyLong(), anyInt(), anyString());
    }

    @Test
    void findCollectionReturnsNullWhenVersionWasNeverRecorded() {
        when(versionMapper.selectCollection(42L, 7)).thenReturn(null);

        assertThat(service.findCollection(42L, 7)).isNull();
    }

    @Test
    void deleteByDocumentRemovesEveryVersionRow() {
        service.deleteByDocument(42L);

        verify(versionMapper).deleteByDocument(42L);
    }

    @Test
    void chunkAndVersionRecordsAreDeletedInOrderAfterVectors() {
        Document document = document(2);
        when(versionMapper.selectByVersions(42L, List.of(1))).thenReturn(List.of(
                new VersionCollection(1, "collection-a")));

        service.purgeStaleVersions(document, List.of(1));

        var ordered = inOrder(agentClient, chunkService, versionMapper);
        ordered.verify(agentClient).deleteVersions(document, List.of(1), false, "collection-a");
        ordered.verify(chunkService).deleteVersions(42L, List.of(1));
        ordered.verify(versionMapper).deleteVersions(42L, List.of(1));
    }

    private static Document document(int activeVersion) {
        Document document = new Document();
        document.setId(42L);
        document.setIndexVersion(activeVersion);
        // 活跃版本 collection 已切换到新值；旧版本清理不得使用它。
        document.setVectorCollection("collection-active");
        return document;
    }
}
