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
import lombok.RequiredArgsConstructor;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.util.StringUtils;

import java.time.LocalDateTime;
import java.util.ArrayList;
import java.util.Collection;
import java.util.Collections;
import java.util.List;

/**
 * 消息的读写与对话内序号分配。
 * <p>
 * <b>关于 sequence_no：</b>不能写成 {@code SELECT MAX(sequence_no) + 1}。两个并发请求会读到
 * 同一个最大值、算出同一个序号，然后一起写进去。这里的做法是先在事务里
 * {@code SELECT ... FOR UPDATE} 锁住所属 conversation 那一行，再在锁内取最大序号 ——
 * 同一对话的消息写入因此被串行化，跨对话仍然并发。
 * <p>
 * <b>「锁内取最大序号」这一步本身也有讲究：它必须是锁定读，不能是普通 SELECT。</b>
 * 只加行锁、然后普通地查 MAX，看起来天经地义，实际是错的 —— 详见
 * {@link #nextSequenceNo}。数据库侧另有 {@code UNIQUE(conversation_id, sequence_no)}
 * 兜底，真撞上了会直接报错而不是静默错序。
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
     * 落一条嵌在对话里的题卡消息（{@code message_type = TEST}）。
     * <p>
     * content 是结构化 JSON（testId / conceptId / questionText…），由
     * {@link VerificationService} 生成。之所以仍然走 {@link #append}：
     * 它需要和普通消息共享同一套序号分配与 conversation.last_message_at 维护，
     * 否则前端按 sequence_no 排序时会看到一张「插队」的题卡。
     * <p>
     * 不校验对话归属 —— 调用方（分析链路）已经确认过这条对话属于该用户。
     */
    @Transactional
    public Message appendTestEvent(Long conversationId, String content) {
        Conversation conversation = conversationMapper.selectById(conversationId);
        if (conversation == null) {
            throw BusinessException.notFound("对话不存在");
        }
        return append(conversation, MessageRole.ASSISTANT, MessageType.TEST, content);
    }

    /**
     * 组装送给 Agent 的历史消息，按时间正序返回，最多 limit 条。
     * <p>
     * <b>必须传 excludeMessageId 把本轮提问排掉。</b>本轮用户消息在 {@link ChatService}
     * 里是<b>先落库</b>再取历史的（这样 {@code GET /messages} 立刻能看到它、序号也严格
     * 按请求顺序分配），所以「最近 limit 条」的最后一条必然就是它。若把它一起发给 Agent，
     * 本轮输入会在 {@code ChatAgentRequest.userMessage} 和 {@code history} 末尾各出现一次 ——
     * Agent 侧 `ContextBuilder._usable_history` 有一层内容比对的防御性去重兜着，
     * 但那是兜底：它按「内容相同」判断，用户连续发两条一样的话时反而会误删上一条。
     * 契约该由这里保证，不能让下游靠猜。
     * <p>
     * 排除写进 SQL 而不是查回来在内存里滤：LIMIT 作用于过滤<b>之后</b>，
     * 这样 limit=20 拿到的是 20 条真实历史，而不是 19 条历史 + 1 条重复。
     * <p>
     * <b>只取 TEXT。</b> TEST 消息的内容是给前端渲染的题卡 JSON，EVENT 是状态事件；
     * 把它们当自然语言塞进 prompt，模型会看到一段没人说过的 JSON。
     *
     * @param excludeMessageId 本轮用户消息 id，可为 null（纯回放场景）
     */
    @Transactional(readOnly = true)
    public List<ChatHistoryMessage> history(Long conversationId, Long excludeMessageId, int limit) {
        return history(conversationId,
                excludeMessageId == null ? List.of() : List.of(excludeMessageId), limit);
    }

    /**
     * 同时排除多条消息的历史。
     * <p>
     * 学习分析发生在助手回复<b>落库之后</b>，本轮的用户消息与助手消息都已经在表里，
     * 而两者都通过 {@code AnalyzeAgentRequest} 的独立字段传给了 Agent。历史里再出现一次，
     * 就是在 prompt 里把同一轮对话说了两遍。
     */
    @Transactional(readOnly = true)
    public List<ChatHistoryMessage> history(Long conversationId, Collection<Long> excludeMessageIds, int limit) {
        LambdaQueryWrapper<Message> query = new LambdaQueryWrapper<Message>()
                .eq(Message::getConversationId, conversationId)
                .eq(Message::getMessageType, MessageType.TEXT)
                .orderByDesc(Message::getSequenceNo)
                .orderByDesc(Message::getId)
                .last("LIMIT " + Math.max(1, limit));
        if (excludeMessageIds != null && !excludeMessageIds.isEmpty()) {
            query.notIn(Message::getId, excludeMessageIds);
        }
        List<Message> chronological = new ArrayList<>(messageMapper.selectList(query));
        // 上面按序号倒序取的「最近 limit 条」，翻正成时间正序 —— Agent 侧约定最后一个元素是最新一条
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
     * 两步，缺一不可：
     * <ol>
     *   <li>{@code FOR UPDATE} 锁住 conversation 行，把同一对话的并发写入串行化
     *       （跨对话仍然并发）；</li>
     *   <li>在锁内取当前最大序号 —— 这一步也必须是<b>锁定读</b>，
     *       见 {@link MessageMapper#selectMaxSequenceNoForUpdate}：
     *       普通 SELECT 读的是事务开始时钉下的快照，拿锁也救不回来。</li>
     * </ol>
     * <p>
     * 这里踩过一次：只做了第 1 步、第 2 步用的是 {@code selectObjs}，同一对话并发发 20 条消息时
     * 一半直接 500（{@code Duplicate entry '7-3' for key 'message.uk_msg_conv_seq'}）。
     * 原因是调用方在走到这里之前已经普通读过了 conversation 与 learning_space，
     * 事务的快照在那时就定住了 —— 行锁确实把两个事务排成了一队，
     * 但后进去的那个看到的仍是「对方插入之前」的 MAX。
     * <p>
     * 数据库侧 {@code UNIQUE(conversation_id, sequence_no)} 仍然留着兜底：真撞上会直接报错，
     * 而不是静默错序。但它是最后一道防线，不该指望它来保证正确性。
     */
    private int nextSequenceNo(Conversation conversation) {
        Conversation locked = conversationMapper.selectOne(new LambdaQueryWrapper<Conversation>()
                .eq(Conversation::getId, conversation.getId())
                .last("FOR UPDATE"));
        if (locked == null) {
            throw BusinessException.notFound("对话不存在");
        }
        Integer max = messageMapper.selectMaxSequenceNoForUpdate(conversation.getId());
        return (max == null ? 0 : max) + 1;
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
