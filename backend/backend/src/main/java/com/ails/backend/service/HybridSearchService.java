package com.ails.backend.service;

import com.ails.backend.config.MemorySearchProperties;
import com.ails.backend.dto.memory.HybridMemoryHit;
import com.ails.backend.dto.memory.HybridMemorySearchResponse;
import com.ails.backend.dto.memory.MemoryResponse;
import com.ails.backend.dto.memory.agent.MemoryVectorHit;
import com.ails.backend.dto.memory.agent.MemoryVectorSearchRequest;
import com.ails.backend.entity.Memory;
import com.ails.backend.enums.MemoryHybridStatus;
import com.ails.backend.mapper.MemoryMapper;
import com.ails.backend.utils.Hashing;
import lombok.extern.slf4j.Slf4j;
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.core.task.TaskExecutor;
import org.springframework.stereotype.Service;
import org.springframework.util.StringUtils;

import java.time.Duration;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.CompletionException;
import java.util.concurrent.RejectedExecutionException;
import java.util.concurrent.TimeUnit;
import java.util.function.Function;
import java.util.stream.Collectors;

/**
 * 混合检索：MySQL FULLTEXT + Agent/Qdrant 向量召回，RRF 融合后<b>强制回源复核</b>。
 *
 * <h3>安全底线</h3>
 * 向量路只返回 {@code memoryId / score / contentHash}，正文永远从 MySQL 回源。
 * 回源 SQL 再次施加 {@code user_id}、{@code status = 'ACTIVE'} 与可见性过滤：
 * Qdrant 中任何陈旧、越权、孤儿数据都会在这里被丢弃。因此 Qdrant 的权限过滤
 * 只是「优化」，SQL 复核才是「保证」。
 *
 * <h3>降级</h3>
 * 两路并行且各自有超时，任何一路失败都不会让整个检索报错：
 * {@link MemoryHybridStatus#DEGRADED_VECTOR} / {@link MemoryHybridStatus#DEGRADED_KEYWORD}。
 * 两路都失败或回源复核失败时返回 {@link MemoryHybridStatus#UNAVAILABLE}，
 * 绝不把「检索不可用」伪装成「没有相关记忆」。
 */
@Slf4j
@Service
public class HybridSearchService {

    /** 查询截断长度：向量路不需要整段对话，过长只会浪费 Embedding 预算。 */
    private static final int MAX_QUERY_LENGTH = 200;
    private static final int MAX_WARNING_LENGTH = 300;

    private final MemoryMapper memoryMapper;
    private final AgentMemoryClient agentMemoryClient;
    private final MemorySearchProperties properties;
    private final TaskExecutor executor;

    public HybridSearchService(MemoryMapper memoryMapper,
                               AgentMemoryClient agentMemoryClient,
                               MemorySearchProperties properties,
                               @Qualifier("memorySearchExecutor") TaskExecutor executor) {
        this.memoryMapper = memoryMapper;
        this.agentMemoryClient = agentMemoryClient;
        this.properties = properties;
        this.executor = executor;
    }

    public HybridMemorySearchResponse search(Long userId,
                                             Long spaceId,
                                             Long conceptId,
                                             String keyword,
                                             Integer requestedLimit) {
        long startedAt = System.nanoTime();
        int limit = normalizeLimit(requestedLimit);

        if (!StringUtils.hasText(keyword)) {
            // 旧语义：空查询返回该用户的 ACTIVE 列表，而不是空结果。
            List<HybridMemoryHit> items = memoryMapper
                    .selectVisibleList(userId, spaceId, conceptId, limit)
                    .stream()
                    .map(memory -> new HybridMemoryHit(
                            MemoryResponse.from(memory), 0.0, null, null, null))
                    .toList();
            return response(MemoryHybridStatus.BLANK_QUERY, items, List.of(), startedAt);
        }

        String query = truncate(keyword.trim(), MAX_QUERY_LENGTH);
        int recallK = properties.recallK();

        CompletableFuture<Channel<List<Long>>> lexicalFuture = this.<List<Long>>submit(
                () -> memoryMapper
                        .searchFulltext(userId, spaceId, conceptId, query, recallK)
                        .stream()
                        .map(Memory::getId)
                        .toList(),
                "词法检索");
        CompletableFuture<Channel<List<MemoryVectorHit>>> vectorFuture =
                this.<List<MemoryVectorHit>>submit(
                () -> agentMemoryClient.search(new MemoryVectorSearchRequest(
                        UUID.randomUUID().toString(), userId, spaceId, conceptId,
                        query, recallK)),
                "向量检索");

        Channel<List<Long>> lexical = lexicalFuture
                .completeOnTimeout(Channel.fail("超时"), properties.lexicalTimeoutMs(),
                        TimeUnit.MILLISECONDS)
                .join();
        Channel<List<MemoryVectorHit>> vector = vectorFuture
                .completeOnTimeout(Channel.fail("超时"), properties.vectorTimeoutMs(),
                        TimeUnit.MILLISECONDS)
                .join();

        List<String> warnings = new ArrayList<>();
        if (!lexical.ok()) {
            warnings.add("词法检索失败：" + lexical.error());
        }
        if (!vector.ok()) {
            warnings.add("向量检索失败：" + vector.error());
        }

        if (!lexical.ok() && !vector.ok()) {
            log.warn("memory.hybrid.search userId={} status=UNAVAILABLE lexical={} vector={}",
                    userId, lexical.error(), vector.error());
            return response(MemoryHybridStatus.UNAVAILABLE, List.of(), warnings, startedAt);
        }

        List<MemoryVectorHit> vectorHits = vector.ok() ? vector.value() : List.of();
        List<RrfFuser.Fused> fused = RrfFuser.fuse(
                lexical.ok() ? lexical.value() : List.of(),
                vectorHits,
                properties.rrfK(),
                properties.lexicalWeight(),
                properties.vectorWeight());

        if (fused.isEmpty()) {
            return response(status(lexical, vector), List.of(), warnings, startedAt);
        }

        List<Long> ids = fused.stream().map(RrfFuser.Fused::memoryId).distinct().toList();
        Map<Long, Memory> visible;
        try {
            visible = memoryMapper.selectVisibleByIds(userId, spaceId, conceptId, ids)
                    .stream()
                    .collect(Collectors.toMap(Memory::getId, Function.identity()));
        } catch (RuntimeException e) {
            // 不能返回未经复核的数据；复核失败与「检索不可用」等价。
            log.error("memory.hybrid.recheck.failed userId={} ids={}", userId, ids.size(), e);
            warnings.add("回源复核失败，已放弃本次检索结果");
            return response(MemoryHybridStatus.UNAVAILABLE, List.of(), warnings, startedAt);
        }

        Map<Long, String> vectorHashes = hashByMemoryId(vectorHits);
        int stale = 0;
        for (Memory memory : visible.values()) {
            String expected = vectorHashes.get(memory.getId());
            if (expected != null && !expected.equals(Hashing.sha256Hex(memory.getContent()))) {
                stale++;
                // 正文以 MySQL 为准；这里只记录索引落后，绝不把陈旧正文带进结果。
                log.warn("memory.hybrid.stale memoryId={} userId={}", memory.getId(), userId);
            }
        }

        Comparator<RrfFuser.Fused> comparator = Comparator
                .comparingDouble(RrfFuser.Fused::score).reversed()
                .thenComparing(fusedHit -> importanceOf(visible.get(fusedHit.memoryId())),
                        Comparator.nullsLast(Comparator.reverseOrder()))
                .thenComparing(fusedHit -> updatedAtOf(visible.get(fusedHit.memoryId())),
                        Comparator.nullsLast(Comparator.reverseOrder()))
                .thenComparingLong(RrfFuser.Fused::memoryId);

        List<HybridMemoryHit> items = fused.stream()
                .filter(fusedHit -> visible.containsKey(fusedHit.memoryId()))
                .sorted(comparator)
                .limit(limit)
                .map(fusedHit -> new HybridMemoryHit(
                        MemoryResponse.from(visible.get(fusedHit.memoryId())),
                        fusedHit.score(),
                        fusedHit.lexicalRank(),
                        fusedHit.vectorRank(),
                        fusedHit.vectorScore()))
                .toList();

        MemoryHybridStatus status = status(lexical, vector);
        log.info("memory.hybrid.search userId={} spaceId={} status={} lexical={} vector={} "
                        + "fused={} returned={} stale={}",
                userId, spaceId, status,
                lexical.ok() ? lexical.value().size() : 0,
                vector.ok() ? vectorHits.size() : 0,
                fused.size(), items.size(), stale);
        return response(status, items, warnings, startedAt);
    }

    private <T> CompletableFuture<Channel<T>> submit(ThrowingSupplier<T> supplier,
                                                     String channelName) {
        try {
            return CompletableFuture
                    .supplyAsync(() -> {
                        try {
                            return Channel.ok(supplier.get());
                        } catch (RuntimeException e) {
                            throw new CompletionException(e);
                        }
                    }, executor)
                    .handle((value, error) -> error == null
                            ? value
                            : Channel.<T>fail(describe(channelName, error)));
        } catch (RejectedExecutionException e) {
            return CompletableFuture.completedFuture(
                    Channel.fail(channelName + "线程池已满"));
        }
    }

    private int normalizeLimit(Integer requestedLimit) {
        int limit = requestedLimit == null || requestedLimit <= 0
                ? properties.defaultLimit()
                : requestedLimit;
        return Math.min(limit, properties.maxLimit());
    }

    private static MemoryHybridStatus status(Channel<?> lexical, Channel<?> vector) {
        if (lexical.ok() && vector.ok()) {
            return MemoryHybridStatus.OK;
        }
        return lexical.ok()
                ? MemoryHybridStatus.DEGRADED_VECTOR
                : MemoryHybridStatus.DEGRADED_KEYWORD;
    }

    private static Map<Long, String> hashByMemoryId(List<MemoryVectorHit> hits) {
        Map<Long, String> hashes = new HashMap<>();
        for (MemoryVectorHit hit : hits) {
            if (hit.memoryId() != null && hit.contentHash() != null) {
                hashes.put(hit.memoryId(), hit.contentHash());
            }
        }
        return hashes;
    }

    private static Float importanceOf(Memory memory) {
        return memory == null ? null : memory.getImportance();
    }

    private static java.time.LocalDateTime updatedAtOf(Memory memory) {
        return memory == null ? null : memory.getUpdatedAt();
    }

    private static HybridMemorySearchResponse response(MemoryHybridStatus status,
                                                       List<HybridMemoryHit> items,
                                                       List<String> warnings,
                                                       long startedAt) {
        return new HybridMemorySearchResponse(status, items, warnings, elapsedMs(startedAt));
    }

    private static long elapsedMs(long startedAt) {
        return Duration.ofNanos(System.nanoTime() - startedAt).toMillis();
    }

    private static String describe(String channelName, Throwable error) {
        Throwable cause = error instanceof CompletionException && error.getCause() != null
                ? error.getCause()
                : error;
        String message = cause.getMessage();
        if (message == null || message.isBlank()) {
            message = cause.getClass().getSimpleName();
        }
        return truncate(channelName + "：" + message, MAX_WARNING_LENGTH);
    }

    private static String truncate(String text, int max) {
        if (text == null) {
            return "";
        }
        return text.length() <= max ? text : text.substring(0, max);
    }

    @FunctionalInterface
    private interface ThrowingSupplier<T> {
        T get();
    }

    /**
     * 单路结果。{@code error == null} 表示该路成功；成功但空列表仍是成功。
     */
    private record Channel<T>(T value, String error) {

        static <T> Channel<T> ok(T value) {
            return new Channel<>(value, null);
        }

        static <T> Channel<T> fail(String error) {
            return new Channel<>(null, error);
        }

        boolean ok() {
            return error == null;
        }
    }
}
