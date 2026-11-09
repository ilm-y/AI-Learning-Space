package com.ails.backend.mapper;

import org.apache.ibatis.mapping.BoundSql;
import org.apache.ibatis.mapping.MappedStatement;
import org.apache.ibatis.mapping.ParameterMapping;
import org.apache.ibatis.session.Configuration;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

import java.time.LocalDateTime;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * D4-4：Manifest 分页 SQL 的静态形状验证（不连接数据库）。
 * <p>
 * 重点不是“SQL 能跑”，而是钉住作用域条件没有被漏掉：
 * {@code agent_run} 没有 {@code space_id}，空间只能经 conversation 证明；
 * 少一个 {@code user_id} / {@code learning_space_id} / {@code deleted_at} 条件
 * 就会变成跨用户或跨空间泄漏。真正执行效果仍需独立 MySQL。
 */
class AgentRunMapperManifestSqlTest {

    private static Configuration configuration;

    @BeforeAll
    static void parseMapper() {
        configuration = new Configuration();
        configuration.addMapper(AgentRunMapper.class);
    }

    @Test
    @DisplayName("分页 SQL 同时约束 user / space / 软删对话，并用 JSON 路径过滤 policy_version 与 intent")
    void pageSqlScopesToUserAndSpace() {
        BoundSql bound = boundSql("selectManifestPage", allFilters());
        String sql = normalize(bound.getSql());

        assertThat(sql)
                .contains("FROM agent_run r")
                .contains("JOIN conversation c ON c.id = r.conversation_id")
                .contains("r.user_id = ?")
                .contains("c.learning_space_id = ?")
                .contains("c.deleted_at IS NULL")
                .contains("r.context_manifest IS NOT NULL")
                .contains("JSON_UNQUOTE(JSON_EXTRACT(r.context_manifest, '$.policy_version')) = ?")
                .contains("JSON_UNQUOTE(JSON_EXTRACT(r.context_manifest, '$.intent')) = ?")
                .contains("r.created_at >= ?")
                .contains("r.created_at < ?")
                .contains("ORDER BY r.created_at DESC, r.id DESC")
                .contains("LIMIT ? OFFSET ?");

        assertThat(properties(bound)).contains(
                "userId", "spaceId", "policyVersion", "intent", "from", "to", "limit", "offset");
    }

    @Test
    @DisplayName("count SQL 与分页 SQL 使用同一组作用域 / 过滤条件")
    void countSqlMatchesPageScope() {
        BoundSql bound = boundSql("countManifestPage", allFilters());
        String sql = normalize(bound.getSql());

        assertThat(sql)
                .contains("SELECT COUNT(*)")
                .contains("JOIN conversation c ON c.id = r.conversation_id")
                .contains("r.user_id = ?")
                .contains("c.learning_space_id = ?")
                .contains("c.deleted_at IS NULL")
                .contains("r.context_manifest IS NOT NULL")
                .contains("JSON_UNQUOTE(JSON_EXTRACT(r.context_manifest, '$.policy_version')) = ?")
                .contains("JSON_UNQUOTE(JSON_EXTRACT(r.context_manifest, '$.intent')) = ?")
                .doesNotContain("LIMIT");
    }

    @Test
    @DisplayName("可选过滤为空时不拼接 JSON / 时间条件，但仍保留 user + space 作用域")
    void optionalFiltersAreOmitted() {
        Map<String, Object> params = new HashMap<>();
        params.put("userId", 1L);
        params.put("spaceId", 2L);
        params.put("policyVersion", null);
        params.put("intent", null);
        params.put("from", null);
        params.put("to", null);
        params.put("limit", 20);
        params.put("offset", 0);

        BoundSql bound = boundSql("selectManifestPage", params);
        String sql = normalize(bound.getSql());

        assertThat(sql)
                .contains("r.user_id = ?")
                .contains("c.learning_space_id = ?")
                .contains("c.deleted_at IS NULL")
                .doesNotContain("JSON_EXTRACT")
                // 只钉时间过滤条件；ORDER BY r.created_at DESC 始终存在，不能一并禁止。
                .doesNotContain("r.created_at >=", "r.created_at <");
        assertThat(properties(bound))
                .contains("userId", "spaceId", "limit", "offset")
                .doesNotContain("policyVersion", "intent", "from", "to");
    }

    private static BoundSql boundSql(String method, Map<String, Object> params) {
        MappedStatement statement = configuration.getMappedStatement(
                AgentRunMapper.class.getName() + "." + method);
        return statement.getBoundSql(params);
    }

    private static Map<String, Object> allFilters() {
        Map<String, Object> params = new HashMap<>();
        params.put("userId", 1L);
        params.put("spaceId", 2L);
        params.put("policyVersion", "d4-2-v1");
        params.put("intent", "LEARNING_EXPLANATION");
        params.put("from", LocalDateTime.of(2026, 11, 1, 0, 0));
        params.put("to", LocalDateTime.of(2026, 11, 9, 0, 0));
        params.put("limit", 20);
        params.put("offset", 0);
        return params;
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
