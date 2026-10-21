package com.ails.backend.utils;

import java.security.SecureRandom;
import java.util.HexFormat;

/**
 * 生成 {@code agent_run.trace_id}：16 字节随机数的 32 位十六进制。
 * <p>
 * 单独成一个工具类，是因为调用方已经不止一个（Chat 与 Analyze 各需要一条轨迹），
 * 两处各写一份 {@link SecureRandom} 迟早会出现一边用 {@code ThreadLocalRandom}、
 * 另一边用 {@code SecureRandom} 的漂移。
 * <p>
 * 用 {@link SecureRandom} 而不是 {@code ThreadLocalRandom}：这个值会出现在日志里，
 * 可预测的 trace id 让「构造一个 id 去翻别人的执行记录」变得容易。
 * 它不参与业务判断，纯粹是关联键，所以不需要全局唯一约束。
 */
public final class TraceIds {

    private static final SecureRandom RANDOM = new SecureRandom();

    private TraceIds() {
    }

    public static String newId() {
        byte[] bytes = new byte[16];
        RANDOM.nextBytes(bytes);
        return HexFormat.of().formatHex(bytes);
    }
}
