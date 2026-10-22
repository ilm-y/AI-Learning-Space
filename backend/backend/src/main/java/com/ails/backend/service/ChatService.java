package com.ails.backend.service;

import com.ails.backend.agent.AgentGateway;
import com.ails.backend.agent.AgentStreamHandler;
import com.ails.backend.common.BusinessException;
import com.ails.backend.dto.agent.ChatAgentRequest;
import com.ails.backend.entity.AgentRun;
import com.ails.backend.entity.Conversation;
import com.ails.backend.entity.LearningSession;
import com.ails.backend.entity.Message;
import com.ails.backend.enums.AgentRunStatus;
import com.ails.backend.enums.AgentRunType;
import com.ails.backend.enums.ConversationStatus;
import com.ails.backend.mapper.AgentRunMapper;
import com.ails.backend.utils.TraceIds;
import com.baomidou.mybatisplus.core.conditions.update.LambdaUpdateWrapper;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.core.task.TaskExecutor;
import org.springframework.http.MediaType;
import org.springframework.stereotype.Service;
import org.springframework.web.servlet.mvc.method.annotation.SseEmitter;

import java.io.IOException;
import java.time.Duration;
import java.time.LocalDateTime;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.concurrent.RejectedExecutionException;
import java.util.concurrent.atomic.AtomicBoolean;

/**
 * 流式对话 —— 夹在「前端（SSE 客户端）」和「Python Tutor Agent」中间的那一层，
 * 负责 收消息 → 调 Agent → 流式吐回复 → 学习闭环 的整条链路。
 * <p>
 * <h3>一次发消息请求的端到端流程</h3>
 * <ol>
 *   <li>前端 POST {@code /api/conversations/{id}/messages}，body 只有 {@code {content}}；</li>
 *   <li>{@link #stream}（Tomcat 请求线程）：校验对话归属、同步落库用户消息、
 *       写一条 agent_run（RUNNING）、创建 {@link SseEmitter} 并立即返回 ——
 *       此刻 HTTP 响应头已发出（Content-Type: text/event-stream），请求线程释放回池子；</li>
 *   <li>生成与推送被丢进 {@code chatStreamExecutor} 线程池（见
 *       {@link com.ails.backend.config.AsyncConfig}），慢生成不占 Tomcat 请求线程；</li>
 *   <li>{@link #streamReply} 里调 {@link AgentGateway#streamReply} 把问题发给 Python Agent
 *       （附最近 20 条历史，<b>不含本轮提问</b>）；该调用<b>阻塞</b>线程池线程，Agent 每吐一段文本
 *       就回调 {@link AgentStreamHandler} 的 onDelta / onComplete / onError 之一；</li>
 *   <li>onDelta：一边转发成 SSE {@code message.delta} 事件推给前端，一边攒进本地 buffer；</li>
 *   <li>onComplete：buffer 整段存成助手消息 → 推 {@code message.end} → agent_run 置 COMPLETED
 *       → 关流 → 触发学习分析（提取 Concept → 产 Evidence）；</li>
 *   <li>onError / 异常：推 {@code message.error} → agent_run 置 FAILED。</li>
 * </ol>
 * <p>
 * <h3>前端事件契约（按事件名分发，不要按到达顺序猜）</h3>
 * <pre>
 *   event: message.start   data: { runId, conversationId }   // 首事件，必有
 *   event: message.delta   data: { delta }                    // 若干次增量文本
 *   event: message.end     data: { messageId, runId }         // 成功终态
 *   event: message.error   data: { runId, code, message }     // 失败终态
 * </pre>
 * runId 是一条 {@code agent_run}（run_type = CHAT）的主键，从第一个事件起就存在，
 * 状态走 RUNNING → COMPLETED / FAILED。前端拿到 runId 就能去查到这次请求的完整轨迹。
 * <p>
 * <h3>两条并发路径，收尾规则不同（本类最容易改错的地方）</h3>
 * <table border="1">
 *   <tr><th>路径</th><th>在哪个线程</th><th>怎么收尾</th></tr>
 *   <tr><td>生成侧：onComplete / onError / catch</td><td>chat-stream-*</td>
 *       <td>先 CAS 抢 {@code finished} 终局名额，抢到才写 agent_run，保证至多写一次</td></tr>
 *   <tr><td>传输侧：emitter 超时 / 连接错误</td><td>Tomcat</td>
 *       <td>不走 {@code finished}，改用 {@link #finishRunIfRunning} 的条件更新，
 *           只在 run 仍为 RUNNING 时落 FAILED，永不覆盖生成侧已经得出的结论</td></tr>
 * </table>
 * 分开的理由：传输断了不等于生成失败。容器回调触发时，生成线程往往还在跑，
 * 它跑完照样要落库助手消息、把 run 写成 COMPLETED —— 用户重进对话时回复是完整的。
 * <p>
 * 真实的生成能力由 {@link AgentGateway} 提供，本类不认识 Python，也不认识桩实现。
 */
@Slf4j
@Service
@RequiredArgsConstructor
public class ChatService {

    /** SSE 连接最长存活时间：5 分钟。到期未走完流程，容器会回调 emitter.onTimeout */
    private static final long SSE_TIMEOUT_MS = 5 * 60 * 1000L;
    /** 送给 Agent 的历史消息条数：最近 20 条，控制 prompt 长度 */
    private static final int HISTORY_LIMIT = 20;

    private final ConversationService conversationService;
    private final MessageService messageService;
    private final AgentGateway agentGateway;
    private final AgentRunMapper agentRunMapper;
    private final LearningAnalysisService learningAnalysisService;
    /**
     * 回复落库之后的学习分析。由 {@link AnalysisDispatchService} 决定是投递 MQ 还是同步执行；
     * 同步路径的细节见 {@link TurnAnalysisService}。
     */
    private final AnalysisDispatchService analysisDispatchService;
    /** 见 {@link com.ails.backend.config.AsyncConfig}：自定义 TaskExecutor 会让 Spring Boot
     *  的 applicationTaskExecutor 自动退让，因此这里不会有第二个同类型 Bean 造成歧义 */
    private final TaskExecutor chatStreamExecutor;

    /**
     * 发一条用户消息并流式返回助手回复。
     * <p>
     * 线程分工：本方法跑在 <b>Tomcat 请求线程</b> 上，只干三件快事 ——
     * 校验、同步落库（用户消息 + agent_run 的 RUNNING 行）、创建 SseEmitter 后立刻返回。
     * 用户消息之所以同步落库，是为了保证 {@code GET /messages} 立刻能看到这条消息、
     * 且序号（sequenceNo）严格按请求到达顺序分配；
     * 而生成回复是慢活（秒级~几十秒），全部丢给 {@code chatStreamExecutor} 线程池。
     * <p>
     * {@code authToken} 必须在<b>这里</b>传进来，不能等到线程池里再取：SSE 的生成跑在
     * {@code chat-stream-*} 线程上，那条线程没有 SecurityContext（它不属于任何请求），
     * {@code SecurityUtil} 在那里会直接抛 401。所以请求线程把 token 取好、当参数带下去。
     */
    public SseEmitter stream(Long userId, Long conversationId, String content, String authToken) {
        // 1) 权限校验：对话必须属于当前用户（否则 403/404）；已归档的对话不再接受新消息
        Conversation conversation = conversationService.ownedConversation(userId, conversationId);
        if (conversation.getStatus() != ConversationStatus.ACTIVE) {
            throw new BusinessException(400, "对话已归档，无法发送消息");
        }

        // 2) 同步落库用户消息（仍在请求线程）—— 此刻前端再 GET /messages 就能看到它
        Message userMessage = messageService.appendUserMessage(userId, conversationId, content);

        // 3) 在调用 Agent 之前先落定「这条对话属于哪个学习活动」。
        //    顺序是硬要求，不能挪到生成之后：Session 是懒创建的，新对话的第一条消息发出时
        //    session_id 还是 NULL，而 Agent 的上下文里要带「本阶段学习目标」。
        //    放到回复生成完之后，最该继承上个 Session 目标的那一轮（新开对话接着学同一个知识点）
        //    反而什么都带不上 —— 见 docs/session-design.md §一.4 / §六。
        //    这一步也顺手把这一轮的 Concept 匹配算好，后面 analyzeQuietly 直接复用，不重复查库。
        //
        //    它排在 createRun 之前，是为了让 agent_run.session_id 在插入时就写对 ——
        //    否则得再补一次 UPDATE，而且中间那段时间里这行轨迹的归属是错的。
        LearningAnalysisService.TurnOpening opening =
                learningAnalysisService.beginTurn(conversation, content);

        // 4) 建 agent_run 轨迹行（RUNNING）—— runId 是后续所有 SSE 事件携带的 id，也是排查失败的入口
        AgentRun run = createRun(userId, conversationId, content, sessionId(opening));

        // 5) 创建 SSE 连接句柄。SseEmitter 是 Spring MVC 的异步响应对象：
        //    返回给 Controller 后 HTTP 响应不再被请求线程阻塞，
        //    之后任何线程都能通过 emitter.send() 往里推数据，直到 emitter.complete() 关流。
        SseEmitter emitter = new SseEmitter(SSE_TIMEOUT_MS);
        // finished：全局「已终局」开关。onComplete / onError / catch 三条「生成侧」路径
        // 都可能竞争收尾，只允许拿到开关的那条写最终状态，避免重复落库、重复关流。
        AtomicBoolean finished = new AtomicBoolean(false);
        LocalDateTime startedAt = LocalDateTime.now();
        // 容器级回调，跑在 Tomcat 线程上，与 chat-stream-* 线程天然并发 —— 写终态时必须让
        // 数据库来做仲裁（见 finishRunIfRunning 的条件更新），不能像生成侧那样先 CAS 再写。
        // 这两条回调<b>不会</b>打断线程池里仍在跑的 agent 线程 —— 见 send() 的注释：
        // 客户端走了 send 会静默吞掉，agent 线程跑完后照常落库助手消息、把 run 改写成 COMPLETED。
        emitter.onTimeout(() -> finishRunIfRunning(run, AgentRunStatus.FAILED, "SSE 超时", startedAt));
        emitter.onError(e -> finishRunIfRunning(run, AgentRunStatus.FAILED, String.valueOf(e.getMessage()), startedAt));

        // 6) 丢进线程池：生成 + 推送在 chat-stream-* 线程上执行，
        //    本请求线程此刻立即把 emitter 返回给 Controller，回池子接下一个请求。
        //    必须包一层：线程池饱和时 execute 会抛 RejectedExecutionException。等它冒到
        //    DispatcherServlet 时响应头早就发出去（Content-Type: text/event-stream），
        //    异常处理器再想写 JSON 错误体也来不及了 —— 只能顺着这条已经建立的流把失败说清楚。
        try {
            chatStreamExecutor.execute(() -> streamReply(
                    emitter, run, conversation, userMessage, authToken, opening, finished, startedAt));
        } catch (RejectedExecutionException e) {
            log.warn("chat stream rejected, runId={}, chatStreamExecutor saturated", run.getId());
            // 先补 message.start 再发 message.error：前端按事件名驱动的状态机是从 start 进入
            // 「生成中」的，缺了它收到 error 会当成一次无主事件丢弃，页面就一直卡在发送态。
            send(emitter, "message.start", Map.of(
                    "runId", run.getId(),
                    "conversationId", conversation.getId()));
            send(emitter, "message.error", Map.of(
                    "runId", run.getId(),
                    "code", 503,
                    "message", "服务繁忙，请稍后重试"));
            // 这里只能走「仍在 RUNNING 才落」的条件更新：本 run 的生成线程从未启动，
            // 但用无条件写去碰它没有意义，条件更新表达的正是「没人收尾过才轮到我来收」。
            finishRunIfRunning(run, AgentRunStatus.FAILED, "chatStreamExecutor rejected", startedAt);
            emitter.complete();
        }

        return emitter;
    }

    // ================= 流式推送（跑在 chat-stream-* 线程池线程上） =================

    /**
     * 单次流式生成的主体，跑在线程池线程上。
     * <p>
     * 与 Agent 的交互：{@link AgentGateway#streamReply} 是<b>阻塞调用</b> ——
     * 它内部向 Python Agent 服务打开一条流式 HTTP 连接，之后 Agent 每收到一段回复文本
     * 就回调一次 {@link AgentStreamHandler}（onDelta 若干次 → onComplete / onError 二选一收尾），
     * 该调用返回即代表与这次 Agent 的交互彻底结束。
     * <p>
     * 与前端的交互：所有输出统一经 {@link #send} 写成 SSE 事件，且只发生在本线程（单线程写 emitter）。
     */
    private void streamReply(SseEmitter emitter, AgentRun run, Conversation conversation,
                             Message userMessage, String authToken,
                             LearningAnalysisService.TurnOpening opening,
                             AtomicBoolean finished, LocalDateTime startedAt) {
        // 回复全文缓冲：每来一个 delta 攒一段，结束时整段落库成一条助手消息。
        // 不逐 delta 落库 —— 否则一条回复会变成 N 条 message 行，污染序号和 GET /messages 列表。
        StringBuilder buffer = new StringBuilder();

        // 首事件：通知前端「开始生成了」。前端可以据此渲染 typing 态；
        // runId 从这里开始携带，前端凭它把整条流关联到 agent_run 的一条轨迹记录。
        send(emitter, "message.start", Map.of(
                "runId", run.getId(),
                "conversationId", conversation.getId()));

        try {
            // 组装发给 Agent 的请求：
            // - runId 让 Agent 侧的日志 / 埋点能跟后端的轨迹行对上；
            // - traceId 是跨进程的那个键，runId 只能在各自进程里找到自己那一半；
            // - history 取本对话最近 20 条，<b>并排掉刚落库的这条用户消息</b>：本轮输入已经由
            //   userMessage 字段单独承载，再出现在历史末尾就是同一句话发了两遍。
            // - session 让 Agent 知道「这一阶段要解决什么、现在卡在哪」。可空 —— 刚建出来的
            //   对话可能还没归属任何学习活动（闲聊就是这个结果）。
            // - authToken 透传用户本人的 JWT，Agent 用它回访后端的只读检索接口。
            ChatAgentRequest request = new ChatAgentRequest(
                    run.getId(),
                    run.getTraceId(),
                    run.getUserId(),
                    conversation.getId(),
                    conversation.getLearningSpaceId(),
                    userMessage.getContent(),
                    messageService.history(conversation.getId(), userMessage.getId(), HISTORY_LIMIT),
                    sessionContext(opening),
                    authToken);

            // —— 与 Agent 交互的核心：阻塞在这里，直到 Agent 的流结束。
            //    下面三个回调是 Agent 侧对我们「说话」的全部方式。
            agentGateway.streamReply(request, new AgentStreamHandler() {
                @Override
                public void onDelta(String delta) {
                    // 一段增量回复文本（不是完整句子，可能是几个字）。
                    if (delta == null || delta.isEmpty()) {
                        return;
                    }
                    buffer.append(delta);                            // ① 本地攒全文，供结束时整段落库
                    send(emitter, "message.delta", Map.of("delta", delta)); // ② 透传给前端（客户端已断开则静默吞掉）
                }

                @Override
                public void onComplete() {
                    // Agent 正常发完全部文本。
                    // CAS 抢终局名额：本回调与 onError / 外层 catch 竞争，抢不到说明终局已写过
                    // （这次回调只是迟到），直接丢弃 —— 否则同一条 agent_run 会被写两次状态。
                    if (!finished.compareAndSet(false, true)) {
                        return;
                    }
                    Message assistantMessage = null;
                    try {
                        // 把 buffer 里的全文存成一条助手消息 —— 从这一刻起 GET /messages 才能看到 AI 回复
                        assistantMessage = messageService.appendAssistantMessage(
                                conversation.getId(), buffer.toString());
                        // 结束事件：messageId 让前端把「刚流式出来的那段字」锚定到消息列表的具体行上
                        send(emitter, "message.end", Map.of(
                                "messageId", assistantMessage.getId(),
                                "runId", run.getId()));
                        // agent_run 置 COMPLETED，outputSummary 记一下输出规模，便于统计
                        finishRun(run, AgentRunStatus.COMPLETED, "chars=" + buffer.length(), null, startedAt);
                    } catch (Exception e) {
                        // 落库失败等价于这次回复不存在，前端必须收到明确的失败事件 ——
                        // 不能让它只看到流被干净地关掉、然后在等 message.end 里挂到超时。
                        // 终局名额已被本回调拿走，所以这里自己兜住，不往外抛：外层 catch 看到
                        // finished 已置位会直接放过，那条 message.error 就永远发不出去了。
                        log.error("persist assistant message failed, runId={}", run.getId(), e);
                        send(emitter, "message.error", Map.of(
                                "runId", run.getId(),
                                "code", 500,
                                "message", "回复保存失败"));
                        finishRun(run, AgentRunStatus.FAILED, null, e.toString(), startedAt);
                    } finally {
                        // 无论中间哪步抛异常，都保证 SSE 连接被关闭，容器资源随之释放
                        emitter.complete();
                    }
                    // 对话闭环的最后一步：静默做学习分析。
                    // 助手回复用 buffer 的全文而不是回读数据库 —— 即使落库失败（assistantMessage 为 null），
                    // Agent 仍能结合它判断这一轮是否产生了学习信号；此时历史里也不会有它，
                    // 因为那条消息根本没有落库。
                    // 顺序上刻意排在 emitter.complete() 之后：这一步要写 Evidence / Understanding /
                    // Revision / State，慢且与前端的即时体验无关，不该让 SSE 连接多挂一秒。
                    analyzeQuietly(conversation, userMessage, buffer.toString(),
                            assistantMessage == null ? null : assistantMessage.getId(),
                            authToken, opening);
                }

                @Override
                public void onError(int code, String message) {
                    // Agent 侧报的错（如模型调用失败、Python 服务返回错误）；
                    // code / message 的语义由 Agent 侧约定，这里原样透传给前端。
                    if (!finished.compareAndSet(false, true)) {
                        return;
                    }
                    // SSE 流式接口没有统一的 ApiResponse 包装 —— 失败信息也走事件下发
                    send(emitter, "message.error", Map.of(
                            "runId", run.getId(),
                            "code", code,
                            "message", message));
                    finishRun(run, AgentRunStatus.FAILED, null, message, startedAt);
                    emitter.complete();
                }
            });

        } catch (Exception e) {
            // 兜底：AgentGateway 调用本身炸掉（连接被拒、响应反序列化失败、onDelta 里抛异常等）会落到这里。
            // onComplete / onError 内部的失败各自就地处理，不会走到这儿 —— 它们已经把终局名额拿走了，
            // 这里再抢也抢不到，抢不到就什么都不做，直接放过。
            log.error("chat stream failed, runId={}", run.getId(), e);
            if (finished.compareAndSet(false, true)) {
                // 此时 onDelta 可能已经推过若干 delta，前端收到 error 后应丢弃已渲染的半成品
                send(emitter, "message.error", Map.of(
                        "runId", run.getId(),
                        "code", 500,
                        "message", "生成回复失败"));
                finishRun(run, AgentRunStatus.FAILED, null, e.toString(), startedAt);
                emitter.complete();
            }
        }
    }

    /**
     * 让学习闭环跟上：这一轮对话会产生 Evidence（进而推动 Understanding 演进）。
     * <p>
     * 分析失败绝不能影响已经推完的对话 —— 用户已经拿到回复了，回滚没有意义，
     * 所以这里吞掉异常，只留 error 日志供排查。
     * <p>
     * 具体走 Agent 还是本地规则版由 {@link TurnAnalysisService} 决定；本方法只负责
     * 「无论如何都别把异常抛回 SSE 线程」。
     */
    private void analyzeQuietly(Conversation conversation, Message userMessage,
                                String assistantContent, Long assistantMessageId,
                                String authToken, LearningAnalysisService.TurnOpening opening) {
        try {
            analysisDispatchService.dispatch(conversation, userMessage, assistantContent,
                    assistantMessageId, authToken, opening);
        } catch (Exception e) {
            log.error("analyze message failed, messageId={}", userMessage.getId(), e);
        }
    }

    /** 把学习活动转成 Agent 需要的那一小块上下文；没有归属时返回 null（Agent 侧据此不渲染该区块）。 */
    private static ChatAgentRequest.SessionContext sessionContext(LearningAnalysisService.TurnOpening opening) {
        LearningSession session = opening == null ? null : opening.session();
        if (session == null) {
            return null;
        }
        return new ChatAgentRequest.SessionContext(
                session.getId(), session.getTitle(), session.getGoal(), session.getCurrentFocus());
    }

    // ================= agent_run =================

    /**
     * 为本次生成写一条轨迹行（RUNNING）。
     * inputSummary 截断到 255 字符，匹配 agent_run.input_summary 列宽。
     */
    private AgentRun createRun(Long userId, Long conversationId, String content, Long sessionId) {
        AgentRun run = new AgentRun();
        run.setUserId(userId);
        run.setConversationId(conversationId);
        // 归属的学习活动。可能为 null：闲聊不产生也不归属任何 Session（Resolver 的 NONE 分支），
        // 这不是数据缺失，见 docs/session-design.md §五。
        run.setSessionId(sessionId);
        run.setRunType(AgentRunType.CHAT);
        run.setStatus(AgentRunStatus.RUNNING);
        run.setInputSummary(truncate(content, 255));
        run.setTraceId(TraceIds.newId());
        agentRunMapper.insert(run);
        return run;
    }

    /** 本轮归属的 Session id；{@link LearningAnalysisService.TurnOpening#session()} 可能为 null。 */
    private static Long sessionId(LearningAnalysisService.TurnOpening opening) {
        return opening.session() == null ? null : opening.session().getId();
    }

    /**
     * 收尾 agent_run。用只带 id 的空实体 updateById，MyBatis-Plus 默认忽略 null 字段，
     * 所以不会把创建时写好的 inputSummary 覆盖成空。
     * <p>
     * 这是<b>生成侧</b>的收尾（onComplete / onError / catch），调用前必须先抢到 {@code finished}
     * 这个终局名额，因此全局至多执行一次，可以无条件写。
     */
    private void finishRun(AgentRun run, AgentRunStatus status, String outputSummary,
                           String error, LocalDateTime startedAt) {
        AgentRun patch = new AgentRun();
        patch.setId(run.getId());
        patch.setStatus(status);
        patch.setOutputSummary(outputSummary);
        patch.setErrorMessage(truncate(error, 1000));
        patch.setFinishedAt(LocalDateTime.now());
        patch.setDurationMs(toMillis(startedAt, patch.getFinishedAt()));
        agentRunMapper.updateById(patch);
    }

    /**
     * 收尾 agent_run，但<b>只在这行还是 RUNNING 时</b>才落终态。给传输侧的路径用
     * （SSE 超时 / 连接错误，见 {@link #stream} 里的容器回调）。
     * <p>
     * 为什么要带这个条件：SSE 超时或连接断开只说明<b>传输</b>没了，不等于这次<b>生成</b>失败 ——
     * 容器回调跑在 Tomcat 线程上，此刻 chat-stream-* 线程很可能还在正常生成，几秒后就会调
     * {@link #finishRun} 写 COMPLETED。如果这里无条件写 FAILED，两个线程会各自 UPDATE 同一行、
     * 结果取决于谁后落笔（「超时判了失败，两秒后又被改成成功」）。
     * <p>
     * 把判断交给数据库：{@code WHERE id = ? AND status = 'RUNNING'} 由行锁串行化。
     * 生成侧若已写下 COMPLETED，这条 UPDATE 影响 0 行，不会把成功的 run 改判成失败；
     * 反过来超时先落了 FAILED，生成侧随后的无条件写会把终态纠正回 COMPLETED（那才是事实）。
     * 两种情况都收敛到同一个结果，与两个线程的调度顺序无关。
     */
    private void finishRunIfRunning(AgentRun run, AgentRunStatus status, String error, LocalDateTime startedAt) {
        AgentRun patch = new AgentRun();
        patch.setId(run.getId());
        patch.setStatus(status);
        patch.setErrorMessage(truncate(error, 1000));
        patch.setFinishedAt(LocalDateTime.now());
        patch.setDurationMs(toMillis(startedAt, patch.getFinishedAt()));
        agentRunMapper.update(patch, new LambdaUpdateWrapper<AgentRun>()
                .eq(AgentRun::getId, run.getId())
                .eq(AgentRun::getStatus, AgentRunStatus.RUNNING));
    }

    // ================= 内部工具 =================

    /**
     * 推一个 SSE 事件给前端。
     * <p>
     * 线上实际走的是 text/event-stream 纯文本格式，每个事件以空行分隔：
     * <pre>
     * event: message.delta
     * data: {"delta":"你好"}
     *
     * </pre>
     * payload 用 LinkedHashMap 做防御性拷贝，保证 JSON 字段顺序与传入一致、稳定可读。
     * <p>
     * 发送时抛 IOException / IllegalStateException 说明客户端已经不在了（关页面、断网、代理超时）——
     * 长流式连接里这是常态而非异常，所以只打 debug 日志，不刷 error。
     * 注意：客户端断开<b>不会</b>打断线程池里的 agent 线程，它会一直跑到 onComplete / onError，
     * 助手消息仍会照常落库 —— 用户重进对话时，消息列表里回复是完整的，不会丢。
     */
    private void send(SseEmitter emitter, String event, Map<String, Object> payload) {
        try {
            emitter.send(SseEmitter.event()
                    .name(event)
                    .data(new LinkedHashMap<>(payload), MediaType.APPLICATION_JSON));
        } catch (IOException | IllegalStateException e) {
            // 客户端断开是常态，不值得刷错误日志
            log.debug("SSE send failed, event={}", event, e);
        }
    }

    /**
     * 两次时刻之间的毫秒数，用于 {@code agent_run.duration_ms}（INT 列）。
     * <p>
     * 不能写成裸的 {@code (int) Duration.toMillis()}：long 收窄到 int 是<b>直接截断高位</b>，
     * 超过 24.8 天就翻成负数，看板上会出现「耗时 -1234ms」这种记录。
     * 目前 SSE 超时是 5 分钟，远够不着，但这是靠一个别处的常量在保平安 ——
     * 一旦有人把超时调大、或者 start/end 因为系统时间被改而算出一个荒唐的差值，就会静默出错。
     * 这里直接封顶，代价是一行 Math.min，收益是这个错误以后不可能再出现。
     */
    private static int toMillis(LocalDateTime from, LocalDateTime to) {
        return (int) Math.min(Integer.MAX_VALUE, Duration.between(from, to).toMillis());
    }

    /** 把超长文本截到 max 长度（匹配 agent_run 各摘要列的列宽）；null 原样返回 */
    private static String truncate(String text, int max) {
        if (text == null) {
            return null;
        }
        return text.length() <= max ? text : text.substring(0, max);
    }
}
