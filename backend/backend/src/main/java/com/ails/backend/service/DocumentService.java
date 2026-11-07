package com.ails.backend.service;

import com.ails.backend.common.BusinessException;
import com.ails.backend.dto.document.DocumentResponse;
import com.ails.backend.dto.document.DocumentUploadResponse;
import com.ails.backend.dto.document.ReadyDocument;
import com.ails.backend.entity.Document;
import com.ails.backend.entity.LearningSpace;
import com.ails.backend.enums.DocumentStatus;
import com.ails.backend.mapper.DocumentMapper;
import com.baomidou.mybatisplus.core.conditions.query.LambdaQueryWrapper;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.dao.DuplicateKeyException;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Propagation;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.util.StringUtils;
import org.springframework.web.multipart.MultipartFile;

import java.util.List;

/**
 * 文档上传、列表、详情、重建与删除入口。
 *
 * <p><b>权限：</b>所有 Space 级操作先调用 {@link LearningSpaceService#ownedSpace(Long, Long)}；
 * 文档详情再校验文档的 {@code user_id} 与 Space 归属。他人资源统一返回 404，
 * 且详情接口对“不存在”和“不属于当前用户”使用同一条文案，避免探测文档是否存在。</p>
 *
 * <p><b>去重：</b>先按 {@code (spaceId, contentHash)} 查有效文档做快速路径；真正的并发
 * 防线是数据库唯一键 {@code uk_doc_space_active_hash}。插入命中 {@link DuplicateKeyException}
 * 时删除刚写入的文件，并读取并发请求已经创建的记录。</p>
 *
 * <p><b>为什么 upload 不加入外层事务：</b>整个流程是「文件落盘 + 单条 insert」。单条 insert
 * 自身原子；更重要的是，重复键失败后需要在<b>新的事务快照</b>里读到并发胜者的记录。
 * 如果套在 REPEATABLE READ 的同一个事务里，重复检查时建立的快照可能看不到后提交的胜者。
 * 因此这里显式 {@link Propagation#NOT_SUPPORTED}，让每次 mapper 调用独立提交。</p>
 */
@Slf4j
@Service
@RequiredArgsConstructor
public class DocumentService {

    private final DocumentMapper documentMapper;
    private final LearningSpaceService spaceService;
    private final DocumentStorageService storageService;
    private final DocumentIndexService indexService;
    private final DocumentDeletionService deletionService;

    @Transactional(readOnly = true)
    public List<DocumentResponse> listBySpace(Long userId, Long spaceId) {
        spaceService.ownedSpace(userId, spaceId);
        return documentMapper.selectList(new LambdaQueryWrapper<Document>()
                        .eq(Document::getLearningSpaceId, spaceId)
                        // user_id 是冗余 owner；这里再带一次做纵深防御。
                        .eq(Document::getUserId, userId)
                        .orderByDesc(Document::getCreatedAt)
                        .orderByDesc(Document::getId))
                .stream().map(DocumentResponse::from).toList();
    }

    /**
     * 返回当前用户当前 Space 中可以被检索的文档及其<b>活跃索引版本</b>。
     *
     * <p>这是 Agent 知识检索的权限白名单：先做 Space 归属校验，再由 Mapper
     * 在 SQL 里同时检查 READY、未软删、索引完整。Embedding 身份由 Agent 传入
     * 当前真实生效的 provider/model/dimension/fingerprint，用来排除不兼容的旧索引；
     * 不传 model 时返回全部，供管理/排查使用。</p>
     */
    @Transactional(readOnly = true)
    public List<ReadyDocument> listReadyDocuments(Long userId, Long spaceId,
                                                  String embeddingModel,
                                                  String embeddingProvider,
                                                  Integer embeddingDimension,
                                                  String embeddingFingerprint) {
        spaceService.ownedSpace(userId, spaceId);
        String model = trimToNull(embeddingModel, 128, "embeddingModel");
        String provider = trimToNull(embeddingProvider, 64, "embeddingProvider");
        String fingerprint = trimToNull(embeddingFingerprint, 128, "embeddingFingerprint");
        if (embeddingDimension != null && embeddingDimension <= 0) {
            throw new BusinessException(400, "embeddingDimension 必须为正整数");
        }
        // fingerprint 是不透明的版本化摘要，必须与 model 一起使用；单独传 fingerprint
        // 无法解释旧文档（迁移前 fingerprint 为 NULL）。
        if (fingerprint != null && model == null) {
            throw new BusinessException(400, "传入 embeddingFingerprint 时必须同时传入 embeddingModel");
        }
        return documentMapper.selectReadyDocuments(
                userId, spaceId, model, provider, embeddingDimension, fingerprint);
    }

    @Transactional(readOnly = true)
    public DocumentResponse get(Long userId, Long documentId) {
        Document document = requireOwnedDocument(userId, documentId);
        return DocumentResponse.from(document);
    }

    /**
     * 上传 Markdown/TXT。返回新建文档或同一 Space 内的已有有效文档。
     */
    @Transactional(propagation = Propagation.NOT_SUPPORTED)
    public DocumentUploadResponse upload(Long userId, Long spaceId, MultipartFile file) {
        // 权限先于文件校验：他人 Space 不应因为文件内容而得到不同的错误信息。
        LearningSpace space = spaceService.ownedSpace(userId, spaceId);
        DocumentStorageService.PreparedFile prepared = storageService.prepare(file);

        Document existing = findActive(spaceId, prepared.sha256());
        if (existing != null) {
            return duplicateResponse(existing);
        }

        DocumentStorageService.StoredFile stored = storageService.store(spaceId, prepared);

        Document document = new Document();
        document.setLearningSpaceId(spaceId);
        // 用 Space 的实际 owner，而不是请求参数中的 userId（两者已由 ownedSpace 校验一致）。
        document.setUserId(space.getUserId());
        document.setContentHash(prepared.sha256());
        document.setName(displayName(prepared.safeFileName()));
        document.setFileName(prepared.safeFileName());
        document.setStorageUri(stored.storageUri());
        document.setFileType(prepared.fileType());
        document.setSize(prepared.size());
        // 先落 UPLOADING，索引任务成功抢占后才会进入 PROCESSING，最终由索引流程置 READY。
        document.setStatus(DocumentStatus.UPLOADING);
        document.setChunkCount(0);
        document.setIndexAttempts(0);
        document.setIndexVersion(0);
        document.setDeleteAttempts(0);

        try {
            int inserted = documentMapper.insert(document);
            if (inserted != 1) {
                throw new BusinessException(500, "文档保存失败");
            }
        } catch (DuplicateKeyException e) {
            // 并发上传同一文件：唯一键是最终防线。先删掉本次多写的文件，再读胜者记录。
            storageService.deleteQuietly(stored.storageUri());
            Document winner = findActive(spaceId, prepared.sha256());
            if (winner != null) {
                return duplicateResponse(winner);
            }
            throw BusinessException.conflict("文档已存在，请刷新后重试");
        } catch (RuntimeException e) {
            // insert 已失败，文件不能留下成为不可追踪的孤儿；删除失败时 storage 会记录路径。
            storageService.deleteQuietly(stored.storageUri());
            log.error("保存文档元数据失败: spaceId={}, storageUri={}",
                    spaceId, stored.storageUri(), e);
            throw e;
        }

        log.info("Uploaded document id={}, spaceId={}, userId={}, size={}, hash={}",
                document.getId(), spaceId, document.getUserId(), prepared.size(), prepared.sha256());

        // 记录已提交，索引本身异步执行；submit 会同步抢到 PROCESSING 或直接置 FAILED，
        // 因此这里重新读取一次，响应中返回真实状态、attempts 与错误信息。
        submitQuietly(document.getId());
        Document saved = documentMapper.selectById(document.getId());
        return new DocumentUploadResponse(DocumentResponse.from(saved != null ? saved : document), false);
    }

    /**
     * 主动重建索引。READY 文档在构建期间保持 READY（旧版本继续服务），
     * 构建失败也保持 READY；这是与“首次索引”不同的失败语义。
     */
    @Transactional(propagation = Propagation.NOT_SUPPORTED)
    public DocumentResponse reindex(Long userId, Long documentId) {
        Document document = requireOwnedDocument(userId, documentId);
        if (document.getStatus() == DocumentStatus.DELETING) {
            throw BusinessException.conflict("文档正在删除中，不能重建索引");
        }
        if (document.getStatus() == DocumentStatus.DELETED) {
            throw BusinessException.notFound("文档不存在");
        }
        submitQuietly(documentId);
        Document saved = documentMapper.selectById(documentId);
        return DocumentResponse.from(saved != null ? saved : document);
    }

    /**
     * 请求删除文档。同步把状态切到 DELETING（立即退出检索白名单），
     * 向量与文件的清理异步执行、可重试。重复删除幂等成功。
     */
    @Transactional(propagation = Propagation.NOT_SUPPORTED)
    public void delete(Long userId, Long documentId) {
        // 用不受 @TableLogic 影响的查询，才能区分“已删除（幂等成功）”与“不存在”。
        Document document = documentMapper.selectAnyById(documentId);
        if (document == null || userId == null || !userId.equals(document.getUserId())) {
            throw BusinessException.notFound("文档不存在");
        }
        try {
            spaceService.ownedSpace(userId, document.getLearningSpaceId());
        } catch (BusinessException e) {
            throw BusinessException.notFound("文档不存在");
        }
        if (document.getStatus() == DocumentStatus.DELETED || document.getDeletedAt() != null) {
            return;
        }
        deletionService.requestDelete(document);
    }

    /**
     * 上传去重命中时的行为：
     * <ul>
     *   <li>READY / PROCESSING：真正重复，直接返回；</li>
     *   <li>FAILED / UPLOADING：上次没索引成功，重新提交；</li>
     *   <li>DELETING：正在删除，重复上传需要等删除完成，返回 409。</li>
     * </ul>
     */
    private DocumentUploadResponse duplicateResponse(Document existing) {
        if (existing.getStatus() == DocumentStatus.DELETING) {
            throw BusinessException.conflict("文档正在删除中，请稍后重试");
        }
        if (existing.getStatus() == DocumentStatus.FAILED
                || existing.getStatus() == DocumentStatus.UPLOADING) {
            submitQuietly(existing.getId());
            Document refreshed = documentMapper.selectById(existing.getId());
            return new DocumentUploadResponse(
                    DocumentResponse.from(refreshed != null ? refreshed : existing), true);
        }
        return new DocumentUploadResponse(DocumentResponse.from(existing), true);
    }

    private void submitQuietly(Long documentId) {
        try {
            indexService.submit(documentId);
        } catch (RuntimeException e) {
            // 上传/重建记录已经保存，不能因为异步调度层的意外异常让客户端误以为失败。
            log.error("提交文档索引任务失败: documentId={}", documentId, e);
        }
    }

    private Document requireOwnedDocument(Long userId, Long documentId) {
        Document document = documentMapper.selectById(documentId);
        if (document == null || userId == null || !userId.equals(document.getUserId())) {
            throw BusinessException.notFound("文档不存在");
        }
        try {
            spaceService.ownedSpace(userId, document.getLearningSpaceId());
        } catch (BusinessException e) {
            // 不把「空间存在但不属于你」与「空间不存在」区分开，避免通过详情接口探测资源。
            throw BusinessException.notFound("文档不存在");
        }
        return document;
    }

    /**
     * 查询同一 Space 内的有效文档。{@code @TableLogic} 会自动附加
     * {@code deleted_at IS NULL}，所以软删记录不会命中，也不会占用 active hash。
     */
    private Document findActive(Long spaceId, String contentHash) {
        return documentMapper.selectOne(new LambdaQueryWrapper<Document>()
                .eq(Document::getLearningSpaceId, spaceId)
                .eq(Document::getContentHash, contentHash)
                .last("LIMIT 1"));
    }

    private static String trimToNull(String value, int max, String field) {
        if (!StringUtils.hasText(value)) {
            return null;
        }
        String trimmed = value.trim();
        if (trimmed.length() > max) {
            throw new BusinessException(400, field + " 超过 " + max + " 字符");
        }
        return trimmed;
    }

    private static String displayName(String fileName) {
        int dot = fileName.lastIndexOf('.');
        String name = (dot > 0 ? fileName.substring(0, dot) : fileName).trim();
        if (name.isEmpty()) {
            return fileName;
        }
        return name.length() <= 255 ? name : name.substring(0, 255);
    }
}
