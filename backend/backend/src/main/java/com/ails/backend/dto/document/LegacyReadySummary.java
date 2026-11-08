package com.ails.backend.dto.document;

import lombok.Data;

/**
 * legacy READY 文档的聚合行，仅供 ready-ids 路径的观测日志使用。
 *
 * <p>用一条 GROUP BY 查询得到“有多少条、涉及哪些 model/collection”，避免为了打日志
 * 把明细全量拉进内存。</p>
 */
@Data
public class LegacyReadySummary {

    private String embeddingModel;

    private String vectorCollection;

    private Integer documentCount;
}
