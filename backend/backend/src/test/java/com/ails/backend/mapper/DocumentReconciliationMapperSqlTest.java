package com.ails.backend.mapper;

import org.apache.ibatis.mapping.BoundSql;
import org.apache.ibatis.mapping.MappedStatement;
import org.apache.ibatis.session.Configuration;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;

import java.time.LocalDateTime;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RAG-02/RAG-03 新增注解 SQL 的解析测试（不连接数据库）。
 *
 * <p>覆盖 {@code <script>}、{@code <foreach>} 与 UPDATE 的条件分支，确保应用启动
 * 时不会因 OGNL/转义写错而映射失败；真实执行效果由 MySQL 集成测试验证。</p>
 */
class DocumentReconciliationMapperSqlTest {

    private static Configuration configuration;

    @BeforeAll
    static void parseMappers() {
        configuration = new Configuration();
        configuration.addMapper(DocumentIndexVersionMapper.class);
        configuration.addMapper(ScheduledTaskLeaseMapper.class);
        configuration.addMapper(DocumentChunkMapper.class);
        configuration.addMapper(DocumentMapper.class);
    }

    @Test
    void versionLookupExpandsEveryStaleVersionIntoAPlaceholder() {
        BoundSql bound = boundSql(
                DocumentIndexVersionMapper.class, "selectByVersions",
                Map.of("documentId", 42L, "versions", List.of(1, 2, 3)));
        String sql = normalize(bound.getSql());

        assertThat(sql)
                .contains("FROM document_index_version")
                .contains("document_id = ?")
                .contains("index_version IN (")
                .contains("AS indexVersion")
                .contains("AS vectorCollection");
        // foreach 会为每个元素生成内部参数名（__frch_version_N），这里只验证
        // “documentId + 三个版本”共 4 个绑定参数，不锁死 MyBatis 内部命名。
        java.util.List<String> properties = bound.getParameterMappings().stream()
                .map(mapping -> mapping.getProperty())
                .toList();
        assertThat(properties).hasSize(4);
        assertThat(properties.get(0)).isEqualTo("documentId");
        assertThat(properties.subList(1, 4))
                .allMatch(property -> property.startsWith("__frch_version_"));
    }

    @Test
    void versionDeleteUsesTheSameVersionSet() {
        BoundSql bound = boundSql(
                DocumentIndexVersionMapper.class, "deleteVersions",
                Map.of("documentId", 42L, "versions", List.of(0)));
        String sql = normalize(bound.getSql());

        assertThat(sql)
                .contains("DELETE FROM document_index_version")
                .contains("index_version IN (");
    }

    @Test
    void reconcilableScanOnlySelectsFullyIndexedReadyDocuments() {
        BoundSql bound = boundSql(
                DocumentMapper.class, "selectReconcilableDocuments", Map.of("limit", 200));
        String sql = normalize(bound.getSql());

        assertThat(sql)
                .contains("d.status = 'READY'")
                .contains("d.deleted_at IS NULL")
                .contains("d.index_started_at IS NULL")
                .contains("d.indexed_at IS NOT NULL")
                .contains("d.embedding_model IS NOT NULL")
                .contains("d.chunk_count > 0")
                .contains("d.index_version IS NOT NULL")
                .contains("LIMIT ?")
                // 轮转排序：最久未对账/从未对账优先，避免只检查固定的前 N 条。
                .contains("(d.vector_reconciliation_checked_at IS NULL) DESC")
                .contains("d.vector_reconciliation_checked_at ASC")
                // legacy 文档必须被扫描到并记为 UNLOCATABLE，不能在这里被过滤掉。
                .doesNotContain("d.vector_collection IS NOT NULL");
    }

    @Test
    void reconciliationUpdateNeverTouchesStatusOrContent() {
        Map<String, Object> params = new HashMap<>();
        params.put("id", 42L);
        params.put("indexVersion", 2);
        params.put("status", "MISMATCH");
        params.put("checkedAt", LocalDateTime.now());
        params.put("detail", "expected=3, actual=2");
        BoundSql bound = boundSql(DocumentMapper.class, "updateVectorReconciliation", params);
        String sql = normalize(bound.getSql());

        assertThat(sql)
                .contains("SET vector_reconciliation_status = ?")
                .contains("vector_reconciliation_checked_at = ?")
                .contains("vector_reconciliation_detail = ?")
                // 条件性写回：版本已切换时旧结果不得覆盖新版本。
                .contains("AND index_version = ?")
                .contains("AND status = 'READY'")
                .contains("AND deleted_at IS NULL")
                .doesNotContain("SET status =")
                .doesNotContain("index_error =");
    }

    @Test
    void activeVectorRefCountIsScopedToTheCheckedVersion() {
        BoundSql bound = boundSql(
                DocumentChunkMapper.class, "countActiveVectorRefs",
                Map.of("documentId", 42L, "indexVersion", 2));
        String sql = normalize(bound.getSql());

        assertThat(sql)
                .contains("FROM document_chunk")
                .contains("document_id = ?")
                .contains("index_version = ?")
                .contains("vector_ref IS NOT NULL")
                .contains("vector_ref <> ''");
    }

    @Test
    void leaseAcquireOnlyStealsExpiredLeases() {
        Map<String, Object> params = new HashMap<>();
        params.put("taskName", "document-vector-reconciliation");
        params.put("owner", "reconcile-1");
        params.put("now", LocalDateTime.now());
        params.put("expiresAt", LocalDateTime.now().plusMinutes(30));
        BoundSql bound = boundSql(ScheduledTaskLeaseMapper.class, "tryAcquire", params);
        String sql = normalize(bound.getSql());

        assertThat(sql)
                .contains("INSERT INTO scheduled_task_lease")
                .contains("ON DUPLICATE KEY UPDATE")
                .contains("IF(expires_at < ?");
        assertThat(bound.getParameterMappings())
                .extracting(mapping -> mapping.getProperty())
                .contains("taskName", "owner", "expiresAt", "now");
    }

    private static BoundSql boundSql(Class<?> mapper, String method, Map<String, Object> params) {
        MappedStatement statement = configuration.getMappedStatement(
                mapper.getName() + "." + method);
        return statement.getBoundSql(params);
    }

    private static String normalize(String sql) {
        return sql.replaceAll("\\s+", " ").trim();
    }
}
