package com.ails.backend.entity;

import com.ails.backend.enums.ImportSourceType;
import com.ails.backend.enums.ImportTaskStatus;
import com.baomidou.mybatisplus.annotation.FieldFill;
import com.baomidou.mybatisplus.annotation.IdType;
import com.baomidou.mybatisplus.annotation.TableField;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableName;
import lombok.Data;

import java.time.LocalDateTime;

/**
 * 历史对话导入任务（import_task）。
 */
@Data
@TableName("import_task")
public class ImportTask {

    @TableId(type = IdType.AUTO)
    private Long id;

    private Long userId;

    private Long learningSpaceId;

    private String fileName;

    private ImportSourceType sourceType;

    private ImportTaskStatus status;

    /** 进度 0~100 */
    private Integer progress;

    private Integer totalMessages;

    private String errorMessage;

    @TableField(fill = FieldFill.INSERT)
    private LocalDateTime createdAt;

    @TableField(fill = FieldFill.INSERT_UPDATE)
    private LocalDateTime updatedAt;
}
