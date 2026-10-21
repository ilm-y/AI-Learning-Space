package com.ails.backend.model;

import com.ails.backend.enums.MemoryReconcileAction;
import com.ails.backend.enums.MemoryScopeType;
import com.ails.backend.enums.MemoryType;
import org.springframework.util.StringUtils;

/**
 * Agent 提出的一条 Memory 候选（与 Web DTO 解耦）。
 * <p>
 * Agent 的回答只表达「它认为这条记忆值得留下、放在哪个作用域」，至于这条记忆最终能不能
 * 写入、能不能从 CANDIDATE 升到 ACTIVE，全部由
 * {@link com.ails.backend.service.MemoryService#reconcileCandidate} 说了算。
 * <p>
 * 这里做两件轻量的归一化，避免把明显不合法的候选一路带到持久层：
 * <ul>
 *   <li>scopeType 缺省时按「有 conceptId → CONCEPT、有 spaceId → SPACE、都没有 → GLOBAL」推断，
 *       与用户手工创建记忆时的规则一致；</li>
 *   <li>GLOBAL 作用域强制清空两个外键 —— 数据库里 GLOBAL 记忆的空间列必须是 NULL，
 *       否则「全局记忆」会同时被当成本空间记忆检索出来。</li>
 * </ul>
 * 注意：<b>归一化不等于校验</b>。CONCEPT 缺 conceptId、SPACE 缺 spaceId 这类候选在
 * {@code MemoryService} 里会被判为不可写入（skip），而不是在这里被猜成别的作用域。
 */
public record MemoryCandidateDraft(
        MemoryScopeType scopeType,
        Long learningSpaceId,
        Long conceptId,
        MemoryType memoryType,
        String memoryKey,
        String content,
        Float importance,
        MemoryReconcileAction action) {

    public MemoryCandidateDraft {
        MemoryScopeType scope = scopeType != null ? scopeType : inferScope(learningSpaceId, conceptId);
        if (scope == MemoryScopeType.GLOBAL) {
            learningSpaceId = null;
            conceptId = null;
        }
        memoryType = memoryType == null ? MemoryType.LEARNING : memoryType;
        memoryKey = StringUtils.hasText(memoryKey) ? memoryKey.trim() : null;
        content = StringUtils.hasText(content) ? content.trim() : null;
        action = action == null ? MemoryReconcileAction.ADD : action;
        scopeType = scope;
    }

    /** 内容为空时整条候选没有意义，调用方可以直接跳过。 */
    public boolean isWritable() {
        return content != null;
    }

    private static MemoryScopeType inferScope(Long spaceId, Long conceptId) {
        if (conceptId != null) {
            return MemoryScopeType.CONCEPT;
        }
        return spaceId != null ? MemoryScopeType.SPACE : MemoryScopeType.GLOBAL;
    }
}
