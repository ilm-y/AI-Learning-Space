package com.ails.backend.enums;

/**
 * 误区的生命周期状态，落在 learning_understanding.misconceptions 的 JSON 里。
 * <p>
 * <pre>
 *   错误证据          ACTIVE    当前仍然存在这个误区
 *      ↓              ↓
 *   反向正确证据      PENDING   出现了反向的正确证据，误区可能正在解除，但还没被独立确认
 *      ↓              ↓
 *   再次独立确认      （解除）   条目从当前快照中移除
 * </pre>
 * <b>解除（RESOLVED）不是这里的一个常量</b>：误区一旦解除就从当前快照里移除，
 * 而不是留一条 RESOLVED 记录 —— 当前快照只回答「现在还存在什么误区」。
 * 「曾经有过、后来被纠正」属于历史，可以在 learning_evidence 与
 * understanding_revision 里查到。
 * <p>
 * 这个区分会直接影响 attention：ACTIVE 优先级最高，PENDING 仍然关注但降级。
 */
public enum MisconceptionStatus {

    /** 当前仍然存在这个误区 */
    ACTIVE,

    /** 已经出现反向的正确证据，但还没有被独立确认 */
    PENDING
}
