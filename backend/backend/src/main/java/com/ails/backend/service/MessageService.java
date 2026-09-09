package com.ails.backend.service;

import com.ails.backend.common.BusinessException;
import com.ails.backend.dto.conversation.MessageRequest;
import com.ails.backend.dto.conversation.MessageResponse;
import com.ails.backend.entity.Conversation;
import com.ails.backend.entity.Message;
import com.ails.backend.enums.ConversationStatus;
import com.ails.backend.enums.MessageRole;
import com.ails.backend.mapper.MessageMapper;
import com.baomidou.mybatisplus.core.conditions.query.LambdaQueryWrapper;
import lombok.RequiredArgsConstructor;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.util.List;

/**
 * 消息管理（M1 仅做 CRUD 落库）。
 * TODO(M3): 用户消息落库后需调用 Python Tutor Agent 生成 ASSISTANT 回复，
 * 并通过 SSE 流式推送回 Flutter。
 */
@Service
@RequiredArgsConstructor
public class MessageService {

    private final MessageMapper messageMapper;
    private final ConversationService conversationService;

    @Transactional(readOnly = true)
    public List<MessageResponse> listByConversation(Long userId, Long conversationId) {
        conversationService.ownedConversation(userId, conversationId);
        return messageMapper.selectList(new LambdaQueryWrapper<Message>()
                        .eq(Message::getConversationId, conversationId)
                        .orderByAsc(Message::getCreatedAt)
                        .orderByAsc(Message::getId))
                .stream().map(MessageResponse::from).toList();
    }

    @Transactional
    public MessageResponse sendUserMessage(Long userId, Long conversationId, MessageRequest request) {
        Conversation conversation = conversationService.ownedConversation(userId, conversationId);
        if (conversation.getStatus() != ConversationStatus.ACTIVE) {
            throw new BusinessException(400, "对话已归档，无法发送消息");
        }
        Message message = new Message();
        message.setConversationId(conversationId);
        message.setRole(MessageRole.USER);
        message.setContent(request.content());
        message.setTokens(estimateTokens(request.content()));
        messageMapper.insert(message);

        // TODO(M3): 异步调用 Tutor Agent，将 ASSISTANT 回复以 SSE 推送给前端
        return MessageResponse.from(message);
    }

    /** 粗略 token 估算（正式接入 LLM 前仅用于占位展示） */
    private int estimateTokens(String content) {
        if (content == null || content.isEmpty()) {
            return 0;
        }
        // 中文按每字 1 token、其余按约 4 字符 1 token 粗略估算
        int cn = (int) content.chars().filter(c -> c > 0x4E00 && c < 0x9FFF).count();
        int other = content.length() - cn;
        return Math.max(1, cn + other / 4);
    }
}
