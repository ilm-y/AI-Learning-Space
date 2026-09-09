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
 * 导入任务表（import_task，异步）
 */
@Data
@TableName("import_task")
public class ImportTask {

    @TableId(type = IdType.AUTO)
    private Long id;

    private Long userId;

    /** 导入到哪个空间 */
    private Long learningSpaceId;

    private String fileName;

    private ImportSourceType sourceType;

    private ImportTaskStatus status;

    /** 进度百分比 0~100 */
    private Integer progress;

    /** 失败原因 */
    private String errorMessage;

    /** 总共解析出多少条消息 */
    private Integer totalMessages;

    @TableField(fill = FieldFill.INSERT)
    private LocalDateTime createdAt;

    @TableField(fill = FieldFill.INSERT_UPDATE)
    private LocalDateTime updatedAt;
}
