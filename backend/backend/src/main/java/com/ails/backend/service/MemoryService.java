package com.ails.backend.service;

import com.ails.backend.common.BusinessException;
import com.ails.backend.dto.memory.MemoryRequest;
import com.ails.backend.dto.memory.MemoryResponse;
import com.ails.backend.entity.Concept;
import com.ails.backend.entity.Memory;
import com.ails.backend.entity.MemorySource;
import com.ails.backend.enums.MemoryScopeType;
import com.ails.backend.enums.MemorySourceType;
import com.ails.backend.enums.MemoryStatus;
import com.ails.backend.enums.MemoryType;
import com.ails.backend.mapper.MemoryMapper;
import com.ails.backend.mapper.MemorySourceMapper;
import com.ails.backend.utils.Hashing;
import com.baomidou.mybatisplus.core.conditions.query.LambdaQueryWrapper;
import lombok.RequiredArgsConstructor;
import org.springframework.dao.DuplicateKeyException;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.util.StringUtils;

import java.util.LinkedHashSet;
import java.util.List;
import java.util.Objects;
import java.util.Set;

/**
 * Agent 长期记忆的生命周期管理。
 * <p>
 * Memory 与 Understanding 是两回事：Understanding 是「用户对知识点的认知模型」，
 * Memory 是「Agent 将来服务这个用户时要带着的长期上下文」（他的偏好、学习方式、
 * 反复出现的困惑）。
 * <p>
 * <b>生命周期用 status 管理，不做物理删除：</b>
 * <pre>
 *   CANDIDATE（Agent 推断，待确认） → ACTIVE（生效） → ARCHIVED（归档）
 * </pre>
 * 所以对外那个 {@code DELETE /api/memories/{id}} 在语义上是「归档」，不是删除 ——
 * 并且它不触碰 Evidence 与 Understanding，学习历史不受影响。
 * <p>
 * <b>作用域（scope_type / scope_key）是本类最核心的不变量，见设计文档 §十八~二十一。</b>
 * 它由 {@code learningSpaceId} / {@code conceptId} 推导，调用方无法直接指定：
 * <pre>
 *   conceptId 非空 → CONCEPT，空间取自该知识点
 *   spaceId   非空 → SPACE
 *   两者都空      → GLOBAL
 * </pre>
 * 推导完立刻校验「同一 user + 同一 scope_key + 同一 memory_key 只能有一条」，
 * 因为这是检索可见性的唯一依据 —— 两条记忆争同一个身份，检索结果就是不确定的。
 * <p>
 * <b>{@link #list} 与 {@link #search} 是两套不同的语义，不要互相「修正」：</b>
 * {@code list} 是管理视图，按字段精确过滤，{@code spaceId=X} 就只看挂在 X 上的；
 * {@code search} 是给 Agent 做上下文检索用的，必须遵守 §二十一 的可见性规则
 * （GLOBAL 对任意 Space 可见）。把 list 也改成可见性语义会让管理视图没法回答
 * 「这个空间到底挂了多少条记忆」。
 */
@Service
@RequiredArgsConstructor
public class MemoryService {

    /** 关键词检索最多拆出多少个词去 OR。上限存在的意义是给 SQL 定一个界，不让它随输入长度膨胀。 */
    private static final int MAX_KEYWORD_TERMS = 16;
    /** 参与检索的词最短长度：单字命中率太低，且放进 OR 里会拖出一大堆无关记忆 */
    private static final int MIN_KEYWORD_TERM_LENGTH = 2;

    private final MemoryMapper memoryMapper;
    private final MemorySourceMapper memorySourceMapper;
    private final LearningSpaceService spaceService;
    private final ConceptService conceptService;

    /** 用户自己创建的 Memory：直接生效，不需要确认 */
    @Transactional
    public MemoryResponse create(Long userId, MemoryRequest request) {
        return MemoryResponse.from(persist(userId, request, MemoryStatus.ACTIVE));
    }

    /**
     * 记录一条 Agent 推断出的 Memory。
     * <p>
     * 这是留给 Agent Runtime 的入口：AI 推断出的内容默认只能停在 CANDIDATE，
     * 必须由用户确认（{@link #update} 里改 status）才升到 ACTIVE。
     * 当前还没有 Agent 往这里写，所以线上不会有 CANDIDATE 数据。
     * <p>
     * 身份撞车时和 {@link #create} 一样抛 409 而不是静默覆盖：Agent 重放同一份推断
     * 时应当能看见「已经有了」，而不是把用户已经改过的内容再盖回去。
     */
    @Transactional
    public Memory createCandidate(Long userId, MemoryRequest request,
                                  MemorySourceType sourceType, Long sourceId) {
        Memory memory = persist(userId, request, MemoryStatus.CANDIDATE);
        MemorySource source = new MemorySource();
        source.setMemoryId(memory.getId());
        source.setSourceType(sourceType);
        source.setSourceId(sourceId);
        memorySourceMapper.insert(source);
        return memory;
    }

    @Transactional(readOnly = true)
    public List<MemoryResponse> list(Long userId, Long spaceId, Long conceptId, MemoryStatus status) {
        return memoryMapper.selectList(new LambdaQueryWrapper<Memory>()
                        .eq(Memory::getUserId, userId)
                        .eq(spaceId != null, Memory::getLearningSpaceId, spaceId)
                        .eq(conceptId != null, Memory::getConceptId, conceptId)
                        .eq(status != null, Memory::getStatus, status)
                        .orderByDesc(Memory::getImportance)
                        .orderByDesc(Memory::getUpdatedAt))
                .stream().map(MemoryResponse::from).toList();
    }

    /**
     * 记忆检索（第一版为关键词匹配；接向量库后由 embeddingRef 承担）。
     * <p>
     * <b>可见性按设计文档 §二十一：</b>
     * <pre>
     *   GLOBAL                             → 任意空间可见
     *   SPACE   AND space_id  = :spaceId   → 只在该空间可见
     *   CONCEPT AND space_id  = :spaceId
     *             AND concept_id = :conceptId → 只在该空间的这个知识点下可见
     * </pre>
     * 老实现是 {@code learning_space_id = :spaceId} 精确匹配，而 GLOBAL 记忆的
     * {@code learning_space_id} 是 NULL —— 于是「全局记忆永远检索不到」，
     * 而且检索返回空列表和「真的没有匹配」长得一模一样，排查时看不出任何异常。
     * <p>
     * 调用方给出的上下文越少，能匹配的作用域越少，这是刻意的保守：<b>检索结果会直接进
     * LLM prompt</b>，拿不准的记忆宁可少给 —— 尤其 CONCEPT 级记忆，它只在
     * 「这个空间 + 这个知识点」的上下文里才成立，只给 spaceId 就把它捞出来是错的。
     * 反之完全不给 spaceId 时不做可见性过滤（返回该用户全部 ACTIVE 记忆），
     * 因为这时调用方本来就没有声明任何上下文，也就无所谓「可见」。
     * <p>
     * <b>keyword 不是「一个词」，而是「一段可以拆的查询文本」，按词拆开后 OR 匹配。</b>
     * 直接把它当成单个子串去 {@code LIKE '%关键词%'} 是行不通的：调用方（Python Agent）
     * 传的是用户原话，整句话几乎不可能原样出现在某条记忆里，于是每次都返回空列表。
     * 拆分规则见 {@link #keywordTerms}：按空白与标点切分，中文再补一层字符二元组
     * （中文没有词间空格，不切二元组的话一整句仍然只是一个子串）。
     * 只要命中任意一个词就算候选，命中多的排在前面由 importance / updatedAt 决定 ——
     * 精确排序是向量检索该干的事，这里不假装自己有那个能力。
     */
    @Transactional(readOnly = true)
    public List<MemoryResponse> search(Long userId, Long spaceId, Long conceptId, String keyword) {
        LambdaQueryWrapper<Memory> query = new LambdaQueryWrapper<Memory>()
                .eq(Memory::getUserId, userId)
                .eq(Memory::getStatus, MemoryStatus.ACTIVE)
                .orderByDesc(Memory::getImportance)
                .orderByDesc(Memory::getUpdatedAt);

        // 必须包在 and(...) 里：不包的话作用域里的 OR 会和外层条件平铺成
        // `user_id = ? AND status = ? OR scope_type = 'GLOBAL'`，OR 一铺开前面两个条件就白写了
        // —— 直接变成「所有用户的全局记忆」，这是最危险的一类拼接错误。
        if (spaceId != null) {
            query.and(w -> {
                w.eq(Memory::getScopeType, MemoryScopeType.GLOBAL);
                w.or(o -> o.eq(Memory::getScopeType, MemoryScopeType.SPACE)
                        .eq(Memory::getLearningSpaceId, spaceId));
                if (conceptId != null) {
                    w.or(o -> o.eq(Memory::getScopeType, MemoryScopeType.CONCEPT)
                            .eq(Memory::getLearningSpaceId, spaceId)
                            .eq(Memory::getConceptId, conceptId));
                }
            });
        }

        List<String> terms = keywordTerms(keyword);
        if (!terms.isEmpty()) {
            query.and(w -> {
                for (String term : terms) {
                    w.or().like(Memory::getContent, term);
                }
            });
        }
        return memoryMapper.selectList(query).stream().map(MemoryResponse::from).toList();
    }

    /**
     * 把查询文本拆成用于 LIKE 的词。
     * <p>
     * 两层：
     * <ol>
     *   <li>按空白与中英标点切分成词 —— 英文/有空格的语言到这一步就够了；</li>
     *   <li>对<b>含中文</b>且长度大于 2 的词，再补上相邻两字的二元组。中文句子没有词边界，
     *       不切的话「我不太理解递归和分治」整体只是一个子串，永远匹配不到内容为「递归」的记忆；
     *       切成「递归」「分治」这类二元组后就能命中。代价是有假阳性（「和分」这种跨词组合），
     *       对「找出可能相关的记忆交给 Agent 参考」这个用途可以接受。</li>
     * </ol>
     */
    private List<String> keywordTerms(String keyword) {
        if (!StringUtils.hasText(keyword)) {
            return List.of();
        }
        Set<String> terms = new LinkedHashSet<>();
        for (String part : keyword.trim().split("[\\s,，。.、;；:：!！?？\"'“”‘’()（）\\[\\]【】/\\\\|]+")) {
            String token = part.trim();
            if (token.length() < MIN_KEYWORD_TERM_LENGTH) {
                continue;
            }
            terms.add(token);
            if (!containsCjk(token)) {
                continue;
            }
            for (int i = 0; i + 2 <= token.length(); i++) {
                terms.add(token.substring(i, i + 2));
            }
            if (terms.size() >= MAX_KEYWORD_TERMS) {
                break;
            }
        }
        // 拆出来的词太多时只取前 MAX_KEYWORD_TERMS 个：SQL 里每多一个 OR 就多一次
        // LIKE 全表扫，而排在后面的是长句尾部，通常也不是用户真正想问的东西
        return terms.stream().limit(MAX_KEYWORD_TERMS).toList();
    }

    private static boolean containsCjk(String text) {
        return text.codePoints().anyMatch(cp -> cp >= 0x4E00 && cp <= 0x9FFF);
    }

    @Transactional(readOnly = true)
    public MemoryResponse get(Long userId, Long memoryId) {
        return MemoryResponse.from(ownedMemory(userId, memoryId));
    }

    @Transactional
    public MemoryResponse update(Long userId, Long memoryId, MemoryRequest request) {
        Memory memory = ownedMemory(userId, memoryId);

        // 生效值 = 请求里给了就用请求的，没给就保留原来的。
        // 不能直接读 request：PATCH 只传 content 时两个 id 都是 null，
        // 照抄会把作用域从 CONCEPT 悄悄降级成 GLOBAL —— 记忆随即对全部空间可见。
        Long spaceId = request.learningSpaceId() != null ? request.learningSpaceId() : memory.getLearningSpaceId();
        Long conceptId = request.conceptId() != null ? request.conceptId() : memory.getConceptId();
        Scope scope = resolveScope(userId, spaceId, conceptId);
        String memoryKey = StringUtils.hasText(request.memoryKey())
                ? request.memoryKey().trim()
                : memory.getMemoryKey();

        requireIdentityFree(userId, scope, memoryKey, memoryId);
        applyScope(memory, scope, memoryKey);
        memory.setMemoryType(request.memoryType());
        memory.setContent(request.content().trim());
        memory.setImportance(request.importance());
        if (request.status() != null) {
            memory.setStatus(request.status());
        }
        requireUpdated(memoryMapper.updateById(memory), "记忆已被其他请求修改，请刷新后重试");
        return MemoryResponse.from(memory);
    }

    /**
     * 归档。对外表现为 {@code DELETE /api/memories/{id}}，但内部只改状态 ——
     * Evidence / Understanding 完全不受影响。
     */
    @Transactional
    public void archive(Long userId, Long memoryId) {
        Memory memory = ownedMemory(userId, memoryId);
        memory.setStatus(MemoryStatus.ARCHIVED);
        requireUpdated(memoryMapper.updateById(memory), "记忆已被其他请求修改，请刷新后重试");
    }

    // ================= 内部工具 =================

    /**
     * 一条记忆的完整身份：作用域 + 作用域内的标识。
     *
     * @param scopeType 作用域类型，同时决定两个 id 列谁有值谁为 NULL
     * @param scopeKey  作用域的字符串形式，见设计文档 §十九
     */
    private record Scope(MemoryScopeType scopeType, String scopeKey, Long spaceId, Long conceptId) {
    }

    /**
     * 从两个可空外键推导作用域，并顺带校验它们的归属。
     * <p>
     * 不校验归属的话这两个字段就是「用户随便填的整数」：{@code learningSpaceId} 可以填别人的空间 id，
     * 于是这条记忆会出现在对方的空间检索里（服务端按 userId + spaceId 查，双方的过滤条件都能命中）；
     * {@code conceptId} 同理，能把记忆挂到别人知识点的理解上下文上。
     * <p>
     * CONCEPT 作用域的空间<b>一律取知识点自己的空间</b>，不采信请求里的 spaceId：
     * 让两者不一致等于允许「宣称在空间 A、实际挂在空间 B 的知识点上」，
     * 而可见性是按空间过滤的，这种数据会让检索结果自相矛盾。请求里给了不一致的值直接报 400。
     */
    private Scope resolveScope(Long userId, Long spaceId, Long conceptId) {
        if (conceptId != null) {
            Concept concept = conceptService.accessibleConcept(userId, conceptId);
            Long conceptSpaceId = concept.getLearningSpaceId();
            if (spaceId != null && !spaceId.equals(conceptSpaceId)) {
                throw new BusinessException("记忆的学习空间与知识点所属空间不一致");
            }
            return new Scope(MemoryScopeType.CONCEPT, "CONCEPT:" + conceptId, conceptSpaceId, conceptId);
        }
        if (spaceId != null) {
            spaceService.ownedSpace(userId, spaceId);
            return new Scope(MemoryScopeType.SPACE, "SPACE:" + spaceId, spaceId, null);
        }
        return new Scope(MemoryScopeType.GLOBAL, "GLOBAL", null, null);
    }

    /** 把作用域写进实体。GLOBAL 的两个 id 列必须是 NULL —— 见 {@link MemoryScopeType} 的说明。 */
    private static void applyScope(Memory memory, Scope scope, String memoryKey) {
        memory.setScopeType(scope.scopeType());
        memory.setScopeKey(scope.scopeKey());
        memory.setLearningSpaceId(scope.spaceId());
        memory.setConceptId(scope.conceptId());
        memory.setMemoryKey(memoryKey);
    }

    /**
     * 落库前校验 request 里带的两个外键归属，并按 §十八 的规则落一条新记忆。
     *
     * @throws BusinessException 409：同作用域下已经有同 memoryKey 的记忆
     */
    private Memory persist(Long userId, MemoryRequest request, MemoryStatus status) {
        Scope scope = resolveScope(userId, request.learningSpaceId(), request.conceptId());
        String memoryKey = effectiveMemoryKey(request.memoryKey(), request.content());
        requireIdentityFree(userId, scope, memoryKey, null);

        Memory memory = new Memory();
        memory.setUserId(userId);
        applyScope(memory, scope, memoryKey);
        memory.setMemoryType(request.memoryType() == null ? MemoryType.LEARNING : request.memoryType());
        memory.setContent(request.content().trim());
        memory.setStatus(status);
        memory.setImportance(request.importance());
        try {
            memoryMapper.insert(memory);
        } catch (DuplicateKeyException e) {
            // 上面刚查过没查到，这里还是撞了 —— 只能是并发插入。
            // 交给唯一索引兜底而不是靠「先查后插」，正是 uk_memory_identity 存在的意义。
            throw identityTaken(scope, memoryKey);
        }
        return memory;
    }

    /**
     * 缺省 memoryKey：用归一化后的 content 哈希兜底。
     * <p>
     * 不能因为调用方不传就存 NULL —— MySQL 的唯一索引允许多行 NULL，
     * 那样 {@code uk_memory_identity} 立刻失效，同一条记忆能被无限重复插入，
     * 而「重复记忆」正是这套 scope 机制要防的东西。
     * <p>
     * 代价是「同一作用域下内容完全相同但想并存两条」做不到，必须显式给不同的 memoryKey ——
     * 这个代价是划算的：那种需求本来就该靠 memoryKey 表达，而不是靠内容碰巧不同。
     */
    private static String effectiveMemoryKey(String requested, String content) {
        if (StringUtils.hasText(requested)) {
            return requested.trim();
        }
        return "auto:" + Hashing.sha256Hex(Hashing.normalize(content)).substring(0, 32);
    }

    /**
     * 确认这个身份没被别的记忆占用。
     *
     * @param excludeId 更新场景下要排除自己，否则每次 PATCH 都会撞上自己
     */
    private void requireIdentityFree(Long userId, Scope scope, String memoryKey, Long excludeId) {
        Long count = memoryMapper.selectCount(new LambdaQueryWrapper<Memory>()
                .eq(Memory::getUserId, userId)
                .eq(Memory::getScopeKey, scope.scopeKey())
                .eq(Memory::getMemoryKey, memoryKey)
                .ne(excludeId != null, Memory::getId, excludeId));
        if (count != null && count > 0) {
            throw identityTaken(scope, memoryKey);
        }
    }

    private static BusinessException identityTaken(Scope scope, String memoryKey) {
        return BusinessException.conflict(
                "同一作用域下已存在标识为「" + memoryKey + "」的记忆（" + scope.scopeKey() + "）");
    }

    /**
     * 乐观锁的落地检查。
     * <p>
     * {@code version} 要靠 {@code MybatisPlusConfig} 里注册的
     * {@code OptimisticLockerInnerInterceptor} 才会进 WHERE 条件；版本对不上时
     * {@code updateById} 返回 0 而不是抛异常，所以这里必须显式判返回值，
     * 否则并发覆盖会静默丢掉一次更新。
     */
    private static void requireUpdated(int rows, String message) {
        if (rows == 0) {
            throw BusinessException.conflict(message);
        }
    }

    private Memory ownedMemory(Long userId, Long memoryId) {
        Memory memory = memoryMapper.selectById(memoryId);
        if (memory == null || !Objects.equals(memory.getUserId(), userId)) {
            throw BusinessException.notFound("记忆不存在");
        }
        return memory;
    }
}
