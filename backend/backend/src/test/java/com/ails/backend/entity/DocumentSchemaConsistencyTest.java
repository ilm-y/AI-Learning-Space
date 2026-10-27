package com.ails.backend.entity;

import com.ails.backend.enums.DocumentStatus;
import org.junit.jupiter.api.Test;

import java.io.IOException;
import java.io.InputStream;
import java.lang.reflect.Field;
import java.lang.reflect.Modifier;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Locale;
import java.util.Set;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import java.util.stream.Collectors;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * schema.sql 与实体/枚举的静态一致性检查。
 * <p>
 * 不需要 MySQL：直接读 classpath 上的 schema.sql。目的是防止「实体加了字段、
 * schema.sql 忘了改」或「ENUM 值不一致」这类启动时不报错、运行时字段全为 null 的问题。
 * 迁移脚本做关键片段检查（列、生成列表达式、索引替换、重复检查），不解析完整 DDL 语法。
 */
class DocumentSchemaConsistencyTest {

    private static final Pattern TABLE_BLOCK = Pattern.compile(
            "CREATE TABLE IF NOT EXISTS `%s` \\((.*?)\\n\\) ENGINE", Pattern.DOTALL);
    private static final Pattern COLUMN = Pattern.compile("(?m)^\\s*`([a-z0-9_]+)`\\s+");
    private static final Pattern STATUS_ENUM = Pattern.compile(
            "`status`\\s+ENUM\\s*\\(([^)]*)\\)", Pattern.CASE_INSENSITIVE);

    private static final String SCHEMA = readResource("/schema.sql");
    private static final String MIGRATION = readResource("/schema-migration-document-rag.sql");

    /** 只服务数据库约束、刻意不映射到实体的派生列 */
    private static final Set<String> DERIVED_COLUMNS = Set.of("active_content_hash");

    @Test
    void documentEntityAndSchemaColumnsMatchExactly() {
        Set<String> schemaColumns = columnsOf(tableBlock("document"));
        Set<String> mappedColumns = new LinkedHashSet<>(schemaColumns);
        mappedColumns.removeAll(DERIVED_COLUMNS);

        assertThat(schemaColumns)
                .as("生成列 active_content_hash 必须存在于 schema.sql")
                .containsAll(DERIVED_COLUMNS);
        assertThat(mappedColumns)
                .as("除生成列外，schema.sql 的 document 列与 Document 实体字段必须双向一致")
                .containsExactlyInAnyOrderElementsOf(entityColumnsOf(Document.class));
    }

    @Test
    void activeContentHashIsGeneratedFromSoftDeleteStateAndNotMapped() {
        String block = tableBlock("document");

        assertThat(block)
                .as("active_content_hash 必须是 STORED 生成列，软删后取 NULL")
                .contains("`active_content_hash` CHAR(64)")
                .contains("GENERATED ALWAYS AS (CASE WHEN `deleted_at` IS NULL THEN `content_hash` ELSE NULL END) STORED");

        assertThat(entityColumnsOf(Document.class))
                .as("生成列只服务数据库约束，不应映射为实体字段")
                .doesNotContain("active_content_hash");
    }

    @Test
    void uniqueIndexIsActiveOnly() {
        String block = tableBlock("document");

        assertThat(block)
                .as("唯一索引必须建在 active_content_hash 上，软删记录才能释放 hash")
                .contains("UNIQUE KEY `uk_doc_space_active_hash` (`learning_space_id`, `active_content_hash`)");
        assertThat(block)
                .as("不能保留包含软删记录的旧唯一索引")
                .doesNotContain("UNIQUE KEY `uk_doc_space_hash`");
    }

    @Test
    void documentChunkEntityAndSchemaColumnsMatchExactly() {
        Set<String> schemaColumns = columnsOf(tableBlock("document_chunk"));
        Set<String> entityColumns = entityColumnsOf(DocumentChunk.class);

        assertThat(schemaColumns)
                .as("schema.sql 的 document_chunk 列与 DocumentChunk 实体字段必须双向一致")
                .containsExactlyInAnyOrderElementsOf(entityColumns);
    }

    @Test
    void documentStatusEnumMatchesSchemaEnumInOrder() {
        Matcher matcher = STATUS_ENUM.matcher(tableBlock("document"));
        assertThat(matcher.find()).as("schema.sql 中找不到 document.status 的 ENUM 定义").isTrue();

        List<String> schemaValues = Arrays.stream(matcher.group(1).split(","))
                .map(String::trim)
                .map(value -> value.replace("'", ""))
                .collect(Collectors.toList());

        List<String> javaValues = Arrays.stream(DocumentStatus.values())
                .map(Enum::name)
                .collect(Collectors.toList());

        assertThat(schemaValues)
                .as("ENUM 顺序决定 MySQL 的存储序号，必须与 Java 枚举声明顺序一致")
                .containsExactlyElementsOf(javaValues);
    }

    @Test
    void migrationScriptCoversEveryRagColumnAndGuard() {
        List<String> ragColumns = List.of(
                "user_id", "content_hash", "active_content_hash", "chunk_count",
                "index_attempts", "index_error", "embedding_model", "index_version",
                "indexed_at", "deleted_at");

        for (String column : ragColumns) {
            assertThat(MIGRATION)
                    .as("迁移脚本缺少列 %s", column)
                    .contains("`" + column + "`");
        }
        assertThat(MIGRATION)
                .as("迁移脚本必须建立 active-only 唯一约束")
                .contains("uk_doc_space_active_hash")
                .contains("CASE WHEN `deleted_at` IS NULL THEN `content_hash` ELSE NULL END");
        assertThat(MIGRATION)
                .as("迁移脚本必须替换旧索引，且不能再创建旧索引")
                .contains("DROP INDEX `uk_doc_space_hash`")
                .doesNotContain("ADD UNIQUE KEY `uk_doc_space_hash`");
        assertThat(MIGRATION)
                .as("迁移前必须检查有效记录的重复 hash")
                .contains("duplicate active (learning_space_id, content_hash)");
        assertThat(MIGRATION)
                .as("迁移脚本在旧数据有歧义时必须中止而不是破坏数据")
                .contains("SIGNAL SQLSTATE '45000'");
    }

    // ===================== helpers =====================

    private static String tableBlock(String table) {
        Pattern pattern = Pattern.compile(String.format(TABLE_BLOCK.pattern(), table), Pattern.DOTALL);
        Matcher matcher = pattern.matcher(SCHEMA);
        assertThat(matcher.find()).as("schema.sql 中找不到表 %s", table).isTrue();
        return matcher.group(1);
    }

    private static Set<String> columnsOf(String tableBlock) {
        Matcher matcher = COLUMN.matcher(tableBlock);
        Set<String> columns = new LinkedHashSet<>();
        while (matcher.find()) {
            columns.add(matcher.group(1));
        }
        return columns;
    }

    private static Set<String> entityColumnsOf(Class<?> entity) {
        List<String> names = new ArrayList<>();
        for (Field field : entity.getDeclaredFields()) {
            if (field.isSynthetic() || Modifier.isStatic(field.getModifiers())) {
                continue;
            }
            names.add(camelToSnake(field.getName()));
        }
        return new LinkedHashSet<>(names);
    }

    private static String camelToSnake(String name) {
        return name.replaceAll("([a-z0-9])([A-Z])", "$1_$2").toLowerCase(Locale.ROOT);
    }

    private static String readResource(String path) {
        try (InputStream in = DocumentSchemaConsistencyTest.class.getResourceAsStream(path)) {
            if (in == null) {
                throw new IllegalStateException("classpath 上找不到 " + path);
            }
            return new String(in.readAllBytes(), StandardCharsets.UTF_8);
        } catch (IOException e) {
            throw new IllegalStateException("读取 " + path + " 失败", e);
        }
    }
}
