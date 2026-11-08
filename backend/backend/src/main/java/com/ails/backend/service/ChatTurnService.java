package com.ails.backend.service;

import com.ails.backend.entity.AgentRun;
import com.ails.backend.entity.Conversation;
import com.ails.backend.entity.Message;
import com.ails.backend.enums.AgentRunStatus;
import com.ails.backend.enums.AgentRunType;
import com.ails.backend.mapper.AgentRunMapper;
import com.ails.backend.mapper.MessageMapper;
import com.ails.backend.utils.TraceIds;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.util.Optional;

/**
 * 一轮对话的「预创建事务」：USER 消息、Session 归属、CHAT run 三件事同进同退。
 * <p>
 * <b>为什么需要这个类：</b>旧实现里 {@code ChatService.stream()} 先
 * {@code appendUserMessage}（独立事务提交），再 {@code beginTurn}、{@code createRun}
 * ——三步之间没有任何事务包裹。若 run 创建失败，数据库里会留下一条 USER 消息但
 * 没有 run：客户端收到错误，消息列表却已经多了一条「没人回答的提问」，重试还会
 * 再插一条。本类用一个短事务把三步绑定在一起：
 * <ol>
 *   <li>{@code SELECT conversation ... FOR UPDATE} —— 锁住对话行。同一对话的
 *       append 从此刻起串行化；同时读到的是<b>最新已提交</b>的 {@code session_id}，
 *       避免并发首条消息各自创建重复 Session（见下）；</li>
 *   <li>落 USER 消息（带幂等键）；</li>
 *   <li>{@code beginTurn} 决定学习活动归属；</li>
 *   <li>插入 CHAT run（RUNNING）。</li>
 * </ol>
 * 任何一步失败，整个事务回滚：不会出现「USER 消息已存在、无 run」的新数据。
 * ASSISTANT 消息不属于这个事务 —— 它在流式生成结束后由 ChatService 独立写入。
 * <p>
 * <b>关于快照：</b>第 1 步的 {@code FOR UPDATE} 是当前读，不建立一致性快照。
 * 如果它等待了另一个事务提交，随后第一条普通 SELECT 建立的快照会包含对方的
 * 提交；因此 {@code beginTurn} 里的候选 Session 查询能看到并发事务刚创建的
 * Session，不会在 {@code conversation.session_id} 已非空的情况下又建一个。
 * <p>
 * <b>本类不负责：</b>生成、SSE、收尾。它只准备「这一轮已经开始了」所需的持久化状态；
 * 生成侧的 run 终态仍由 {@code ChatService.finishRun} 写。
 */
@Slf4j
@Service
@RequiredArgsConstructor
public class ChatTurnService {

    private final ConversationService conversationService;
    private final MessageService messageService;
    private final LearningAnalysisService learningAnalysisService;
    private final AgentRunMapper agentRunMapper;
    private final MessageMapper messageMapper;

    /**
     * 按幂等键查找已经存在的一轮对话。只读，不产生任何写入。
     * <p>
     * USER 消息与 CHAT run 由 {@link #prepare} 在同一事务里写入，因此：
     * <ul>
     *   <li>查到 USER 消息时，run 要么已经提交，要么数据被外部改动过（状态 C）；</li>
     *   <li>查不到 USER 消息时，即使 run 存在（理论上不可能），也不把它当作命中，
     *       避免把一次全新的请求嫁接到一条无主 run 上。</li>
     * </ul>
     */
    @Transactional(readOnly = true)
    public Optional<TurnSnapshot> findExisting(Long conversationId, String clientRequestId) {
        Message userMessage = messageMapper.selectByConversationAndClientRequestId(
                conversationId, clientRequestId);
        if (userMessage == null) {
            return Optional.empty();
        }
        AgentRun run = agentRunMapper.selectByConversationAndClientRequestId(
                conversationId, clientRequestId);
        Message assistantMessage = null;
        if (run != null && run.getAssistantMessageId() != null) {
            // selectById 受 @TableLogic 约束：助手消息被软删时返回 null，
            // 调用方会把它当作「没有可回放的助手消息」，不会返回一个已不可见的消息 id。
            assistantMessage = messageMapper.selectById(run.getAssistantMessageId());
        }
        return Optional.of(new TurnSnapshot(userMessage, null, run, assistantMessage));
    }

    /**
     * 预创建一轮对话。事务边界见类注释；调用方必须在事务提交后才开始流式生成。
     *
     * @param conversation 请求线程上已经通过归属校验的对话，本方法会重新加锁读取
     * @param content      用户消息内容
     * @param clientRequestId 客户端幂等键
     */
    @Transactional
    public TurnSnapshot prepare(Conversation conversation, String content, String clientRequestId) {
        Conversation locked = conversationService.ownedConversationForUpdate(
                conversation.getUserId(), conversation.getId());
        Message userMessage = messageService.appendUserMessage(locked, content, clientRequestId);
        LearningAnalysisService.TurnOpening opening = learningAnalysisService.beginTurn(locked, content);
        AgentRun run = createRun(locked.getUserId(), locked.getId(), content,
                sessionId(opening), clientRequestId);
        return new TurnSnapshot(userMessage, opening, run, null);
    }

    /**
     * 本轮快照。
     *
     * @param opening          新请求才有值；回放时为 null
     * @param assistantMessage 仅当 run 已记录 assistantMessageId 且消息仍可见时才有值
     */
    public record TurnSnapshot(Message userMessage,
                               LearningAnalysisService.TurnOpening opening,
                               AgentRun run,
                               Message assistantMessage) {
    }

    private AgentRun createRun(Long userId, Long conversationId, String content,
                               Long sessionId, String clientRequestId) {
        AgentRun run = new AgentRun();
        run.setUserId(userId);
        run.setConversationId(conversationId);
        // 可能为 null：闲聊不产生也不归属任何 Session，见 docs/session-design.md §五。
        run.setSessionId(sessionId);
        run.setRunType(AgentRunType.CHAT);
        run.setStatus(AgentRunStatus.RUNNING);
        run.setInputSummary(truncate(content, 255));
        run.setTraceId(TraceIds.newId());
        run.setClientRequestId(clientRequestId);
        agentRunMapper.insert(run);
        return run;
    }

    private static Long sessionId(LearningAnalysisService.TurnOpening opening) {
        return opening == null || opening.session() == null ? null : opening.session().getId();
    }

    /** 把超长文本截到 max 长度（匹配 agent_run 摘要列宽）；null 原样返回。 */
    private static String truncate(String text, int max) {
        if (text == null) {
            return null;
        }
        return text.length() <= max ? text : text.substring(0, max);
    }
}
