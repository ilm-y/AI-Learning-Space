package com.ails.backend.mapper;

import com.ails.backend.enums.RelationType;
import org.apache.ibatis.mapping.BoundSql;
import org.apache.ibatis.mapping.MappedStatement;
import org.apache.ibatis.mapping.ParameterMapping;
import org.apache.ibatis.session.Configuration;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;

import java.util.HashMap;
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * 不连接数据库，验证 {@link ConceptRelationMapper#selectRelated} 的注解动态 SQL
 * 能被 MyBatis 解析，并确认作用域条件没有被漏掉。
 * <p>
 * 真正执行 SQL 的过滤效果需要独立 MySQL；这里用于在没有数据库的环境里
 * 发现 {@code <script>} / {@code <foreach>} / 列名写错导致的启动期映射失败，
 * 同时把“跨 Space 不返回”固定到 SQL 文本上。
 */
class ConceptRelationMapperSqlTest {

    private static MappedStatement statement;

    @BeforeAll
    static void parseMapper() {
        Configuration configuration = new Configuration();
        configuration.addMapper(ConceptRelationMapper.class);
        statement = configuration.getMappedStatement(
                ConceptRelationMapper.class.getName() + ".selectRelated");
    }

    @Test
    void sqlScopesBothEndpointsToTheSameSpaceAndExcludesSoftDeletedConcepts() {
        BoundSql bound = boundSql();
        String sql = normalize(bound.getSql());

        assertThat(sql)
                // concept_relation 没有 space_id/user_id，作用域只能来自两端 concept
                .contains("src.learning_space_id = ?")
                .contains("tgt.learning_space_id = ?")
                .contains("src.deleted_at IS NULL")
                .contains("tgt.deleted_at IS NULL")
                // 只返回与目标概念直接相连的一跳
                .contains("r.source_concept_id = ?")
                .contains("r.target_concept_id = ?")
                // 类型白名单与条数上限由参数化传入
                .contains("r.relation_type IN")
                .contains("LIMIT ?");

        assertThat(properties(bound))
                .contains("conceptId", "spaceId", "limit")
                .anyMatch(property -> property.startsWith("__frch_type_"));
    }

    @Test
    void directionIsExpressedRelativeToTheQueriedConcept() {
        String sql = normalize(boundSql().getSql());

        // source = 被查询概念 → OUTGOING；target = 被查询概念 → INCOMING
        assertThat(sql).contains("THEN 'OUTGOING'").contains("ELSE 'INCOMING'");
    }

    private static BoundSql boundSql() {
        Map<String, Object> params = new HashMap<>();
        params.put("conceptId", 20L);
        params.put("spaceId", 1L);
        params.put("relationTypes", List.of(RelationType.PREREQUISITE, RelationType.RELATED));
        params.put("limit", 5);
        return statement.getBoundSql(params);
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
