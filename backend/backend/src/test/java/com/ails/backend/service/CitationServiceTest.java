package com.ails.backend.service;

import com.ails.backend.dto.conversation.CitationMetadata;
import com.ails.backend.dto.conversation.CitationResponse;
import com.ails.backend.entity.Document;
import com.ails.backend.entity.DocumentChunk;
import com.ails.backend.entity.Message;
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
import static org.mockito.Mockito.verifyNoInteractions;
import static org.mockito.Mockito.when;

/**
 * 引用持久化与历史可用性判定。
 * <p>
 * 可用性<b>不</b>在写入时固化：文档可能在之后被软删、chunk 可能在重新索引时被清理。
 * 所以这里固定的是「读取时按当时状态重新判断」这一条，而不是某个布尔值曾经写过什么。
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
                new CitationMetadata(DOCUMENT_ID, CHUNK_ID, 0, "zenframe.md"));

        String json = citationService.serialize(citations);

        assertThat(json).contains("\"documentId\":10")
                .contains("\"chunkId\":5")
                .contains("\"chunkIndex\":0")
                .contains("\"sourceTitle\":\"zenframe.md\"");
        assertThat(citationService.parse(json)).isEqualTo(citations);
        assertThat(citationService.serialize(List.of())).isNull();
        assertThat(citationService.serialize(null)).isNull();
        assertThat(citationService.parse(null)).isEmpty();
        assertThat(citationService.parse("  ")).isEmpty();
    }

    @Test
    @DisplayName("损坏的 JSON 只让引用为空，不让整条消息列表失败")
    void corruptJsonDegradesToEmpty() {
        assertThat(citationService.parse("{not json")).isEmpty();
        assertThat(citationService.parse("{\"documentId\":10}")).isEmpty();
    }

    @Test
    @DisplayName("文档与 chunk 都在且属于当前用户：available=true")
    void resolvesAvailableCitation() {
        when(documentMapper.selectBatchIds(any())).thenReturn(List.of(document(DOCUMENT_ID, USER_ID)));
        when(chunkMapper.selectBatchIds(any())).thenReturn(List.of(chunk(CHUNK_ID, DOCUMENT_ID)));

        Map<Long, List<CitationResponse>> result = citationService.resolveForMessages(
                USER_ID, List.of(message(MESSAGE_ID, DOCUMENT_ID, CHUNK_ID, 0, "zenframe.md")));

        assertThat(result.get(MESSAGE_ID)).containsExactly(
                new CitationResponse(DOCUMENT_ID, CHUNK_ID, 0, "zenframe.md", true));
    }

    @Test
    @DisplayName("文档已软删（查询不出来）：available=false，但仍保留标题快照")
    void deletedDocumentIsUnavailable() {
        // @TableLogic 会在 SQL 层排除软删行，这里用空结果模拟「文档已删」
        when(documentMapper.selectBatchIds(any())).thenReturn(List.of());

        Map<Long, List<CitationResponse>> result = citationService.resolveForMessages(
                USER_ID, List.of(message(MESSAGE_ID, DOCUMENT_ID, CHUNK_ID, 0, "zenframe.md")));

        assertThat(result.get(MESSAGE_ID)).containsExactly(
                new CitationResponse(DOCUMENT_ID, CHUNK_ID, 0, "zenframe.md", false));
    }

    @Test
    @DisplayName("chunk 已被重新索引清理：available=false，不会生成指向空片段的链接")
    void missingChunkIsUnavailable() {
        when(documentMapper.selectBatchIds(any())).thenReturn(List.of(document(DOCUMENT_ID, USER_ID)));
        when(chunkMapper.selectBatchIds(any())).thenReturn(List.of());

        Map<Long, List<CitationResponse>> result = citationService.resolveForMessages(
                USER_ID, List.of(message(MESSAGE_ID, DOCUMENT_ID, CHUNK_ID, 0, "zenframe.md")));

        assertThat(result.get(MESSAGE_ID).get(0).available()).isFalse();
    }

    @Test
    @DisplayName("文档属于其他用户：引用不可用，不能借历史消息绕过文档权限")
    void otherUsersDocumentIsUnavailable() {
        when(documentMapper.selectBatchIds(any()))
                .thenReturn(List.of(document(DOCUMENT_ID, 2L)));
        when(chunkMapper.selectBatchIds(any())).thenReturn(List.of(chunk(CHUNK_ID, DOCUMENT_ID)));

        Map<Long, List<CitationResponse>> result = citationService.resolveForMessages(
                USER_ID, List.of(message(MESSAGE_ID, DOCUMENT_ID, CHUNK_ID, 0, "别人的资料.md")));

        assertThat(result.get(MESSAGE_ID)).containsExactly(
                new CitationResponse(DOCUMENT_ID, CHUNK_ID, 0, "别人的资料.md", false));
    }

    @Test
    @DisplayName("chunk 属于另一篇文档：身份对不上，available=false")
    void chunkFromAnotherDocumentIsUnavailable() {
        when(documentMapper.selectBatchIds(any())).thenReturn(List.of(document(DOCUMENT_ID, USER_ID)));
        when(chunkMapper.selectBatchIds(any())).thenReturn(List.of(chunk(CHUNK_ID, 99L)));

        Map<Long, List<CitationResponse>> result = citationService.resolveForMessages(
                USER_ID, List.of(message(MESSAGE_ID, DOCUMENT_ID, CHUNK_ID, 0, "zenframe.md")));

        assertThat(result.get(MESSAGE_ID).get(0).available()).isFalse();
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

    private static Message message(long messageId, long documentId, long chunkId,
                                   Integer chunkIndex, String title) {
        Message message = new Message();
        message.setId(messageId);
        message.setCitations(JsonUtil.toJson(
                List.of(new CitationMetadata(documentId, chunkId, chunkIndex, title))));
        return message;
    }

    private static Document document(long id, long userId) {
        Document document = new Document();
        document.setId(id);
        document.setUserId(userId);
        return document;
    }

    private static DocumentChunk chunk(long id, long documentId) {
        DocumentChunk chunk = new DocumentChunk();
        chunk.setId(id);
        chunk.setDocumentId(documentId);
        return chunk;
    }
}
