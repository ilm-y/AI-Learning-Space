package com.ails.backend.service;

import com.ails.backend.common.BusinessException;
import com.ails.backend.config.DocumentStorageProperties;
import lombok.extern.slf4j.Slf4j;
import org.springframework.stereotype.Service;
import org.springframework.web.multipart.MultipartFile;

import java.io.IOException;
import java.nio.ByteBuffer;
import java.nio.charset.CharacterCodingException;
import java.nio.charset.CodingErrorAction;
import java.nio.charset.StandardCharsets;
import java.nio.file.AtomicMoveNotSupportedException;
import java.nio.file.FileAlreadyExistsException;
import java.nio.file.Files;
import java.nio.file.InvalidPathException;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.nio.file.StandardOpenOption;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.Locale;
import java.util.Set;
import java.util.UUID;
import java.util.regex.Pattern;

/**
 * 文档原文的落盘与基础校验。
 *
 * <p><b>信任边界：</b>客户端文件名、Content-Type、用户 ID 都不可信。这里只使用：
 * 服务端生成的空间 ID、UUID 文件名、以及按字节计算的 SHA-256。客户端文件名仅经过
 * 清理后作为展示用 {@code file_name} 保存，绝不参与磁盘路径拼接。</p>
 *
 * <p><b>落盘流程：</b>先在 {@code <root>/.tmp} 写临时文件，完整写入并校验大小后，
 * 用原子移动放到 {@code <root>/space-<id>/<uuid>.<ext>}。目标文件已存在时移动失败，
 * 绝不覆盖。临时文件在失败路径的 {@code finally} 中清理。</p>
 *
 * <p><b>路径安全：</b>最终路径必须规范化后仍位于存储根目录内；创建目录后再做一次
 * real-path 检查，防止 {@code space-<id>} 被替换成指向根目录之外的符号链接。</p>
 */
@Slf4j
@Service
public class DocumentStorageService {

    private static final Set<String> ALLOWED_EXTENSIONS = Set.of("md", "txt");

    /**
     * 控制字符（含 NUL）与双向控制字符（可伪造文件名的显示顺序）直接丢弃。
     * 换行、制表符也在 {@code \p{Cntrl}} 里，但它们只可能出现在客户端文件名中，
     * 不属于合法文件名的一部分。
     */
    private static final Pattern UNSAFE_FILE_NAME_CHARS =
            Pattern.compile("[\\p{Cntrl}\\u202A-\\u202E\\u2066-\\u2069]");

    private static final int MAX_FILE_NAME_LENGTH = 255;

    private final Path storageRoot;
    private final long maxSizeBytes;

    public DocumentStorageService(DocumentStorageProperties properties) {
        String configuredRoot = properties == null || !hasText(properties.root())
                ? DocumentStorageProperties.DEFAULT_ROOT
                : properties.root();
        long configuredMax = properties == null ? 0 : properties.maxSizeBytes();
        this.maxSizeBytes = configuredMax > 0
                ? configuredMax
                : DocumentStorageProperties.DEFAULT_MAX_SIZE_BYTES;

        Path root = Path.of(configuredRoot).toAbsolutePath().normalize();
        try {
            Files.createDirectories(root);
            if (!Files.isDirectory(root) || !Files.isWritable(root)) {
                throw new IllegalStateException("文档存储根目录不可写: " + root);
            }
            // 统一用 real path 做后续包含性判断，避免根目录本身是符号链接时比较失真。
            this.storageRoot = root.toRealPath();
        } catch (IOException e) {
            throw new IllegalStateException("初始化文档存储根目录失败: " + root, e);
        }
        log.info("Document storage root={}, maxSizeBytes={}", storageRoot, maxSizeBytes);
    }

    /**
     * 校验上传文件并读取原始字节，不落盘。
     * <p>
     * 先做校验再查询重复，重复文件不会产生任何临时文件或正式文件。
     */
    public PreparedFile prepare(MultipartFile file) {
        if (file == null || file.isEmpty()) {
            throw new BusinessException(400, "请选择要上传的非空文件");
        }
        if (file.getSize() > maxSizeBytes) {
            throw payloadTooLarge();
        }

        byte[] bytes;
        try {
            bytes = file.getBytes();
        } catch (IOException e) {
            throw new BusinessException(400, "读取上传文件失败");
        }
        if (bytes.length == 0) {
            throw new BusinessException(400, "文件内容为空");
        }
        if (bytes.length > maxSizeBytes) {
            throw payloadTooLarge();
        }

        String safeFileName = sanitizeFileName(file.getOriginalFilename());
        String extension = extensionOf(safeFileName);
        validateBaseName(safeFileName);

        String text = decodeUtf8(bytes);
        if (text.isBlank()) {
            throw new BusinessException(400, "文件内容为空");
        }
        if (text.indexOf('\u0000') >= 0) {
            // UTF-16/二进制文件可能“碰巧”是合法 UTF-8（ASCII 字符之间夹 NUL）。
            // 文本资料不应包含 NUL，直接拒绝比把二进制内容存进 RAG 更安全。
            throw new BusinessException(400, "文件必须是 UTF-8 编码的文本，不能包含 NUL 字节");
        }

        String sha256 = sha256Hex(bytes);
        // fileType 由扩展名推导，刻意不使用客户端的 Content-Type。
        return new PreparedFile(safeFileName, extension, extension, bytes, sha256, bytes.length);
    }

    /**
     * 将已校验的文件写入服务端生成的路径。
     *
     * @param spaceId  已经过 {@code LearningSpaceService.ownedSpace} 校验的空间 ID
     * @param prepared {@link #prepare(MultipartFile)} 的结果
     */
    public StoredFile store(Long spaceId, PreparedFile prepared) {
        if (spaceId == null || spaceId <= 0) {
            throw new IllegalArgumentException("spaceId 必须为正数");
        }
        if (prepared == null) {
            throw new IllegalArgumentException("prepared 不能为空");
        }

        String relativeDir = "space-" + spaceId;
        Path directory = storageRoot.resolve(relativeDir).normalize();
        assertInsideRoot(directory);

        Path temporary = null;
        try {
            Files.createDirectories(directory);
            assertRealPathInsideRoot(directory);

            String fileName = UUID.randomUUID().toString().replace("-", "")
                    + "." + prepared.extension();
            Path target = directory.resolve(fileName).normalize();
            assertInsideRoot(target);

            Path tempDirectory = storageRoot.resolve(".tmp").normalize();
            assertInsideRoot(tempDirectory);
            Files.createDirectories(tempDirectory);
            assertRealPathInsideRoot(tempDirectory);

            temporary = Files.createTempFile(tempDirectory, "upload-", ".part");
            byte[] bytes = prepared.bytes();
            Files.write(temporary, bytes,
                    StandardOpenOption.TRUNCATE_EXISTING, StandardOpenOption.WRITE);
            if (Files.size(temporary) != bytes.length) {
                throw new IOException("临时文件写入不完整: " + temporary);
            }

            try {
                Files.move(temporary, target, StandardCopyOption.ATOMIC_MOVE);
            } catch (AtomicMoveNotSupportedException e) {
                // 同一根目录通常支持原子移动；极端文件系统不支持时退回普通 move。
                // 仍然不传 REPLACE_EXISTING，因此不会覆盖已有文件。
                log.debug("文件系统不支持原子移动，退回普通 move: {}", target);
                Files.move(temporary, target);
            }
            temporary = null;

            String storageUri = relativeDir + "/" + fileName;
            log.info("Stored document file: uri={}, size={}", storageUri, bytes.length);
            return new StoredFile(storageUri, target, prepared.sha256());
        } catch (FileAlreadyExistsException e) {
            throw new BusinessException(409, "文件存储冲突，请重试");
        } catch (IOException e) {
            throw new IllegalStateException("保存文档文件失败", e);
        } finally {
            if (temporary != null) {
                try {
                    Files.deleteIfExists(temporary);
                } catch (IOException e) {
                    log.warn("清理上传临时文件失败: {}", temporary, e);
                }
            }
        }
    }

    /**
     * 读取存储根目录内的原文，供索引任务通过内部 HTTP 转交给 Agent。
     *
     * <p>与删除一样，只接受服务端写入的相对 URI。这里还做一次 real-path 校验，
     * 防止目录被替换成指向根目录之外的符号链接；读取上限复用单文件大小配置。</p>
     */
    public byte[] read(String storageUri) {
        if (!hasText(storageUri)) {
            throw new IllegalArgumentException("storageUri 不能为空");
        }
        Path relative;
        try {
            relative = Path.of(storageUri);
        } catch (InvalidPathException e) {
            throw new IllegalArgumentException("非法 storageUri: " + storageUri, e);
        }
        if (relative.isAbsolute()) {
            throw new IllegalArgumentException("拒绝读取绝对路径: " + storageUri);
        }

        Path target = storageRoot.resolve(relative).normalize();
        if (!target.startsWith(storageRoot) || target.equals(storageRoot)) {
            throw new IllegalArgumentException("拒绝读取存储根目录之外的路径: " + target);
        }
        try {
            Path real = target.toRealPath();
            if (!real.startsWith(storageRoot) || !Files.isRegularFile(real)) {
                throw new IllegalArgumentException("存储路径不是根目录内的普通文件: " + target);
            }
            long size = Files.size(real);
            if (size > maxSizeBytes) {
                throw new IllegalArgumentException("文档超过读取上限: " + size + " bytes");
            }
            return Files.readAllBytes(real);
        } catch (IOException e) {
            throw new IllegalStateException("读取文档原文失败: " + target, e);
        }
    }

    /**
     * 尽力删除已落盘的文件，用于数据库写入失败后的补偿。
     * <p>
     * 只接受存储根目录内的相对 URI；越界路径、绝对路径和目录一律拒绝并记录警告。
     * 删除失败不会抛出异常，避免掩盖原始数据库异常。
     */
    public void deleteQuietly(String storageUri) {
        if (!hasText(storageUri)) {
            return;
        }
        Path relative;
        try {
            relative = Path.of(storageUri);
        } catch (InvalidPathException e) {
            log.warn("拒绝删除非法存储 URI: {}", storageUri);
            return;
        }
        if (relative.isAbsolute()) {
            log.warn("拒绝删除绝对路径存储 URI: {}", storageUri);
            return;
        }

        Path target = storageRoot.resolve(relative).normalize();
        if (!target.startsWith(storageRoot) || target.equals(storageRoot)) {
            log.warn("拒绝删除存储根目录之外的路径: {}", target);
            return;
        }
        try {
            if (Files.isDirectory(target)) {
                log.warn("拒绝删除目录: {}", target);
                return;
            }
            if (Files.deleteIfExists(target)) {
                log.info("Deleted stored document file: {}", target);
            }
        } catch (IOException e) {
            // 文件可能已被外部清理；记录路径，便于人工追踪。
            log.warn("删除文档文件失败: {}", target, e);
        }
    }

    // ===================== 校验与工具 =====================

    private String sanitizeFileName(String originalFileName) {
        if (!hasText(originalFileName)) {
            throw new BusinessException(400, "文件名不能为空");
        }

        // 统一两种分隔符后只取最后一段：../../evil.md、/etc/passwd.md、
        // C:\temp\evil.md 都不会把路径带进磁盘。
        String name = originalFileName.replace('\\', '/');
        int lastSlash = name.lastIndexOf('/');
        if (lastSlash >= 0) {
            name = name.substring(lastSlash + 1);
        }
        name = UNSAFE_FILE_NAME_CHARS.matcher(name).replaceAll("").trim();

        if (name.isEmpty() || ".".equals(name) || "..".equals(name)) {
            throw new BusinessException(400, "文件名不合法");
        }
        if (name.length() > MAX_FILE_NAME_LENGTH) {
            int dot = name.lastIndexOf('.');
            String extension = dot > 0 ? name.substring(dot) : "";
            int keep = MAX_FILE_NAME_LENGTH - extension.length();
            name = name.substring(0, Math.max(1, keep)) + extension;
        }
        return name;
    }

    private String extensionOf(String fileName) {
        int dot = fileName.lastIndexOf('.');
        if (dot <= 0 || dot == fileName.length() - 1) {
            throw new BusinessException(400, "仅支持 .md 和 .txt 文件");
        }
        String extension = fileName.substring(dot + 1).toLowerCase(Locale.ROOT);
        if (!ALLOWED_EXTENSIONS.contains(extension)) {
            throw new BusinessException(400, "仅支持 .md 和 .txt 文件");
        }
        return extension;
    }

    private void validateBaseName(String fileName) {
        int dot = fileName.lastIndexOf('.');
        String baseName = dot > 0 ? fileName.substring(0, dot) : "";
        if (baseName.isBlank() || ".".equals(baseName) || "..".equals(baseName)) {
            throw new BusinessException(400, "文件名不合法");
        }
    }

    private String decodeUtf8(byte[] bytes) {
        try {
            return StandardCharsets.UTF_8.newDecoder()
                    .onMalformedInput(CodingErrorAction.REPORT)
                    .onUnmappableCharacter(CodingErrorAction.REPORT)
                    .decode(ByteBuffer.wrap(bytes))
                    .toString();
        } catch (CharacterCodingException e) {
            throw new BusinessException(400, "文件必须是 UTF-8 编码的文本");
        }
    }

    private void assertInsideRoot(Path path) {
        if (!path.startsWith(storageRoot) || path.equals(storageRoot)) {
            throw new IllegalStateException("文档存储路径越界: " + path);
        }
    }

    private void assertRealPathInsideRoot(Path path) throws IOException {
        Path realPath = path.toRealPath();
        if (!realPath.startsWith(storageRoot)) {
            throw new IOException("文档存储目录解析后越界: " + realPath);
        }
    }

    private BusinessException payloadTooLarge() {
        long maxMiB = maxSizeBytes / (1024 * 1024);
        String limit = maxMiB > 0 ? maxMiB + " MB" : maxSizeBytes + " 字节";
        return new BusinessException(413, "文件超过大小上限（" + limit + "）");
    }

    private static boolean hasText(String value) {
        return value != null && !value.isBlank();
    }

    private static String sha256Hex(byte[] bytes) {
        try {
            byte[] digest = MessageDigest.getInstance("SHA-256").digest(bytes);
            StringBuilder hex = new StringBuilder(digest.length * 2);
            for (byte b : digest) {
                hex.append(Character.forDigit((b >> 4) & 0xF, 16));
                hex.append(Character.forDigit(b & 0xF, 16));
            }
            return hex.toString();
        } catch (NoSuchAlgorithmException e) {
            // SHA-256 是 JDK 强制实现的算法
            throw new IllegalStateException("JVM 不支持 SHA-256", e);
        }
    }

    /**
     * 校验通过、等待落盘的文件。
     *
     * @param safeFileName 清理后的原文件名（仅用于展示和 DB.file_name）
     * @param extension    小写扩展名（md/txt），用于生成 UUID 文件名
     * @param fileType     服务端推导的类型，当前等于 extension
     * @param bytes        原始字节
     * @param sha256       原始字节的 SHA-256（64 位小写十六进制）
     * @param size         字节数
     */
    public record PreparedFile(
            String safeFileName,
            String extension,
            String fileType,
            byte[] bytes,
            String sha256,
            long size) {
    }

    /**
     * 已落盘文件。
     *
     * @param storageUri 存储根目录下的相对 URI，使用 {@code /} 分隔
     * @param absolutePath 规范化后的绝对路径
     * @param sha256     原始字节的 SHA-256
     */
    public record StoredFile(String storageUri, Path absolutePath, String sha256) {
    }
}
