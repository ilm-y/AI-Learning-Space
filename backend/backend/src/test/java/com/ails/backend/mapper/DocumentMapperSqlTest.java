package com.ails.backend.mapper;

import org.apache.ibatis.mapping.BoundSql;
import org.apache.ibatis.mapping.MappedStatement;
import org.apache.ibatis.mapping.ParameterMapping;
import org.apache.ibatis.session.Configuration;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;

import java.util.HashMap;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * 不连接数据库，验证 {@link DocumentMapper#selectReadyDocuments} 的注解动态 SQL
 * 能被 MyBatis 正确解析，并检查权限/状态/活跃版本条件没有被漏掉。
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
                DocumentMapper.class.getName() + ".selectReadyDocuments");
    }

    @Test
    void sqlRequiresReadyUndeletedAndFullyIndexedActiveVersion() {
        BoundSql bound = boundSql("text-embedding-v4", "openai-compatible", 1024, "v1:abc");
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
                // 只统计活跃版本的 chunk：构建中的新版本不能影响白名单。
                .contains("c.index_version = d.index_version")
                .contains("c.vector_ref IS NOT NULL")
                // 返回活跃版本号，Agent 据此过滤 doc_version。
                .contains("d.index_version AS indexVersion");
    }

    @Test
    void embeddingIdentityFilterIsAppliedOnlyWhenModelProvided() {
        BoundSql withModel = boundSql(
                "text-embedding-v4", "openai-compatible", 1024, "v1:abc");
        BoundSql withoutModel = boundSql(null, null, null, null);

        String with = normalize(withModel.getSql());
        assertThat(with)
                .contains("d.embedding_fingerprint = ?")
                .contains("d.embedding_model = ?")
                .contains("d.embedding_provider = ?")
                .contains("d.embedding_dimension = ?");
        assertThat(normalize(withoutModel.getSql())).doesNotContain("d.embedding_model = ?");

        assertThat(properties(withModel))
                .contains("userId", "spaceId", "embeddingFingerprint", "embeddingModel",
                        "embeddingProvider", "embeddingDimension");
        assertThat(properties(withoutModel)).containsExactlyInAnyOrder("userId", "spaceId");
    }

    @Test
    void legacyDocumentsWithoutFingerprintStillMatchByModelProviderAndDimension() {
        BoundSql bound = boundSql("hash-v1", "hash", 256, "v1:abc");
        String sql = normalize(bound.getSql());

        // 迁移前 provider/dimension/fingerprint 为 NULL 的文档按 model 兼容，
        // 不能因为升级而全部消失；但若记录了不同的 provider/dimension 必须排除。
        assertThat(sql).contains("d.embedding_fingerprint IS NULL");
        assertThat(sql).contains("d.embedding_model = ?");
        assertThat(sql).contains("d.embedding_provider IS NULL");
        assertThat(sql).contains("d.embedding_dimension IS NULL");
    }

    private static BoundSql boundSql(
            String embeddingModel,
            String embeddingProvider,
            Integer embeddingDimension,
            String embeddingFingerprint) {
        Map<String, Object> params = new HashMap<>();
        params.put("userId", 1L);
        params.put("spaceId", 2L);
        params.put("embeddingModel", embeddingModel);
        params.put("embeddingProvider", embeddingProvider);
        params.put("embeddingDimension", embeddingDimension);
        params.put("embeddingFingerprint", embeddingFingerprint);
        return statement.getBoundSql(params);
    }

    private static java.util.List<String> properties(BoundSql bound) {
        return bound.getParameterMappings().stream()
                .map(ParameterMapping::getProperty)
                .toList();
    }

    private static String normalize(String sql) {
        return sql.replaceAll("\\s+", " ").trim();
    }
}
