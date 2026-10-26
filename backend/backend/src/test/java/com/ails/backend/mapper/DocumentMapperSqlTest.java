package com.ails.backend.mapper;

import org.apache.ibatis.mapping.BoundSql;
import org.apache.ibatis.mapping.MappedStatement;
import org.apache.ibatis.session.Configuration;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;

import java.util.HashMap;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * 不连接数据库，验证 {@link DocumentMapper#selectReadyIndexedIds} 的
 * 注解动态 SQL 能被 MyBatis 正确解析，并检查权限/状态条件没有被漏掉。
 *
 * <p>真正执行 SQL 的过滤效果由 {@code DocumentServiceIntegrationTest} 在
 * 独立 MySQL 库中验证；这里用于在没有 MySQL 的环境里也能发现
 * `<script>`、转义字符或 OGNL 写错导致的启动期映射失败。</p>
 */
class DocumentMapperSqlTest {

    private static MappedStatement statement;

    @BeforeAll
    static void parseMapper() {
        Configuration configuration = new Configuration();
        configuration.addMapper(DocumentMapper.class);
        statement = configuration.getMappedStatement(
                DocumentMapper.class.getName() + ".selectReadyIndexedIds");
    }

    @Test
    void sqlAlwaysRequiresReadyUndeletedAndFullyIndexedDocuments() {
        BoundSql bound = boundSql("text-embedding-v4");
        String sql = normalize(bound.getSql());

        assertThat(sql)
                .contains("d.learning_space_id = ?")
                .contains("d.user_id = ?")
                .contains("d.status = 'READY'")
                .contains("d.deleted_at IS NULL")
                .contains("d.indexed_at IS NOT NULL")
                .contains("d.embedding_model IS NOT NULL")
                .contains("d.chunk_count > 0")
                .contains("d.chunk_count = (")
                .contains("c.vector_ref IS NOT NULL");
    }

    @Test
    void embeddingModelFilterIsAppliedOnlyWhenProvided() {
        BoundSql withModel = boundSql("text-embedding-v4");
        BoundSql withoutModel = boundSql(null);

        assertThat(normalize(withModel.getSql())).contains("d.embedding_model = ?");
        assertThat(normalize(withoutModel.getSql())).doesNotContain("d.embedding_model = ?");

        assertThat(withModel.getParameterMappings()).hasSize(3);
        assertThat(withoutModel.getParameterMappings()).hasSize(2);
        assertThat(withModel.getParameterMappings().get(2).getProperty())
                .isEqualTo("embeddingModel");
    }

    private static BoundSql boundSql(String embeddingModel) {
        Map<String, Object> params = new HashMap<>();
        params.put("userId", 1L);
        params.put("spaceId", 2L);
        params.put("embeddingModel", embeddingModel);
        return statement.getBoundSql(params);
    }

    private static String normalize(String sql) {
        return sql.replaceAll("\\s+", " ").trim();
    }
}
