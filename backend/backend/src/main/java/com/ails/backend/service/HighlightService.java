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
 * 高亮标记：仅允许在属于自己的对话消息上做标记。
 */
@Service
@RequiredArgsConstructor
public class HighlightService {

    private final HighlightMapper highlightMapper;
    private final MessageMapper messageMapper;
    private final ConversationMapper conversationMapper;

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
        highlight.setType(request.type());
        highlight.setSelectedText(request.selectedText());
        highlight.setNote(request.note());
        highlightMapper.insert(highlight);

        // TODO(M4): 高亮(尤其 MY_UNDERSTANDING)可作为证据交给 Learning-State Agent
        return HighlightResponse.from(highlight);
    }

    @Transactional(readOnly = true)
    public List<HighlightResponse> listMine(Long userId) {
        return highlightMapper.selectList(new LambdaQueryWrapper<Highlight>()
                        .eq(Highlight::getUserId, userId)
                        .orderByDesc(Highlight::getCreatedAt))
                .stream().map(HighlightResponse::from).toList();
    }
}
