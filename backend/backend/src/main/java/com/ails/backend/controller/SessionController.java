package com.ails.backend.controller;

import com.ails.backend.common.BusinessException;
import lombok.RequiredArgsConstructor;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

/**
 * 学习会话（一次专注学习的开始与结束）—— <b>骨架，尚未接入</b>。
 * <p>
 * learning_session 表已在位。会话的意义是把分散的对话聚成「一次学习」，
 * 也是 agent_run.sessionId 的归属；等 Python Agent 接入后再实现。
 */
@RestController
@RequestMapping("/api/sessions")
@RequiredArgsConstructor
public class SessionController {

    private static final String NOT_READY = "学习会话能力尚未接入";

    @GetMapping("/current")
    public void current() {
        throw new BusinessException(501, NOT_READY);
    }

    @PostMapping
    public void start() {
        throw new BusinessException(501, NOT_READY);
    }

    @PostMapping("/{id}/complete")
    public void complete(@PathVariable Long id) {
        throw new BusinessException(501, NOT_READY);
    }
}
