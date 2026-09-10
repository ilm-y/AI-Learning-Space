package com.ails.backend.service;

import com.ails.backend.common.BusinessException;
import com.ails.backend.dto.highlight.HighlightRequest;
import com.ails.backend.dto.highlight.HighlightResponse;
import com.ails.backend.entity.Conversation;
import com.ails.backend.entity.Highlight;
import com.ails.backend.entity.Message;
import com.ails.backend.mapper.ConversationMapper;
import com.ails.backend.mapper.HighlightMapper;
import com.ails.backend.mapper.MessageMapper;
import com.baomidou.mybatisplus.core.conditions.query.LambdaQueryWrapper;
import lombok.RequiredArgsConstructor;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.util.List;

/**
 * 高亮标记：用户在 AI 回复上标记重点 / 疑问 / 自己的理解。
 * <p>
 * 批注是仅次于测试的强信号来源 —— 用户主动写下的「我的理解」比系统从他的闲聊里
 * 猜测要可靠得多。因此创建批注后会立刻跑一次学习分析，让
 * Evidence → Understanding → Revision → LearningState 这条链路同步推进，
 * 前端 POST 完再 GET understanding 就能看到结果。
 * <p>
 * 哪类批注产生证据、产生什么样的证据，由 {@link LearningAnalysisService} 决定；
 * 本类只负责归属校验与落库。
 */
@Service
@RequiredArgsConstructor
public class HighlightService {

    private final HighlightMapper highlightMapper;
    private final MessageMapper messageMapper;
    private final ConversationMapper conversationMapper;
    private final LearningAnalysisService learningAnalysisService;

    @Transactional
    public HighlightResponse create(Long userId, Long messageId, HighlightRequest request) {
        Message message = messageMapper.selectById(messageId);
        if (message == null) {
            throw BusinessException.notFound("消息不存在");
        }
        Conversation conversation = conversationMapper.selectById(message.getConversationId());
        if (conversation == null || !conversation.getUserId().equals(userId)) {
            throw BusinessException.forbidden("不能对不属于自己的消息做标记");
        }

        Highlight highlight = new Highlight();
        highlight.setUserId(userId);
        highlight.setMessageId(messageId);
        highlight.setMarkType(request.markType());
        highlight.setSelectedText(request.selectedText());
        highlight.setStartOffset(request.startOffset());
        highlight.setEndOffset(request.endOffset());
        highlight.setNote(request.note());
        highlightMapper.insert(highlight);

        // 批注 → 证据 → 理解 → 投影（IMPORTANT / CONCEPT_LINK 不产生证据，此处为空操作）
        learningAnalysisService.analyzeHighlight(highlight, message);

        return HighlightResponse.from(highlight);
    }

    @Transactional(readOnly = true)
    public List<HighlightResponse> listMine(Long userId) {
        return highlightMapper.selectList(new LambdaQueryWrapper<Highlight>()
                        .eq(Highlight::getUserId, userId)
                        .orderByDesc(Highlight::getCreatedAt))
                .stream().map(HighlightResponse::from).toList();
    }

    @Transactional(readOnly = true)
    public List<HighlightResponse> listByMessage(Long userId, Long messageId) {
        return highlightMapper.selectList(new LambdaQueryWrapper<Highlight>()
                        .eq(Highlight::getUserId, userId)
                        .eq(Highlight::getMessageId, messageId)
                        .orderByAsc(Highlight::getStartOffset)
                        .orderByAsc(Highlight::getId))
                .stream().map(HighlightResponse::from).toList();
    }
}
