# BC-003 SSE 流结束时被安全过滤链硬断

- **发现日期**：2026-10-21
- **影响范围**：所有 `text/event-stream` 接口（当前是发消息）
- **严重程度**：高 —— 客户端收不到 chunked 终止块，流式库按「连接异常」处理

## 复现输入

```bash
curl -N -X POST http://127.0.0.1:8080/api/conversations/{id}/messages \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"content":"TLB 是什么？"}'
```

## 错误行为

事件本身全部到达（`message.start` / `delta*` / `message.end`），随后连接被硬关：

```text
curl: (18) transfer closed with outstanding read data remaining
httpx: RemoteProtocolError: peer closed connection without sending complete message body
       (incomplete chunked read)
```

服务端日志：

```text
AccessDeniedException: Access Denied
  at AuthorizationFilter ...
Unable to handle the Spring Security Exception because the response is already committed.
```

## 根因

`SseEmitter#complete()` 会让容器对同一个请求再做一次 **ASYNC 分发**，重新走一遍
过滤器链。`JwtAuthenticationFilter` 继承 `OncePerRequestFilter`，而后者默认
`shouldNotFilterAsyncDispatch() == true` —— 这一次分发里没有重新解析 JWT，
`SecurityContext` 为空，`anyRequest().authenticated()` 抛出 `AccessDeniedException`。

此时响应早已提交（`Content-Type: text/event-stream`），Spring Security 的异常处理
写不进去任何 401 响应体，只能终止连接，于是 chunked 编码缺少终止块。

值得注意的是：用户已经收到了完整的 `message.end`，所以「界面上看起来是好的」，
只有严格校验传输完整性的客户端（curl、httpx、Dart 的流式读取）才会暴露。

## 修复

`JwtAuthenticationFilter#shouldNotFilterAsyncDispatch` 返回 `false`：

```java
@Override
protected boolean shouldNotFilterAsyncDispatch() {
    return false;
}
```

请求头在同一个请求的所有分发之间是保留的，重新解析 `Authorization` 即可。
`JwtAuthenticationFilterTest` 用 `DispatcherType.ASYNC` 直接锁住这个行为。

## 回归用例

所有含 `sendMessage` 的 live 用例（如 `live-cross-session-session-attach`）：
修复前 5 条 SSE 用例全部因 `RemoteProtocolError` 失败，修复后 9/9 通过。
