package com.ails.backend.mapper;

import org.apache.ibatis.mapping.BoundSql;
import org.apache.ibatis.mapping.MappedStatement;
import org.apache.ibatis.mapping.ParameterMapping;
import org.apache.ibatis.session.Configuration;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

import java.util.HashMap;
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * 不连接数据库，验证批量 Evidence 查询的注解动态 SQL 能被 MyBatis 解析，
 * 并确认「LIMIT 在 SQL 内、Sources 一次 IN」不是应用层伪造出来的。
 * <p>
 * 真正执行 SQL 才能证明过滤效果，需要独立 MySQL；这里固定 SQL 形状，
 * 防止后续重构把 LIMIT 去掉、改成先全量拉取再切片。
 */
class EvidenceMapperBatchSqlTest {

    private static MappedStatement evidenceStatement;
    private static MappedStatement sourceStatement;

    @BeforeAll
    static void parseMappers() {
        Configuration configuration = new Configuration();
        configuration.addMapper(LearningEvidenceMapper.class);
        configuration.addMapper(EvidenceSourceMapper.class);
        evidenceStatement = configuration.getMappedStatement(
                LearningEvidenceMapper.class.getName() + ".selectRecentByConcepts");
        sourceStatement = configuration.getMappedStatement(
                EvidenceSourceMapper.class.getName() + ".selectByEvidenceIds");
    }

    @Test
    @DisplayName("证据查询：user_id + Space（JOIN concept）+ concept_id IN + 时间倒序 + SQL 内 LIMIT")
    void evidenceQueryIsBoundedAndUserScoped() {
        BoundSql bound = evidenceBoundSql();
        String sql = normalize(bound.getSql());

        assertThat(sql)
                .contains("FROM learning_evidence e")
                .contains("JOIN concept c ON c.id = e.concept_id")
                .contains("e.user_id = ?")
                // 空间过滤在 SQL 内自包含：learning_evidence 本身没有 space_id。
                .contains("c.learning_space_id = ?")
                .contains("e.concept_id IN")
                .contains("ORDER BY e.created_at DESC, e.id DESC")
                // LIMIT 必须下行到 SQL，而不是取回全部后由 Java 截断。
                .contains("LIMIT ?");

        assertThat(properties(bound))
                .contains("userId", "spaceId", "limit")
                .anyMatch(property -> property.startsWith("__frch_conceptId_"));
    }

    @Test
    @DisplayName("Sources 查询：一次 WHERE evidence_id IN (...)，无逐条条件")
    void sourcesQueryIsOneInQuery() {
        BoundSql bound = sourceBoundSql();
        String sql = normalize(bound.getSql());

        assertThat(sql)
                .contains("FROM evidence_source")
                .contains("WHERE evidence_id IN")
                .contains("ORDER BY id ASC")
                // 批量查询本身不再需要 LIMIT；条数由上层证据数决定。
                .doesNotContain("LIMIT");

        assertThat(properties(bound))
                .anyMatch(property -> property.startsWith("__frch_evidenceId_"))
                .doesNotContain("evidenceId");
    }

    private static BoundSql evidenceBoundSql() {
        Map<String, Object> params = new HashMap<>();
        params.put("userId", 1L);
        params.put("spaceId", 7L);
        params.put("conceptIds", List.of(10L, 20L));
        params.put("limit", 5);
        return evidenceStatement.getBoundSql(params);
    }

    private static BoundSql sourceBoundSql() {
        Map<String, Object> params = new HashMap<>();
        params.put("evidenceIds", List.of(100L, 101L));
        return sourceStatement.getBoundSql(params);
    }

    private static List<String> properties(BoundSql bound) {
        return bound.getParameterMappings().stream()
                .map(ParameterMapping::getProperty)
                .toList();
    }

    private static String normalize(String sql) {
        return sql.replaceAll("\\s+", " ").trim();
    }
}
