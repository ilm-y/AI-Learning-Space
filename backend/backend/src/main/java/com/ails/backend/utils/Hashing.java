package com.ails.backend.utils;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.Locale;

/**
 * 确定性指纹工具。
 * <p>
 * 存在的理由是「身份」判断需要跨请求稳定：Evidence 的 {@code dedupe_key} 和
 * Memory 缺省 memoryKey 时兜底的 {@code auto:} 值，都是「同一份内容必须算出同一个字符串」。
 * 两处各写一份 SHA-256 迟早会分叉，所以集中在这里。
 */
public final class Hashing {

    private Hashing() {
    }

    /**
     * 归一化文本：去首尾、折叠内部空白、统一小写。
     * <p>
     * 只用于计算指纹，<b>不改写入的原文</b> —— 用户看到的内容该怎样还怎样。
     * 不做这层归一化的话，「TLB  Miss」和「tlb miss」会算成两条互不相干的记录。
     */
    public static String normalize(String text) {
        return text == null ? "" : text.trim().replaceAll("\\s+", " ").toLowerCase(Locale.ROOT);
    }

    /** SHA-256 十六进制小写（64 位）。 */
    public static String sha256Hex(String text) {
        try {
            byte[] digest = MessageDigest.getInstance("SHA-256").digest(text.getBytes(StandardCharsets.UTF_8));
            StringBuilder hex = new StringBuilder(digest.length * 2);
            for (byte b : digest) {
                hex.append(Character.forDigit((b >> 4) & 0xF, 16));
                hex.append(Character.forDigit(b & 0xF, 16));
            }
            return hex.toString();
        } catch (NoSuchAlgorithmException e) {
            // SHA-256 是 JDK 强制实现的算法，走不到这里
            throw new IllegalStateException("JVM 不支持 SHA-256", e);
        }
    }
}
