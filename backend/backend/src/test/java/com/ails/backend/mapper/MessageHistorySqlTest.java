package com.ails.backend.mapper;

import org.apache.ibatis.mapping.BoundSql;
import org.apache.ibatis.mapping.MappedStatement;
import org.apache.ibatis.mapping.ParameterMapping;
import org.apache.ibatis.session.Configuration;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * 历史消息查询的 SQL 归属边界。
 * <p>
 * {@code message} 表没有 {@code user_id}，因此用户归属必须通过
 * {@code JOIN conversation} 在 SQL 内证明。这里不连接数据库，只固定 SQL 形状：
 * 防止后续把归属校验退回「调用方记得先查 ownedConversation」的隐式约定。
 */
class MessageHistorySqlTest {

    private static MappedStatement historyStatement;

    @BeforeAll
    static void parseMapper() {
        Configuration configuration = new Configuration();
        configuration.addMapper(MessageMapper.class);
        historyStatement = configuration.getMappedStatement(
                MessageMapper.class.getName() + ".selectHistoryForUser");
    }

    @Test
    @DisplayName("历史查询：JOIN conversation + user_id + 双方软删 + TEXT + 倒序 LIMIT")
    void historyQueryIsUserScopedAndBounded() {
        BoundSql bound = historyBoundSql(List.of(100L));
        String sql = normalize(bound.getSql());

        assertThat(sql)
                .contains("FROM message m")
                .contains("JOIN conversation c ON c.id = m.conversation_id")
                .contains("m.conversation_id = ?")
                // 用户归属由 SQL 自己证明，不依赖调用方先查询。
                .contains("c.user_id = ?")
                .contains("m.message_type = 'TEXT'")
                .contains("m.deleted_at IS NULL")
                .contains("c.deleted_at IS NULL")
                .contains("m.id NOT IN")
                .contains("ORDER BY m.sequence_no DESC, m.id DESC")
                .contains("LIMIT ?");

        assertThat(properties(bound))
                .contains("userId", "conversationId", "limit")
                .anyMatch(property -> property.startsWith("__frch_excludedId_"));
    }

    @Test
    @DisplayName("没有需要排除的消息时，SQL 不生成 NOT IN 片段")
    void historyQueryOmitsExclusionWhenEmpty() {
        String sql = normalize(historyBoundSql(List.of()).getSql());

        assertThat(sql).doesNotContain("NOT IN");
    }

    private static BoundSql historyBoundSql(List<Long> excluded) {
        Map<String, Object> params = new HashMap<>();
        params.put("userId", 1L);
        params.put("conversationId", 2L);
        params.put("excludeMessageIds", new ArrayList<>(excluded));
        params.put("limit", 20);
        return historyStatement.getBoundSql(params);
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
