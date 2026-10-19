package com.ails.backend.controller;

import com.ails.backend.common.ApiResponse;
import com.ails.backend.dto.session.SessionRequest;
import com.ails.backend.dto.session.SessionResponse;
import com.ails.backend.dto.session.SessionUpdateRequest;
import com.ails.backend.enums.SessionStatus;
import com.ails.backend.service.SessionService;
import com.ails.backend.utils.SecurityUtil;
import jakarta.validation.Valid;
import lombok.RequiredArgsConstructor;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PatchMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import java.util.List;

/**
 * 学习活动（一段围绕明确学习目标展开的连续学习，可以跨多个 Conversation）。
 * <p>
 * <b>绝大多数 Session 不是通过这些接口产生的。</b> 它们是懒创建的：新建 Conversation 时
 * {@code session_id} 是 NULL，用户发出第一条有效学习消息后由
 * {@code SessionResolver} 决定加入已有 Session 还是新建一个。
 * 这里的 {@code POST} 只服务于「用户明确要求另起一段」这一种情形。
 * <p>
 * Session 不承载长期认知 —— Evidence / Understanding / Memory 都不在这里，
 * 结束一个 Session 也不会让它们消失（见 {@code docs/session-design.md} §十）。
 */
@RestController
@RequestMapping("/api/sessions")
@RequiredArgsConstructor
public class SessionController {

    private final SessionService sessionService;

    /**
     * 当前学习活动：某空间下最近活跃的那一条。
     * <p>
     * 没有进行中的 Session 时返回 {@code data: null} 而不是 404 —— 这不是错误状态，
     * 新用户第一次打开就是这样的，返回 404 只会迫使每个调用方都把「还没开始学」
     * 当成异常来处理。
     */
    @GetMapping("/current")
    public ApiResponse<SessionResponse> current(@RequestParam(required = false) Long spaceId) {
        return ApiResponse.ok(sessionService.current(SecurityUtil.currentUserId(), spaceId));
    }

    @GetMapping
    public ApiResponse<List<SessionResponse>> list(@RequestParam(required = false) Long spaceId,
                                                   @RequestParam(required = false) SessionStatus status) {
        return ApiResponse.ok(sessionService.list(SecurityUtil.currentUserId(), spaceId, status));
    }

    @GetMapping("/{id}")
    public ApiResponse<SessionResponse> get(@PathVariable Long id) {
        return ApiResponse.ok(sessionService.get(SecurityUtil.currentUserId(), id));
    }

    /** 显式开一个新的学习目标 */
    @PostMapping
    public ApiResponse<SessionResponse> start(@Valid @RequestBody SessionRequest request) {
        return ApiResponse.ok("已开始新的学习", sessionService.start(SecurityUtil.currentUserId(), request));
    }

    /**
     * 改标题 / 目标 / 当前焦点，或推进状态。
     * <p>
     * 状态里有 {@code PAUSED} 和 {@code ABANDONED} 两个只能从这里到达 ——
     * 它们不是「完成」，系统不会替用户做这个判断。
     */
    @PatchMapping("/{id}")
    public ApiResponse<SessionResponse> update(@PathVariable Long id,
                                               @RequestBody SessionUpdateRequest request) {
        return ApiResponse.ok(sessionService.update(SecurityUtil.currentUserId(), id, request));
    }

    /** 学习目标达成。认知留在这里产生的 Evidence / Understanding 里，不受影响。 */
    @PostMapping("/{id}/complete")
    public ApiResponse<SessionResponse> complete(@PathVariable Long id) {
        return ApiResponse.ok("已完成", sessionService.complete(SecurityUtil.currentUserId(), id));
    }
}
