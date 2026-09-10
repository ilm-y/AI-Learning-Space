package com.ails.backend.service;

import com.ails.backend.common.BusinessException;
import com.ails.backend.dto.agent.ChatHistoryMessage;
import com.ails.backend.dto.conversation.MessageResponse;
import com.ails.backend.entity.Conversation;
import com.ails.backend.entity.Message;
import com.ails.backend.enums.ConversationStatus;
import com.ails.backend.enums.MessageRole;
import com.ails.backend.enums.MessageType;
import com.ails.backend.mapper.ConversationMapper;
import com.ails.backend.mapper.MessageMapper;
import com.baomidou.mybatisplus.core.conditions.query.LambdaQueryWrapper;
import com.baomidou.mybatisplus.core.conditions.query.QueryWrapper;
import lombok.RequiredArgsConstructor;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.util.StringUtils;

import java.time.LocalDateTime;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;

/**
 * 消息的读写与对话内序号分配。
 * <p>
 * <b>关于 sequence_no：</b>不能写成 {@code SELECT MAX(sequence_no) + 1}。两个并发请求会读到
 * 同一个最大值、算出同一个序号，然后一起写进去。这里的做法是先在事务里
 * {@code SELECT ... FOR UPDATE} 锁住所属 conversation 那一行，再算下一个序号 ——
 * 同一对话的消息写入因此被串行化，跨对话仍然并发。数据库侧另有
 * {@code UNIQUE(conversation_id, sequence_no)} 兜底，真撞上了会直接报错而不是静默错序。
 */
@Service
@RequiredArgsConstructor
public class MessageService {

    private final MessageMapper messageMapper;
    private final ConversationMapper conversationMapper;
    private final ConversationService conversationService;

    @Transactional(readOnly = true)
    public List<MessageResponse> listByConversation(Long userId, Long conversationId) {
        conversationService.ownedConversation(userId, conversationId);
        return messageMapper.selectList(new LambdaQueryWrapper<Message>()
                        .eq(Message::getConversationId, conversationId)
                        .orderByAsc(Message::getSequenceNo)
                        .orderByAsc(Message::getId))
                .stream().map(MessageResponse::from).toList();
    }

    /**
     * 落一条用户消息。返回的实体已带 sequenceNo。
     */
    @Transactional
    public Message appendUserMessage(Long userId, Long conversationId, String content) {
        Conversation conversation = requireActiveConversation(userId, conversationId);
        return append(conversation, MessageRole.USER, MessageType.TEXT, content);
    }

    /**
     * 落一条助手消息。
     * <p>
     * 这里不校验归属：调用方（ChatService）已经在自己那条请求里校验过对话所有权。
     */
    @Transactional
    public Message appendAssistantMessage(Long conversationId, String content) {
        Conversation conversation = conversationMapper.selectById(conversationId);
        if (conversation == null) {
            throw BusinessException.notFound("对话不存在");
        }
        return append(conversation, MessageRole.ASSISTANT, MessageType.TEXT, content);
    }

    /**
     * 组装送给 Agent 的历史消息（不含刚写入的这条）。只取最近 limit 条，按时间正序返回。
     */
    @Transactional(readOnly = true)
    public List<ChatHistoryMessage> history(Long conversationId, int limit) {
        List<Message> recent = messageMapper.selectList(new LambdaQueryWrapper<Message>()
                .eq(Message::getConversationId, conversationId)
                .orderByDesc(Message::getSequenceNo)
                .orderByDesc(Message::getId)
                .last("LIMIT " + Math.max(1, limit)));
        List<Message> chronological = new ArrayList<>(recent);
        Collections.reverse(chronological);
        return chronological.stream()
                .map(m -> new ChatHistoryMessage(m.getRole(), m.getContent()))
                .toList();
    }

    // ================= 内部实现 =================

    private Message append(Conversation conversation, MessageRole role, MessageType type, String content) {
        Message message = new Message();
        message.setConversationId(conversation.getId());
        message.setRole(role);
        message.setMessageType(type);
        message.setContent(content);
        message.setTokens(estimateTokens(content));
        message.setSequenceNo(nextSequenceNo(conversation));
        messageMapper.insert(message);

        conversation.setLastMessageAt(LocalDateTime.now());
        conversationMapper.updateById(conversation);
        return message;
    }

    /**
     * 取对话内下一个序号。
     * <p>
     * 先 {@code FOR UPDATE} 锁住 conversation 行，把同一对话的并发写入串行化，再取当前最大值。
     */
    private int nextSequenceNo(Conversation conversation) {
        Conversation locked = conversationMapper.selectOne(new LambdaQueryWrapper<Conversation>()
                .eq(Conversation::getId, conversation.getId())
                .last("FOR UPDATE"));
        if (locked == null) {
            throw BusinessException.notFound("对话不存在");
        }
        List<Object> maxSeq = messageMapper.selectObjs(new QueryWrapper<Message>()
                .select("COALESCE(MAX(sequence_no), 0)")
                .eq("conversation_id", conversation.getId()));
        Object value = maxSeq.isEmpty() ? null : maxSeq.get(0);
        int current = value instanceof Number number ? number.intValue() : 0;
        return current + 1;
    }

    private Conversation requireActiveConversation(Long userId, Long conversationId) {
        Conversation conversation = conversationService.ownedConversation(userId, conversationId);
        if (conversation.getStatus() != ConversationStatus.ACTIVE) {
            throw new BusinessException(400, "对话已归档，无法发送消息");
        }
        return conversation;
    }

    /** 粗略 token 估算（正式接入 LLM 前仅用于占位展示） */
    private int estimateTokens(String content) {
        if (!StringUtils.hasText(content)) {
            return 0;
        }
        // 中文按每字 1 token、其余按约 4 字符 1 token 粗略估算
        int cn = (int) content.chars().filter(c -> c > 0x4E00 && c < 0x9FFF).count();
        int other = content.length() - cn;
        return Math.max(1, cn + other / 4);
    }
}
