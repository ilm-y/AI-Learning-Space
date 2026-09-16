package com.ails.backend.service;

import com.ails.backend.common.BusinessException;
import com.ails.backend.dto.memory.MemoryRequest;
import com.ails.backend.dto.memory.MemoryResponse;
import com.ails.backend.entity.Memory;
import com.ails.backend.entity.MemorySource;
import com.ails.backend.enums.MemorySourceType;
import com.ails.backend.enums.MemoryStatus;
import com.ails.backend.enums.MemoryType;
import com.ails.backend.mapper.MemoryMapper;
import com.ails.backend.mapper.MemorySourceMapper;
import com.baomidou.mybatisplus.core.conditions.query.LambdaQueryWrapper;
import lombok.RequiredArgsConstructor;
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
     * <b>keyword 不是「一个词」，而是「一段可以拆的查询文本」，按词拆开后 OR 匹配。</b>
     * 直接把它当成单个子串去 {@code LIKE '%关键词%'} 是行不通的：调用方（Python Agent）
     * 传的是用户原话，整句话几乎不可能原样出现在某条记忆里，于是每次都返回空列表 ——
     * 而空列表和不报错长得一模一样，看起来像「检索跑了、就是没结果」，实际上一次都没匹配上。
     * <p>
     * 拆分规则见 {@link #keywordTerms}：按空白与标点切分，中文再补一层字符二元组
     * （中文没有词间空格，不切二元组的话一整句仍然只是一个子串）。
     * 只要命中任意一个词就算候选，命中多的排在前面由 importance / updatedAt 决定 ——
     * 精确排序是向量检索该干的事，这里不假装自己有那个能力。
     */
    @Transactional(readOnly = true)
    public List<MemoryResponse> search(Long userId, Long spaceId, String keyword) {
        LambdaQueryWrapper<Memory> query = new LambdaQueryWrapper<Memory>()
                .eq(Memory::getUserId, userId)
                .eq(Memory::getStatus, MemoryStatus.ACTIVE)
                .eq(spaceId != null, Memory::getLearningSpaceId, spaceId)
                .orderByDesc(Memory::getImportance)
                .orderByDesc(Memory::getUpdatedAt);

        List<String> terms = keywordTerms(keyword);
        if (!terms.isEmpty()) {
            // 必须包在 and(...) 里：不包的话 OR 会和外层的 eq 平铺成
            // `user_id = ? AND status = ? OR content LIKE ?`，OR 一铺开前面两个条件就白写了。
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
        memory.setMemoryType(request.memoryType());
        memory.setPath(request.path());
        memory.setContent(request.content().trim());
        memory.setImportance(request.importance());
        // 改挂载点与新建同样要校验归属，否则「先建一条自己的记忆、再 PATCH 到别人的空间上」就能绕过 persist 里的检查
        requireOwnedRefs(userId, request.learningSpaceId(), request.conceptId());
        if (request.learningSpaceId() != null) {
            memory.setLearningSpaceId(request.learningSpaceId());
        }
        if (request.conceptId() != null) {
            memory.setConceptId(request.conceptId());
        }
        if (request.status() != null) {
            memory.setStatus(request.status());
        }
        memoryMapper.updateById(memory);
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
        memoryMapper.updateById(memory);
    }

    // ================= 内部工具 =================

    /**
     * 落库前校验 request 里带的两个外键归属。
     * <p>
     * 不校验的话这两个字段就是「用户随便填的整数」：{@code learningSpaceId} 可以填别人的空间 id，
     * 于是这条记忆会出现在对方的空间检索里（服务端按 userId + spaceId 查，双方的过滤条件都能命中）；
     * {@code conceptId} 同理，能把记忆挂到别人知识点的理解上下文上。
     * 两个 id 都是可空的（记忆可以不挂空间/知识点），只有在给了值的时候才需要它真的属于当前用户。
     */
    private Memory persist(Long userId, MemoryRequest request, MemoryStatus status) {
        requireOwnedRefs(userId, request.learningSpaceId(), request.conceptId());
        Memory memory = new Memory();
        memory.setUserId(userId);
        memory.setLearningSpaceId(request.learningSpaceId());
        memory.setConceptId(request.conceptId());
        memory.setMemoryType(request.memoryType() == null ? MemoryType.LEARNING : request.memoryType());
        memory.setPath(request.path());
        memory.setContent(request.content().trim());
        memory.setStatus(status);
        memory.setImportance(request.importance());
        memoryMapper.insert(memory);
        return memory;
    }

    /** 两个可空外键的归属校验：非空即必须属于当前用户，否则按「不存在」处理（404） */
    private void requireOwnedRefs(Long userId, Long spaceId, Long conceptId) {
        if (spaceId != null) {
            spaceService.ownedSpace(userId, spaceId);
        }
        if (conceptId != null) {
            conceptService.accessibleConcept(userId, conceptId);
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
