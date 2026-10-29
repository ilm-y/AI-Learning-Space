package com.ails.backend.agent;

/**
 * Agent 流式输出的回调出口。
 * <p>
 * {@link AgentGateway} 只负责把内容「推出来」，至于推到 SSE、WebSocket 还是别的地方，
 * 由调用方（{@code ChatService}）决定 —— 换传输层不用动 Agent 侧。
 *
 * <h3>回调时序（实现方与调用方共同的契约）</h3>
 * <pre>
 *   onDelta × N（N ≥ 0）  →  onComplete 或 onError（恰好一次，二选一）
 * </pre>
 * 终局回调之后不得再回调任何方法。当前唯一的调用方 ChatService 用一个
 * {@code AtomicBoolean} 做终局开关：终局后迟到的调用会被直接丢弃而不是报错，
 * 但那是<b>兜底</b>—— 实现方不要把「多发一次终局」或「终局后又吐 delta」当正常流程。
 *
 * <h3>线程约束</h3>
 * 三个方法都会在<b>调用 {@link AgentGateway#streamReply} 的那条线程</b>上串行调用
 * （对 ChatService 而言是 {@code chat-stream-*} 线程池线程）。
 * 实现方若要改成并发回调，必须同时确认调用方的 handler 是线程安全的 ——
 * ChatService 的实现<b>不是</b>：它用一个普通 StringBuilder 攒增量文本。
 *
 * <h3>两个方法都可能「没有接收方」</h3>
 * 客户端（浏览器 / Flutter）可能随时断开。调用方会自己吞掉这种发送失败并继续把整轮跑完，
 * 所以实现方不必、也无法从回调的返回值判断前端还在不在 —— 回调没有返回值就是这个原因。
 */
public interface AgentStreamHandler {

    /**
     * 一段增量文本。
     * <p>
     * 不是完整的句子，可能是几个字，调用方负责拼接。空串 / null 会被调用方忽略，
     * 但实现方最好不要推 —— 白跑一趟序列化和一次 SSE 写。
     *
     * @param delta 本段增量文本
     */
    void onDelta(String delta);

    /**
     * 流正常结束：内容已经全部推送完毕。
     * <p>
     * 调用方收到后会把这轮攒下的全文落库成一条助手消息、推 {@code message.end}、
     * 把 agent_run 置 COMPLETED，并触发学习分析。
     * <p>
     * 若实现方知道终态有截断，应调用 {@link #onComplete(StreamCompletion)} 而不是
     * 本方法；本方法只表示「没有更多文本」，不保证内容是完整的。
     * <p>
     * <b>这个方法不得抛异常。</b>它是「正常结束」的声明，不是执行动作的地方 ——
     * 调用方在 onComplete 里已经占掉了终局名额，抛出去的异常既不会变成前端的
     * {@code message.error}（转调 onError 会因为终局已定而被丢弃），也不会被重试，
     * 前端最后只看到一个没有 {@code message.end} 的断流。
     * 调用方内部会尽量自己兜住它那侧的失败（例如落库失败转成 {@code message.error}），
     * 但那是它自己的实现细节，不是实现方可以依赖的。
     */
    void onComplete();

    /**
     * 流结束，并带上 Agent 归一化的终态元数据。
     * <p>
     * {@code completion.truncated()} 为 {@code true} 时，已经推送的文本只是部分
     * 内容：调用方可以保留并展示它，但不能把这一轮当成完整回答继续触发学习分析。
     * <p>
     * 默认实现退回 {@link #onComplete()}，让只关心「正常结束」的旧实现保持可用。
     * 需要区分截断的调用方覆盖本方法即可。
     *
     * @param completion 终态元数据，不应为 {@code null}
     */
    default void onComplete(StreamCompletion completion) {
        onComplete();
    }

    /**
     * 流异常结束：这次生成失败了。
     * <p>
     * 调用方会把 code / message 原样透传给前端（{@code message.error} 事件），
     * 并把 agent_run 置 FAILED，因此 {@code message} 请写成能给人看的说明，
     * 不要塞堆栈。
     *
     * @param code    错误码，语义由实现方与前端约定（HTTP 风格的 4xx / 5xx 即可）
     * @param message 面向使用者的失败说明
     */
    void onError(int code, String message);
}
