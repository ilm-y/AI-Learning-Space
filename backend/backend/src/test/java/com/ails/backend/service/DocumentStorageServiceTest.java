package com.ails.backend.service;

import com.ails.backend.common.BusinessException;
import com.ails.backend.config.DocumentStorageProperties;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;
import org.springframework.mock.web.MockMultipartFile;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.catchThrowableOfType;

/**
 * 存储层测试：真实文件系统 + MockMultipartFile，不启动 Spring/MySQL。
 */
class DocumentStorageServiceTest {

    @TempDir
    Path root;

    private DocumentStorageService service(long maxSizeBytes) {
        return new DocumentStorageService(new DocumentStorageProperties(root.toString(), maxSizeBytes));
    }

    @Test
    void storesMarkdownWithHashAndServerGeneratedPath() throws IOException {
        DocumentStorageService service = service(1024 * 1024);
        byte[] bytes = "hello".getBytes(StandardCharsets.UTF_8);
        MockMultipartFile file = new MockMultipartFile(
                "file", "notes.md", "application/octet-stream", bytes);

        DocumentStorageService.PreparedFile prepared = service.prepare(file);

        assertThat(prepared.safeFileName()).isEqualTo("notes.md");
        assertThat(prepared.extension()).isEqualTo("md");
        assertThat(prepared.fileType()).isEqualTo("md");
        assertThat(prepared.size()).isEqualTo(bytes.length);
        assertThat(prepared.sha256())
                .isEqualTo("2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824");

        DocumentStorageService.StoredFile stored = service.store(10L, prepared);

        assertThat(stored.storageUri()).matches("space-10/[0-9a-f]{32}\\.md");
        assertThat(stored.storageUri()).doesNotContain("..");
        Path storedPath = root.resolve(stored.storageUri());
        assertThat(storedPath).exists();
        assertThat(Files.readAllBytes(storedPath)).isEqualTo(bytes);
        assertThat(stored.absolutePath()).isEqualTo(storedPath.toAbsolutePath().normalize());
    }

    @Test
    void storesTxtAsTxt() {
        DocumentStorageService service = service(1024);
        MockMultipartFile file = new MockMultipartFile(
                "file", "notes.txt", "text/plain", "plain".getBytes(StandardCharsets.UTF_8));

        DocumentStorageService.StoredFile stored = service.store(3L, service.prepare(file));

        assertThat(stored.storageUri()).matches("space-3/[0-9a-f]{32}\\.txt");
    }

    @Test
    void ignoresClientContentTypeAndUsesExtension() {
        DocumentStorageService service = service(1024);
        MockMultipartFile file = new MockMultipartFile(
                "file", "notes.md", "application/pdf", "# still markdown".getBytes(StandardCharsets.UTF_8));

        DocumentStorageService.PreparedFile prepared = service.prepare(file);

        assertThat(prepared.extension()).isEqualTo("md");
        assertThat(prepared.fileType()).isEqualTo("md");
    }

    @Test
    void sanitizesTraversalAbsoluteAndWindowsPaths() {
        DocumentStorageService service = service(1024);
        record Case(String clientName, String expected) {
        }
        List<Case> cases = List.of(
                new Case("../../evil.md", "evil.md"),
                new Case("..\\..\\evil.md", "evil.md"),
                new Case("/etc/passwd.md", "passwd.md"),
                new Case("C:\\temp\\evil.md", "evil.md"));

        for (Case c : cases) {
            MockMultipartFile file = new MockMultipartFile(
                    "file", c.clientName(), "text/markdown", "x".getBytes(StandardCharsets.UTF_8));
            DocumentStorageService.PreparedFile prepared = service.prepare(file);
            assertThat(prepared.safeFileName())
                    .as("client filename %s", c.clientName())
                    .isEqualTo(c.expected());
        }
    }

    @Test
    void rejectsUnsupportedExtension() {
        BusinessException error = catchThrowableOfType(() -> service(1024).prepare(
                new MockMultipartFile("file", "notes.pdf", "application/pdf", "x".getBytes())),
                BusinessException.class);

        assertThat(error.getCode()).isEqualTo(400);
        assertThat(error.getMessage()).contains(".md").contains(".txt");
    }

    @Test
    void rejectsMissingExtension() {
        BusinessException error = catchThrowableOfType(() -> service(1024).prepare(
                new MockMultipartFile("file", "notes", "text/plain", "x".getBytes())),
                BusinessException.class);

        assertThat(error.getCode()).isEqualTo(400);
    }

    @Test
    void rejectsFileNameThatIsOnlyExtension() {
        BusinessException error = catchThrowableOfType(() -> service(1024).prepare(
                new MockMultipartFile("file", ".md", "text/markdown", "x".getBytes())),
                BusinessException.class);

        assertThat(error.getCode()).isEqualTo(400);
        assertThat(error.getMessage()).contains("仅支持");
    }

    @Test
    void rejectsTraversalOnlyFileName() {
        BusinessException error = catchThrowableOfType(() -> service(1024).prepare(
                new MockMultipartFile("file", "../", "text/markdown", "x".getBytes())),
                BusinessException.class);

        assertThat(error.getCode()).isEqualTo(400);
        assertThat(error.getMessage()).contains("文件名");
    }

    @Test
    void rejectsNullFileName() {
        BusinessException error = catchThrowableOfType(() -> service(1024).prepare(
                new MockMultipartFile("file", null, "text/markdown", "x".getBytes())),
                BusinessException.class);

        assertThat(error.getCode()).isEqualTo(400);
        assertThat(error.getMessage()).contains("文件名");
    }

    @Test
    void rejectsEmptyFile() {
        BusinessException error = catchThrowableOfType(() -> service(1024).prepare(
                new MockMultipartFile("file", "empty.md", "text/markdown", new byte[0])),
                BusinessException.class);

        assertThat(error.getCode()).isEqualTo(400);
    }

    @Test
    void rejectsWhitespaceOnlyFile() {
        BusinessException error = catchThrowableOfType(() -> service(1024).prepare(
                new MockMultipartFile("file", "blank.txt", "text/plain",
                        "  \n\t  ".getBytes(StandardCharsets.UTF_8))),
                BusinessException.class);

        assertThat(error.getCode()).isEqualTo(400);
        assertThat(error.getMessage()).contains("内容为空");
    }

    @Test
    void rejectsOversizedFileBeforeWriting() {
        BusinessException error = catchThrowableOfType(() -> service(8).prepare(
                new MockMultipartFile("file", "big.md", "text/markdown",
                        "123456789".getBytes(StandardCharsets.UTF_8))),
                BusinessException.class);

        assertThat(error.getCode()).isEqualTo(413);
    }

    @Test
    void rejectsInvalidUtf8() {
        byte[] invalid = new byte[]{(byte) 0xC3, (byte) 0x28};
        BusinessException error = catchThrowableOfType(() -> service(1024).prepare(
                new MockMultipartFile("file", "bad.md", "text/markdown", invalid)),
                BusinessException.class);

        assertThat(error.getCode()).isEqualTo(400);
        assertThat(error.getMessage()).contains("UTF-8");
    }

    @Test
    void rejectsUtf16LikeNulBytes() {
        byte[] utf16le = "hi".getBytes(StandardCharsets.UTF_16LE);
        BusinessException error = catchThrowableOfType(() -> service(1024).prepare(
                new MockMultipartFile("file", "utf16.txt", "text/plain", utf16le)),
                BusinessException.class);

        assertThat(error.getCode()).isEqualTo(400);
        assertThat(error.getMessage()).contains("NUL");
    }

    @Test
    void doesNotOverwriteExistingFiles() {
        DocumentStorageService service = service(1024);
        DocumentStorageService.PreparedFile prepared = service.prepare(
                new MockMultipartFile("file", "same.md", "text/markdown", "same".getBytes()));

        DocumentStorageService.StoredFile first = service.store(7L, prepared);
        DocumentStorageService.StoredFile second = service.store(7L, prepared);

        assertThat(first.storageUri()).isNotEqualTo(second.storageUri());
        assertThat(root.resolve(first.storageUri())).exists();
        assertThat(root.resolve(second.storageUri())).exists();

        service.deleteQuietly(first.storageUri());
        assertThat(root.resolve(first.storageUri())).doesNotExist();
        assertThat(root.resolve(second.storageUri())).exists();
    }

    @Test
    void deleteQuietlyRefusesPathsOutsideRoot() throws IOException {
        DocumentStorageService service = service(1024);
        Path outside = root.getParent().resolve("outside-" + System.nanoTime() + ".md");
        Files.writeString(outside, "must survive");

        service.deleteQuietly("../" + outside.getFileName());
        service.deleteQuietly(outside.toAbsolutePath().toString());

        assertThat(outside).exists();
        Files.deleteIfExists(outside);
    }
}
