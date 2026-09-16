package com.ails.backend.mapper;

import com.ails.backend.entity.LearningUnderstanding;
import com.baomidou.mybatisplus.core.mapper.BaseMapper;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;

public interface LearningUnderstandingMapper extends BaseMapper<LearningUnderstanding> {

    /**
     * 按 (user_id, concept_id) 取理解快照的写锁，必须在事务里调用。
     * <p>
     * 这一条读要承担三件事，所以它必须是<b>加锁读</b>：
     * <ol>
     *   <li><b>串行化 apply 的读改写。</b>apply 是「读快照 → 改列表 → 写回 → 生成 Revision」，
     *       不串行的话两个并发分析会各自读到同一份旧快照，后写的把先写的观察整个覆盖掉；</li>
     *   <li><b>serial 分配 revision_no。</b>{@code nextRevisionNo} 是 {@code COUNT(*)+1}，
     *       与 {@code uk_revision_no(understanding_id, revision_no)} 之间本来是裸奔的；</li>
     *   <li><b>让证据查重真的成立。</b>EvidenceService 的「跨 run 查重」是一次普通 SELECT，
     *       本身是 TOCTOU 的。同一 (user, concept) 的两个分析在这里被强制排队后，
     *       后一个的普通 SELECT 才看得见前一个已提交的写入。</li>
     * </ol>
     * 行不存在时，等值条件在唯一索引 {@code uk_understanding_user_concept} 上取的是 gap 锁，
     * 并发 INSERT 会被对方的插入意向锁挡住 —— 但 gap 锁之间不冲突，所以「两个事务都没查到、
     * 都去 INSERT」这条路径仍然要靠调用方捕获 DuplicateKeyException 兜底。
     * <p>
     * <b>调用约定：它必须是所在事务的第一条语句。</b>InnoDB 的一致性读视图是在事务里第一次
     * <b>普通</b> SELECT 时建立的；先普通读、再加锁，等锁到手时视图已经旧了，看不到对方刚提交的写入。
     */
    @Select("SELECT * FROM learning_understanding WHERE user_id = #{userId} AND concept_id = #{conceptId} FOR UPDATE")
    LearningUnderstanding selectByUserAndConceptForUpdate(@Param("userId") Long userId,
                                                          @Param("conceptId") Long conceptId);
}
