package com.ails.backend.service;

import com.ails.backend.common.BusinessException;
import com.ails.backend.dto.conversation.ConversationRequest;
import com.ails.backend.dto.conversation.ConversationResponse;
import com.ails.backend.entity.Conversation;
import com.ails.backend.enums.ConversationStatus;
import com.ails.backend.mapper.ConversationMapper;
import com.baomidou.mybatisplus.core.conditions.query.LambdaQueryWrapper;
import lombok.RequiredArgsConstructor;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.util.StringUtils;

import java.util.List;

/**
 * 对话管理：空间下的创建/列表/详情/归档/删除，带归属校验。
 */
@Service
@RequiredArgsConstructor
public class ConversationService {

    private final ConversationMapper conversationMapper;
    private final LearningSpaceService spaceService;

    @Transactional(readOnly = true)
    public List<ConversationResponse> listBySpace(Long userId, Long spaceId) {
        spaceService.ownedSpace(userId, spaceId);
        return conversationMapper.selectList(new LambdaQueryWrapper<Conversation>()
                        .eq(Conversation::getUserId, userId)
                        .eq(Conversation::getLearningSpaceId, spaceId)
                        .orderByDesc(Conversation::getCreatedAt))
                .stream().map(ConversationResponse::from).toList();
    }

    @Transactional
    public ConversationResponse createInSpace(Long userId, Long spaceId, ConversationRequest request) {
        spaceService.ownedSpace(userId, spaceId);
        Conversation conversation = new Conversation();
        conversation.setUserId(userId);
        conversation.setLearningSpaceId(spaceId);
        conversation.setTitle(StringUtils.hasText(request.title()) ? request.title().trim() : null);
        conversation.setStatus(ConversationStatus.ACTIVE);
        conversationMapper.insert(conversation);
        return ConversationResponse.from(conversation);
    }

    @Transactional(readOnly = true)
    public ConversationResponse get(Long userId, Long conversationId) {
        return ConversationResponse.from(ownedConversation(userId, conversationId));
    }

    @Transactional
    public ConversationResponse updateTitle(Long userId, Long conversationId, ConversationRequest request) {
        Conversation conversation = ownedConversation(userId, conversationId);
        if (StringUtils.hasText(request.title())) {
            conversation.setTitle(request.title().trim());
        }
        conversationMapper.updateById(conversation);
        return ConversationResponse.from(conversation);
    }

    @Transactional
    public ConversationResponse archive(Long userId, Long conversationId) {
        Conversation conversation = ownedConversation(userId, conversationId);
        conversation.setStatus(ConversationStatus.ARCHIVED);
        conversationMapper.updateById(conversation);
        return ConversationResponse.from(conversation);
    }

    @Transactional
    public void delete(Long userId, Long conversationId) {
        ownedConversation(userId, conversationId);
        // 软删：MyBatis-Plus 的 @TableLogic 会把 deleteById 变成 UPDATE deleted_at = now()。
        // 消息与证据都保留 —— 对话只是学习历史的容器，删掉它不该毁掉证据链。
        conversationMapper.deleteById(conversationId);
    }

    /**
     * 取当前用户拥有的对话并<b>锁定该行</b>，供聊天预创建事务使用。
     * <p>
     * 与 {@link #ownedConversation} 的区别：
     * <ul>
     *   <li>使用 {@code SELECT ... FOR UPDATE}，必须在外层事务中调用，锁持有到事务结束；</li>
     *   <li>读到的是最新已提交值（当前读），因此能看到并发事务刚写入的 {@code session_id}。</li>
     * </ul>
     * 同一对话的 append 由此串行化；不同对话锁不同主键行，互不阻塞。
     */
    public Conversation ownedConversationForUpdate(Long userId, Long conversationId) {
        Conversation conversation = conversationMapper.selectByIdForUpdate(conversationId);
        if (conversation == null || !conversation.getUserId().equals(userId)) {
            throw BusinessException.notFound("对话不存在");
        }
        spaceService.ownedSpace(userId, conversation.getLearningSpaceId());
        return conversation;
    }

    /**
     * 取当前用户拥有的对话，否则视为不存在。
     * <p>
     * 除了对话本身的归属，还要确认它所在的学习空间仍然存在：空间被软删之后，
     * 它下面的知识点会随之一并 404（{@code ConceptService.requireOwnedSpace} 会查空间），
     * 如果对话还能照常读写，「删除空间」就变成了两套语义 —— 一半的东西消失了，
     * 另一半还在，而且没有任何入口能把它们恢复回来。
     */
    public Conversation ownedConversation(Long userId, Long conversationId) {
        Conversation conversation = conversationMapper.selectById(conversationId);
        if (conversation == null || !conversation.getUserId().equals(userId)) {
            throw BusinessException.notFound("对话不存在");
        }
        spaceService.ownedSpace(userId, conversation.getLearningSpaceId());
        return conversation;
    }
}
