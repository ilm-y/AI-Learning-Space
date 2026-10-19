package com.ails.backend.agent;

import com.ails.backend.dto.agent.ChatAgentRequest;

/**
 * Backend 与 Agent Runtime 之间唯一的边界。
 * <p>
 * 现在有两个实现，由配置项 {@code ails.agent.gateway} 二选一：
 * <ul>
 *   <li>{@link StubAgentGateway} —— {@code stub}（缺省）。本机回显，不依赖任何外部进程。</li>
 *   <li>{@link HttpPythonAgentGateway} —— {@code http}。真发给 Python Agent。</li>
 * </ul>
 * 换实现只改这一个配置项，ChatController / ChatService 不需要改动 —— 这正是这层抽象的意义，
 * Controller 里因此也不必留任何「以后要调 Python Agent」的注释。
 * <p>
 * <b>两个实现的装配方式：</b>这里注入的是<b>按类型</b>的 {@link AgentGateway}，
 * 两个候选 Bean 同时存在会启动即报 NoUniqueBeanDefinitionException，所以两边都挂了
 * {@code @ConditionalOnProperty}（桩带 {@code matchIfMissing = true}）。以后再加实现，
 * 同样必须挂条件，不能只写一个裸的 {@code @Component}。
 *
 * <h3>实现契约（调用方 {@code ChatService} 就按这套语义写代码）</h3>
 * <ol>
 *   <li><b>阻塞调用。</b>方法要一直阻塞到这次 Agent 交互<b>彻底结束</b>再返回，
 *       不能「启动一个后台任务就返回」。ChatService 把「本方法返回」当作
 *       「与 Agent 的交互已结束」的信号，返回之后不再接受任何回调。</li>
 *   <li><b>在调用方线程上回调。</b>handler 的三个方法应当在调用 {@code streamReply}
 *       的那条线程上依次调用。ChatService 传入的 handler 实现<b>不是线程安全的</b>
 *       （内部用一个普通 StringBuilder 攒增量文本），并且假定回调串行、不会并发。
 *       实现方不要为了「提高并发」自己开线程池去回调，除非同时把这条约束改掉。</li>
 *   <li><b>终局回调恰好一次。</b>调用序列固定为
 *       {@code onDelta × N（N ≥ 0）} → {@code onComplete} <b>或</b> {@code onError}，
 *       二选一。终局回调之后不得再调用任何方法 —— 迟到的 {@code onComplete} 会被
 *       ChatService 的终局开关丢弃，但那是它的兜底，不是实现方把「收尾发两次」
 *       「终局后又吐 delta」当成正常流程的理由。</li>
 *   <li><b>错误走 onError，不要抛。</b>模型调用失败、下游返回错误、响应反序列化失败，
 *       都属于「这次生成失败」，应当转成 {@code onError(code, message)} 而不是抛异常。
 *       调用方只对自己那侧的异常兜底，抛出去会把栈打出来当故障处理。</li>
 *   <li><b>不碰传输层。</b>实现只管「把内容推出来」——推到 SSE、WebSocket 还是别的地方，
 *       由调用方决定。实现方不得持有或关闭调用方的连接对象。</li>
 *   <li><b>不写业务数据。</b>Evidence / Understanding 等学习闭环的落库由后端完成
 *       （见 {@code LearningAnalysisService}），Agent 侧只读不写，
 *       这样同一次对话的写入路径只有一条。</li>
 * </ol>
 */
public interface AgentGateway {

    /**
     * 请求一次流式回复，把内容经 {@code handler} 推回给调用方。
     * <p>
     * 参数 {@link ChatAgentRequest} 里的 {@code history} <b>不含</b>本轮提问
     * （本轮由 {@code userMessage} 单独承载），实现方直接把它当成「这轮之前说过的话」用。
     *
     * @param request 本轮请求：身份信息（runId / userId / conversationId / spaceId）、
     *                本轮提问、以及不含本轮提问的对话历史
     * @param handler 内容与终局的回调出口，约束见类注释
     */
    void streamReply(ChatAgentRequest request, AgentStreamHandler handler);
}
