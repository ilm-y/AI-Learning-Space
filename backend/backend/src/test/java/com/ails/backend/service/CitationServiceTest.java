package com.ails.backend.service;

import com.ails.backend.dto.conversation.CitationMetadata;
import com.ails.backend.dto.conversation.CitationResponse;
import com.ails.backend.entity.Document;
import com.ails.backend.entity.DocumentChunk;
import com.ails.backend.entity.Message;
import com.ails.backend.enums.DocumentStatus;
import com.ails.backend.mapper.DocumentChunkMapper;
import com.ails.backend.mapper.DocumentMapper;
import com.ails.backend.utils.JsonUtil;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.times;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.verifyNoInteractions;
import static org.mockito.Mockito.when;

/**
 * 引用持久化、版本快照与历史可用性判定。
 * <p>
 * 可用性<b>不</b>在写入时固化：文档可能在之后被软删、chunk 可能在重新索引时被清理。
 * 所以这里固定的是「读取时按当时状态重新判断」这一条，而不是某个布尔值曾经写过什么。
 * <p>
 * 方案 C 的语义：available 只表示「当前 READY 且属于活跃版本」；旧版本用
 * isHistorical 表达「当时引用过」，不要求旧 chunk 被保留。
 */
class CitationServiceTest {

    private static final long USER_ID = 1L;
    private static final long MESSAGE_ID = 100L;
    private static final long DOCUMENT_ID = 10L;
    private static final long CHUNK_ID = 5L;

    private DocumentMapper documentMapper;
    private DocumentChunkMapper chunkMapper;
    private CitationService citationService;

    @BeforeEach
    void setUp() {
        documentMapper = mock(DocumentMapper.class);
        chunkMapper = mock(DocumentChunkMapper.class);
        citationService = new CitationService(documentMapper, chunkMapper);
    }

    @Test
    @DisplayName("序列化/解析往返：字段不丢失，空列表写 NULL")
    void serializesAndParses() {
        List<CitationMetadata> citations = List.of(
                new CitationMetadata(DOCUMENT_ID, CHUNK_ID, 0, "zenframe.md", 3));

        String json = citationService.serialize(citations);

        assertThat(json).contains("\"documentId\":10")
                .contains("\"chunkId\":5")
                .contains("\"chunkIndex\":0")
                .contains("\"sourceTitle\":\"zenframe.md\"")
                .contains("\"indexVersion\":3");
        assertThat(citationService.parse(json)).isEqualTo(citations);
        assertThat(citationService.serialize(List.of())).isNull();
        assertThat(citationService.serialize(null)).isNull();
        assertThat(citationService.parse(null)).isEmpty();
        assertThat(citationService.parse("  ")).isEmpty();
    }

    @Test
    @DisplayName("旧 JSON 没有 indexVersion：解析为 null，不报错")
    void parsesLegacyJsonWithoutIndexVersion() {
        List<CitationMetadata> parsed = citationService.parse(
                "[{\"documentId\":10,\"chunkId\":5,\"chunkIndex\":0,\"sourceTitle\":\"zenframe.md\"}]");

        assertThat(parsed).containsExactly(
                new CitationMetadata(DOCUMENT_ID, CHUNK_ID, 0, "zenframe.md"));
    }

    @Test
    @DisplayName("未知字段被忽略：后续扩展不会让旧版本解析失败")
    void ignoresUnknownJsonFields() {
        List<CitationMetadata> parsed = citationService.parse(
                "[{\"documentId\":10,\"chunkId\":5,\"chunkIndex\":0,"
                        + "\"sourceTitle\":\"zenframe.md\",\"futureField\":\"ignored\"}]");

        assertThat(parsed).containsExactly(
                new CitationMetadata(DOCUMENT_ID, CHUNK_ID, 0, "zenframe.md"));
    }

    @Test
    @DisplayName("损坏的 JSON 只让引用为空，不让整条消息列表失败")
    void corruptJsonDegradesToEmpty() {
        assertThat(citationService.parse("{not json")).isEmpty();
        assertThat(citationService.parse("{\"documentId\":10}")).isEmpty();
    }

    @Test
    @DisplayName("文档与 chunk 都在、文档 READY、版本一致：available=true，非历史")
    void resolvesAvailableCitation() {
        when(documentMapper.selectBatchIds(any()))
                .thenReturn(List.of(document(DOCUMENT_ID, USER_ID, DocumentStatus.READY, 0)));
        when(chunkMapper.selectBatchIds(any()))
                .thenReturn(List.of(chunk(CHUNK_ID, DOCUMENT_ID, 0)));

        Map<Long, List<CitationResponse>> result = citationService.resolveForMessages(
                USER_ID, List.of(message(MESSAGE_ID, DOCUMENT_ID, CHUNK_ID, 0, "zenframe.md")));

        assertThat(result.get(MESSAGE_ID)).containsExactly(
                new CitationResponse(DOCUMENT_ID, CHUNK_ID, 0, "zenframe.md", true, false));
    }

    @Test
    @DisplayName("文档已软删（查询不出来）：available=false，但仍保留标题快照")
    void deletedDocumentIsUnavailable() {
        // @TableLogic 会在 SQL 层排除软删行，这里用空结果模拟「文档已删」
        when(documentMapper.selectBatchIds(any())).thenReturn(List.of());

        Map<Long, List<CitationResponse>> result = citationService.resolveForMessages(
                USER_ID, List.of(message(MESSAGE_ID, DOCUMENT_ID, CHUNK_ID, 0, "zenframe.md")));

        assertThat(result.get(MESSAGE_ID)).containsExactly(
                new CitationResponse(DOCUMENT_ID, CHUNK_ID, 0, "zenframe.md", false, false));
    }

    @Test
    @DisplayName("chunk 已被重新索引清理：available=false，不会生成指向空片段的链接")
    void missingChunkIsUnavailable() {
        when(documentMapper.selectBatchIds(any()))
                .thenReturn(List.of(document(DOCUMENT_ID, USER_ID, DocumentStatus.READY, 0)));
        when(chunkMapper.selectBatchIds(any())).thenReturn(List.of());

        Map<Long, List<CitationResponse>> result = citationService.resolveForMessages(
                USER_ID, List.of(message(MESSAGE_ID, DOCUMENT_ID, CHUNK_ID, 0, "zenframe.md")));

        assertThat(result.get(MESSAGE_ID).get(0).available()).isFalse();
        assertThat(result.get(MESSAGE_ID).get(0).isHistorical()).isFalse();
    }

    @Test
    @DisplayName("文档属于其他用户：引用不可用，不能借历史消息绕过文档权限")
    void otherUsersDocumentIsUnavailable() {
        when(documentMapper.selectBatchIds(any()))
                .thenReturn(List.of(document(DOCUMENT_ID, 2L, DocumentStatus.READY, 0)));
        when(chunkMapper.selectBatchIds(any()))
                .thenReturn(List.of(chunk(CHUNK_ID, DOCUMENT_ID, 0)));

        Map<Long, List<CitationResponse>> result = citationService.resolveForMessages(
                USER_ID, List.of(message(MESSAGE_ID, DOCUMENT_ID, CHUNK_ID, 0, "别人的资料.md")));

        assertThat(result.get(MESSAGE_ID)).containsExactly(
                new CitationResponse(DOCUMENT_ID, CHUNK_ID, 0, "别人的资料.md", false, false));
    }

    @Test
    @DisplayName("chunk 属于另一篇文档：身份对不上，available=false")
    void chunkFromAnotherDocumentIsUnavailable() {
        when(documentMapper.selectBatchIds(any()))
                .thenReturn(List.of(document(DOCUMENT_ID, USER_ID, DocumentStatus.READY, 0)));
        when(chunkMapper.selectBatchIds(any()))
                .thenReturn(List.of(chunk(CHUNK_ID, 99L, 0)));

        Map<Long, List<CitationResponse>> result = citationService.resolveForMessages(
                USER_ID, List.of(message(MESSAGE_ID, DOCUMENT_ID, CHUNK_ID, 0, "zenframe.md")));

        assertThat(result.get(MESSAGE_ID).get(0).available()).isFalse();
        assertThat(result.get(MESSAGE_ID).get(0).isHistorical()).isFalse();
    }

    @Test
    @DisplayName("文档非 READY（PROCESSING）：available=false")
    void nonReadyDocumentIsUnavailable() {
        when(documentMapper.selectBatchIds(any()))
                .thenReturn(List.of(document(DOCUMENT_ID, USER_ID, DocumentStatus.PROCESSING, 0)));
        when(chunkMapper.selectBatchIds(any()))
                .thenReturn(List.of(chunk(CHUNK_ID, DOCUMENT_ID, 0)));

        List<CitationResponse> result = citationService.resolve(USER_ID,
                List.of(citation(DOCUMENT_ID, CHUNK_ID, 0)));

        assertThat(result).containsExactly(
                new CitationResponse(DOCUMENT_ID, CHUNK_ID, 0, "zenframe.md", false, false));
    }

    @Test
    @DisplayName("文档 DELETING：available=false（chunk 可能还没清理）")
    void deletingDocumentIsUnavailable() {
        when(documentMapper.selectBatchIds(any()))
                .thenReturn(List.of(document(DOCUMENT_ID, USER_ID, DocumentStatus.DELETING, 0)));
        when(chunkMapper.selectBatchIds(any()))
                .thenReturn(List.of(chunk(CHUNK_ID, DOCUMENT_ID, 0)));

        List<CitationResponse> result = citationService.resolve(USER_ID,
                List.of(citation(DOCUMENT_ID, CHUNK_ID, 0)));

        assertThat(result).containsExactly(
                new CitationResponse(DOCUMENT_ID, CHUNK_ID, 0, "zenframe.md", false, false));
    }

    @Test
    @DisplayName("chunk 版本与文档活跃版本不一致（且更新）：available=false，不是历史")
    void newerChunkVersionIsUnavailableButNotHistorical() {
        when(documentMapper.selectBatchIds(any()))
                .thenReturn(List.of(document(DOCUMENT_ID, USER_ID, DocumentStatus.READY, 1)));
        when(chunkMapper.selectBatchIds(any()))
                .thenReturn(List.of(chunk(CHUNK_ID, DOCUMENT_ID, 2)));

        List<CitationResponse> result = citationService.resolve(USER_ID,
                List.of(citation(DOCUMENT_ID, CHUNK_ID, 2)));

        assertThat(result).containsExactly(
                new CitationResponse(DOCUMENT_ID, CHUNK_ID, 0, "zenframe.md", false, false));
    }

    @Test
    @DisplayName("老版本 chunk 仍存在但非活跃：available=false + isHistorical=true")
    void inactiveOlderVersionIsHistorical() {
        when(documentMapper.selectBatchIds(any()))
                .thenReturn(List.of(document(DOCUMENT_ID, USER_ID, DocumentStatus.READY, 2)));
        when(chunkMapper.selectBatchIds(any()))
                .thenReturn(List.of(chunk(CHUNK_ID, DOCUMENT_ID, 1)));

        List<CitationResponse> result = citationService.resolve(USER_ID,
                List.of(citation(DOCUMENT_ID, CHUNK_ID, 1)));

        assertThat(result).containsExactly(
                new CitationResponse(DOCUMENT_ID, CHUNK_ID, 0, "zenframe.md", false, true));
    }

    @Test
    @DisplayName("indexVersion 为 null 的防御：活跃版本无法确认时不可用")
    void nullIndexVersionIsUnavailable() {
        when(documentMapper.selectBatchIds(any()))
                .thenReturn(List.of(document(DOCUMENT_ID, USER_ID, DocumentStatus.READY, 1)));
        when(chunkMapper.selectBatchIds(any()))
                .thenReturn(List.of(chunk(CHUNK_ID, DOCUMENT_ID, null)));

        List<CitationResponse> result = citationService.resolve(USER_ID,
                List.of(citation(DOCUMENT_ID, CHUNK_ID, 1)));

        assertThat(result).containsExactly(
                new CitationResponse(DOCUMENT_ID, CHUNK_ID, 0, "zenframe.md", false, false));
    }

    @Test
    @DisplayName("旧记录无快照、chunk 已清理：available=false + isHistorical=false（无法区分已删除）")
    void legacyRecordWithoutSnapshotAndMissingChunkIsNotHistorical() {
        when(documentMapper.selectBatchIds(any()))
                .thenReturn(List.of(document(DOCUMENT_ID, USER_ID, DocumentStatus.READY, 2)));
        when(chunkMapper.selectBatchIds(any())).thenReturn(List.of());

        List<CitationResponse> result = citationService.resolve(USER_ID,
                List.of(citation(DOCUMENT_ID, CHUNK_ID, null)));

        assertThat(result).containsExactly(
                new CitationResponse(DOCUMENT_ID, CHUNK_ID, 0, "zenframe.md", false, false));
    }

    @Test
    @DisplayName("旧记录无快照、chunk 仍在：用 chunk.indexVersion 兜底判断历史")
    void legacyRecordWithoutSnapshotUsesChunkVersion() {
        when(documentMapper.selectBatchIds(any()))
                .thenReturn(List.of(document(DOCUMENT_ID, USER_ID, DocumentStatus.READY, 2)));
        when(chunkMapper.selectBatchIds(any()))
                .thenReturn(List.of(chunk(CHUNK_ID, DOCUMENT_ID, 1)));

        List<CitationResponse> result = citationService.resolve(USER_ID,
                List.of(citation(DOCUMENT_ID, CHUNK_ID, null)));

        assertThat(result).containsExactly(
                new CitationResponse(DOCUMENT_ID, CHUNK_ID, 0, "zenframe.md", false, true));
    }

    @Test
    @DisplayName("READY 重建失败、活跃版本未变：旧引用仍是当前可用")
    void readyRebuildFailureKeepsActiveVersionAvailable() {
        when(documentMapper.selectBatchIds(any()))
                .thenReturn(List.of(document(DOCUMENT_ID, USER_ID, DocumentStatus.READY, 1)));
        when(chunkMapper.selectBatchIds(any()))
                .thenReturn(List.of(chunk(CHUNK_ID, DOCUMENT_ID, 1)));

        List<CitationResponse> result = citationService.resolve(USER_ID,
                List.of(citation(DOCUMENT_ID, CHUNK_ID, 1)));

        assertThat(result).containsExactly(
                new CitationResponse(DOCUMENT_ID, CHUNK_ID, 0, "zenframe.md", true, false));
    }

    @Test
    @DisplayName("resolve 批量查询：多条引用只查一次 document / chunk")
    void resolveBatchesLookups() {
        when(documentMapper.selectBatchIds(any()))
                .thenReturn(List.of(document(DOCUMENT_ID, USER_ID, DocumentStatus.READY, 0)));
        when(chunkMapper.selectBatchIds(any())).thenReturn(List.of(
                chunk(CHUNK_ID, DOCUMENT_ID, 0),
                chunk(CHUNK_ID + 1, DOCUMENT_ID, 0)));

        citationService.resolve(USER_ID, List.of(
                citation(DOCUMENT_ID, CHUNK_ID, 0),
                citation(DOCUMENT_ID, CHUNK_ID + 1, 0)));

        verify(documentMapper, times(1)).selectBatchIds(any());
        verify(chunkMapper, times(1)).selectBatchIds(any());
    }

    @Test
    @DisplayName("没有引用的消息不查库，也不出现在结果里（兼容功能上线前的历史消息）")
    void messagesWithoutCitationsSkipLookups() {
        Message message = new Message();
        message.setId(MESSAGE_ID);
        message.setCitations(null);

        Map<Long, List<CitationResponse>> result =
                citationService.resolveForMessages(USER_ID, List.of(message));

        assertThat(result).isEmpty();
        verifyNoInteractions(documentMapper, chunkMapper);
    }

    @Test
    @DisplayName("resolve 空列表不查库")
    void emptyResolveSkipsLookups() {
        assertThat(citationService.resolve(USER_ID, List.of())).isEmpty();

        verifyNoInteractions(documentMapper, chunkMapper);
    }

    @Test
    @DisplayName("stampIndexVersions：从现存 chunk 写入版本快照；查不到时保留 null")
    void stampsIndexVersionsFromChunks() {
        when(chunkMapper.selectBatchIds(any()))
                .thenReturn(List.of(chunk(CHUNK_ID, DOCUMENT_ID, 4)));

        List<CitationMetadata> stamped = citationService.stampIndexVersions(List.of(
                citation(DOCUMENT_ID, CHUNK_ID, null),
                new CitationMetadata(DOCUMENT_ID, CHUNK_ID + 1, 0, "missing.md")));

        assertThat(stamped).containsExactly(
                new CitationMetadata(DOCUMENT_ID, CHUNK_ID, 0, "zenframe.md", 4),
                new CitationMetadata(DOCUMENT_ID, CHUNK_ID + 1, 0, "missing.md", null));
        verify(chunkMapper, times(1)).selectBatchIds(any());
    }

    private static CitationMetadata citation(long documentId, long chunkId, Integer indexVersion) {
        return new CitationMetadata(documentId, chunkId, 0, "zenframe.md", indexVersion);
    }

    private static Message message(long messageId, long documentId, long chunkId,
                                   Integer chunkIndex, String title) {
        Message message = new Message();
        message.setId(messageId);
        message.setCitations(JsonUtil.toJson(
                List.of(new CitationMetadata(documentId, chunkId, chunkIndex, title))));
        return message;
    }

    private static Document document(long id, long userId, DocumentStatus status,
                                     Integer indexVersion) {
        Document document = new Document();
        document.setId(id);
        document.setUserId(userId);
        document.setStatus(status);
        document.setIndexVersion(indexVersion);
        return document;
    }

    private static DocumentChunk chunk(long id, long documentId, Integer indexVersion) {
        DocumentChunk chunk = new DocumentChunk();
        chunk.setId(id);
        chunk.setDocumentId(documentId);
        chunk.setIndexVersion(indexVersion);
        return chunk;
    }
}
