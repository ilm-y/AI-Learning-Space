# 前端 API 契约（FRONTEND_API_SPEC）

> 面向：Vue 3 前端开发。用于生成 TypeScript 类型、封装请求层与 SSE 客户端。
>
> - **审计基线**：`backend/backend/src/main/java/com/ails/backend/`（2026-11-07，`main` 分支 `943dbc3`）
> - **审计范围**：`controller/` 全部 17 个 Controller、`dto/` 全部 56 个类，以及统一响应体、全局异常处理、Spring Security 配置、`ChatService` 的 SSE 事件契约。
> - **只读分析**：本文档由源码推导，未修改任何代码。
> - **以源码为准**：`docs/database&API_design.md` 中的接口章节（31 节）早于当前实现，路径与数量均已过时，请勿据此开发。

---

## 目录

1. [通用约定](#1-通用约定)
2. [Controller 总览](#2-controller-总览)
3. [SSE 流式对话](#3-sse-流式对话)
4. [接口明细](#4-接口明细)
5. [TypeScript 类型定义](#5-typescript-类型定义)
6. [DTO 字段清单](#6-dto-字段清单)
7. [接入注意事项与已知差异](#7-接入注意事项与已知差异)

---

## 1. 通用约定

### 1.1 Base URL

| 环境 | 地址 |
| --- | --- |
| 本地开发 | `http://localhost:8080` |

- 后端**没有设置 context-path**，所有接口路径都以 `/api` 开头，例如 `http://localhost:8080/api/spaces`。
- 建议前端用环境变量 `VITE_API_BASE_URL=http://localhost:8080`，请求层统一拼接 `/api/...`。
- CORS 已允许任意来源（`allowedOriginPatterns=*`），允许方法 `GET/POST/PUT/DELETE/PATCH/OPTIONS`，允许任意请求头，**不允许携带 Cookie**（`allowCredentials=false`）。前端直接跨域调用即可，无需代理；若用代理也可。

### 1.2 统一响应体

除 SSE 接口外，所有接口返回：

```java
public record ApiResponse<T>(int code, String message, T data)
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `code` | `number` | `0` 表示成功；非 `0` 时与 HTTP 状态码一致（400/401/403/404/409/413/422/500/501） |
| `message` | `string` | 成功时可能是 `"success"` 或业务提示（如 `"创建成功"`）；失败时为错误说明 |
| `data` | `T \| null` | 业务数据；`Void` 接口与失败响应为 `null` |

```json
{ "code": 0, "message": "success", "data": { } }
```

```json
{ "code": 404, "message": "文档不存在", "data": null }
```

> **判断成功请用 `code === 0`，不要只看 HTTP 状态。** 成功响应 HTTP 也是 200；失败时 HTTP 状态与 `code` 同步设置。

### 1.3 鉴权

- 除 `/api/auth/**` 与 `OPTIONS` 预检外，**所有接口都需要** `Authorization: Bearer <token>`。
- token 来自 `POST /api/auth/login` 或 `POST /api/auth/register` 的 `data.token`，默认有效期 7 天。
- 无状态 JWT，没有 Cookie、没有 refresh token、**没有 logout 接口**；前端退出登录只需丢弃本地 token。
- 401（未登录 / token 过期）与 403（已登录但无权访问）也返回统一 `ApiResponse`：

```json
{ "code": 401, "message": "未登录或 Token 已过期", "data": null }
{ "code": 403, "message": "没有权限访问该资源", "data": null }
```

> 资源归属校验通常把「不存在」和「不属于你」统一映射为 **404**（避免泄露资源是否存在），所以前端看到 404 时按「不存在或无权限」处理。

### 1.4 错误处理

| HTTP / code | 触发场景 | 前端处理建议 |
| --- | --- | --- |
| `400` | 参数校验失败、请求体格式错误、枚举值不合法、业务参数错误（如对话已归档） | 展示 `message`，表单场景可高亮字段 |
| `401` | 未携带 / 携带无效或过期 token | 清除本地 token，跳转登录 |
| `403` | 已认证但无权限 | 提示无权限 |
| `404` | 资源不存在或不属于当前用户 | 提示不存在 / 返回列表页 |
| `409` | 状态冲突（重复、已作答、正在删除、乐观锁失败等） | 提示刷新后重试 |
| `413` | 上传文件超过大小上限 | 提示文件过大 |
| `500` | 未预期异常 | 通用错误提示 |
| `501` | 功能尚未接入（导出 / 导入 / 学习计划） | 隐藏入口或提示「暂未开放」 |

参数校验失败的消息格式为 `"字段名 校验失败说明"`，例如：

```json
{ "code": 400, "message": "content 消息内容不能为空", "data": null }
```

### 1.5 数据格式约定

| Java 类型 | JSON / TS 类型 | 说明 |
| --- | --- | --- |
| `String` | `string` | camelCase 字段名 |
| `Long` / `long` | `number` | 主键、外键；当前量级远低于 `Number.MAX_SAFE_INTEGER` |
| `Integer` / `int` | `number` | |
| `Float` / `Double` / `double` | `number` | |
| `Boolean` / `boolean` | `boolean` | |
| `LocalDateTime` | `string` | ISO-8601 **本地时间、无时区**，如 `"2026-11-07T22:36:00"` |
| 枚举 | string union | 序列化为**大写常量名**，如 `"ACTIVE"` |
| `List<T>` | `T[]` | 不会出现 `null`，未命中时为 `[]`（除非字段本身可空） |

> **时间解析**：`new Date("2026-11-07T22:36:00")` 在浏览器中按**本地时区**解析，正好符合后端语义（后端用 `LocalDateTime`，无时区）。不要手动补 `Z`，否则会被当成 UTC。

### 1.6 方法语义与幂等性

- `DELETE` 在多数资源上是**软删除 / 归档**，不会物理删除历史：
  - 对话、知识点、学习空间：软删除；
  - 记忆：语义是**归档**（`ARCHIVED`），不是删除；
  - 文档：先置 `DELETING`，异步清理，重复删除**幂等成功**。
- `PUT` 是整体更新，未传的可空字段**会被清空**（如 `PUT /api/spaces/{id}` 不传 `description` 会清空描述）。
- `PATCH` 是部分更新，但各接口的必填校验不同，详见第 4 节。

---

## 2. Controller 总览

| # | Controller | 基础路径 | 接口数 | 说明 |
| --- | --- | --- | --- | --- |
| 1 | `AuthController` | `/api/auth` | 2 | 注册 / 登录（免鉴权） |
| 2 | `UserController` | `/api/user` | 3 | 当前用户资料 |
| 3 | `SpaceController` | `/api/spaces` | 7 | 学习空间 + 空间下的知识点 |
| 4 | `ConceptController` | 无类级前缀 | 3 | `/api/concepts/{id}` 查询 / 更新 / 删除 |
| 5 | `ConversationController` | 无类级前缀 | 6 | 空间下的对话子资源 + 对话自身管理 |
| 6 | `ChatController` | `/api/conversations` | 2 | 消息列表 + **SSE 发消息** |
| 7 | `DocumentController` | 无类级前缀 | 6 | 文档上传 / 列表 / 详情 / 重建 / 删除 |
| 8 | `HighlightController` | 无类级前缀 | 3 | AI 回复上的批注 |
| 9 | `UnderstandingController` | `/api/understandings` | 3 | 理解快照与历史（只读） |
| 10 | `EvidenceController` | 无类级前缀 | 2 | 证据链（只读） |
| 11 | `LearningStateController` | `/api/learning-state` | 4 | 学习状态 / 待关注 / 覆盖度 |
| 12 | `TestController` | `/api/tests` | 4 | 出题 / 待答 / 作答 |
| 13 | `MemoryController` | `/api/memories` | 7 | 长期记忆 CRUD + 检索 + 混合检索 |
| 14 | `SessionController` | `/api/sessions` | 6 | 学习活动 |
| 15 | `ExportController` | `/api/export` | 1 | **骨架，返回 501** |
| 16 | `ImportController` | `/api/imports` | 2 | **骨架，返回 501** |
| 17 | `LearningPlanController` | `/api/learning-plans` | 3 | **骨架，返回 501** |

**合计 64 个接口，其中 SSE 接口 1 个**（`POST /api/conversations/{id}/messages`）。

---

## 3. SSE 流式对话

这是全项目**唯一**的流式接口，也是前端最需要特殊处理的一个。

### 3.1 接口

```
POST /api/conversations/{id}/messages
Authorization: Bearer <token>
Content-Type: application/json
Accept: text/event-stream

{ "content": "用户这一轮说的话", "clientRequestId": "550e8400-e29b-41d4-a716-446655440000" }
```

`clientRequestId` **必填**（8–64 字符，建议 UUID），一次用户发送动作生成一次；
同一次发送的自动重试必须复用同一个值。`content` 最长 8000 字符，超长返回 400。

| 项 | 值 |
| --- | --- |
| Controller | `ChatController#sendMessage` |
| 返回类型 | `SseEmitter`（**不包 `ApiResponse`**） |
| 响应 Content-Type | `text/event-stream` |
| 连接超时 | **5 分钟**（`SSE_TIMEOUT_MS = 5 * 60 * 1000`） |
| 心跳事件 | **无**（服务端不发 comment 心跳） |
| 结束方式 | 成功发 `message.end` 后关闭；失败发 `message.error` 后关闭 |

### 3.2 事件契约

事件格式为标准 SSE：`event: <name>\ndata: <json>\n\n`，`data` 是 JSON。

| 事件名 | data 结构 | 时机 | 前端动作 |
| --- | --- | --- | --- |
| `message.start` | `{ runId: number, conversationId: number }` | 首事件，**必有** | 进入「生成中」状态，创建占位消息 |
| `message.delta` | `{ delta: string }` | 增量文本，若干次 | 追加到当前助手消息 |
| `message.end` | `{ messageId: number, runId: number, citations: CitationResponse[] }`，截断时追加 `{ truncated: true, finishReason: string \| null }` | 成功终态，**必有**（成功时） | 用 `messageId` 锚定消息行，渲染引用 |
| `message.error` | `{ runId: number, code: number, message: string }` | 失败终态 | 丢弃半成品，展示错误 |

示例：

```
event: message.start
data: {"runId":101,"conversationId":9}

event: message.delta
data: {"delta":"三次握手"}

event: message.delta
data: {"delta":"建立的是连接状态"}

event: message.end
data: {"messageId":88,"runId":101,"citations":[{"documentId":12,"chunkId":340,"chunkIndex":3,"sourceTitle":"tcp.md","available":true}]}
```

### 3.3 状态机

```
            message.start
   idle ───────────────────► streaming
                                │
                 ┌──────────────┼──────────────┐
                 │              │              │
        message.delta     message.end    message.error
                 │              │              │
                 └──► streaming │              │
                                ▼              ▼
                             success         error
```

- `message.error` 可能出现在任何阶段。收到后**必须丢弃已渲染的半成品**。
- 正常路径下 `message.start` → 若干 `message.delta` → `message.end`，随后服务端关闭连接。
- 若使用 `@microsoft/fetch-event-source` 之类的库，遇到连接关闭不要自动重连：一次 POST 就是一轮对话。若确需重试，必须复用同一个 `clientRequestId`，服务端会回放原结果（202/200/409），不会重复落库；不要用新 id 重发同一问题。

### 3.4 幂等回放（同 `clientRequestId` 重复 POST）

重放**不会**建立 SSE、不会重新生成，直接返回 JSON（强制
`application/json`），响应体形如：

```json
{
  "idempotentReplay": true,
  "state": "PROCESSING",
  "conversationId": 9,
  "userMessageId": 87,
  "runId": 101,
  "assistantMessageId": null,
  "runStatus": "RUNNING",
  "message": "请求处理中，请勿重复提交"
}
```

| 原请求状态 | HTTP | `state` | 前端处理 |
| --- | --- | --- | --- |
| 仍在生成（`RUNNING/PENDING/STALE`） | **202** | `PROCESSING` | 提示「请求处理中」，继续等待原连接或稍后重试；**不要**当成成功 |
| 已完成 / 截断 | **200** | `COMPLETED` / `TRUNCATED` | 用 `assistantMessageId` 拉取历史消息，不要重新生成 |
| USER 消息存在但无 run | **409** | `INCONSISTENT` | 提示用户换一个 `clientRequestId` 重发 |
| 原请求失败且无回复 | **409** | `FAILED` | 提示失败；重试必须使用新的 `clientRequestId` |
| 同 id、不同 `content` | **409** | `CLIENT_REQUEST_CONFLICT` | 客户端 bug：每次发送内容应生成新的 id |

### 3.5 重要行为

1. **用户消息在流开始前已同步落库。** 请求返回响应头之前，用户消息、Session 归属与
   `agent_run(RUNNING)` 已在同一事务里提交（失败则整体回滚）。因此流式过程中随时
   `GET /api/conversations/{id}/messages` 都能看到用户消息；助手消息只有收到
   `message.end` 后才落库。
2. **流建立前发生的错误返回普通 JSON，不是 SSE。** 以下情况在创建 `SseEmitter` 之前抛出，走全局异常处理：
   - 对话不存在 / 不属于当前用户 → `404`；
   - 对话已归档（`status != ACTIVE`）→ `400`，`message = "对话已归档，无法发送消息"`；
   - 请求体 `content` 为空或超过 8000 字符、`clientRequestId` 缺失或长度越界 → `400`；
   - 预创建事务失败（极少见）→ `500`，且不会留下用户消息。

   所以前端要**先看响应头**：`Content-Type: text/event-stream` 才按 SSE 解析，否则按 `ApiResponse` 解析错误。
3. **线程池饱和时**仍返回 SSE，但会立即推送 `message.start` + `message.error`（`code = 503`，`message = "服务繁忙，请稍后重试"`）。
4. `message.end.citations` 中的 `available` 恒为 `true`（实时链路刚通过检索白名单）。历史消息重新读取时可能变成 `false`。
5. `truncated = true` 表示模型因 Token 上限停止，文本可展示但不完整；此时**不会**触发后续学习分析。
6. 助手消息落库失败时推 `message.error`（`code = 500`，`message = "回复保存失败"`）。

### 3.5 前端实现要点

浏览器原生 `EventSource` **不支持 POST、不支持自定义请求头**，因此不能直接用。推荐：

- `fetch` + `ReadableStream` 手动解析 SSE；或
- `@microsoft/fetch-event-source`（支持 POST、headers、AbortController）。

伪代码：

```ts
const controller = new AbortController()
const res = await fetch(`${BASE}/api/conversations/${id}/messages`, {
  method: 'POST',
  headers: {
    'Content-Type': 'application/json',
    Accept: 'text/event-stream',
    Authorization: `Bearer ${token}`,
  },
  body: JSON.stringify({ content }),
  signal: controller.signal,
})

if (!res.headers.get('content-type')?.includes('text/event-stream')) {
  const err = await res.json() as ApiResponse<null>
  throw new Error(err.message)
}

// 按行解析：空行分隔事件；逐行累积 event: 与 data:
```

---

## 4. 接口明细

> 以下所有「响应」除 SSE 外均指 `ApiResponse<T>` 中的 `T`。所有接口默认需要 Bearer token（除特别标注）。

### 4.1 AuthController — `/api/auth`

| 方法 | 路径 | 鉴权 | 请求体 | 响应 `data` |
| --- | --- | --- | --- | --- |
| `POST` | `/api/auth/register` | 否 | `RegisterRequest` | `AuthResponse` |
| `POST` | `/api/auth/login` | 否 | `LoginRequest` | `AuthResponse` |

**`RegisterRequest`**

| 字段 | 类型 | 必填 | 约束 |
| --- | --- | --- | --- |
| `username` | string | 是 | 3~50 字符 |
| `password` | string | 是 | 6~72 字符 |
| `email` | string \| null | 否 | 邮箱格式 |

**`LoginRequest`**：`username`（必填）、`password`（必填）。

**`AuthResponse`**：`token: string`、`tokenType: string`（固定 `"Bearer"`）、`user: UserDTO`。

- 注册成功 `message = "注册成功"`；登录成功 `message = "success"`。
- 用户名重复 → `409`。

### 4.2 UserController — `/api/user`

| 方法 | 路径 | 请求体 | 响应 `data` |
| --- | --- | --- | --- |
| `GET` | `/api/user/me` | — | `UserDTO` |
| `PUT` | `/api/user/me` | `UpdateProfileRequest` | `UserDTO` |
| `PUT` | `/api/user/me/password` | `ChangePasswordRequest` | `null` |

- `UpdateProfileRequest`：`email?: string | null`（邮箱格式）。
- `ChangePasswordRequest`：`oldPassword: string`（必填）、`newPassword: string`（必填，6~72）。
- 修改密码成功 `message = "密码修改成功"`；旧密码错误 → `400`。
- `UserDTO` 永不含密码哈希。

### 4.3 SpaceController — `/api/spaces`

| 方法 | 路径 | 请求体 | 响应 `data` |
| --- | --- | --- | --- |
| `GET` | `/api/spaces` | — | `LearningSpaceResponse[]` |
| `POST` | `/api/spaces` | `LearningSpaceRequest` | `LearningSpaceResponse` |
| `GET` | `/api/spaces/{id}` | — | `LearningSpaceResponse` |
| `PUT` | `/api/spaces/{id}` | `LearningSpaceRequest` | `LearningSpaceResponse` |
| `DELETE` | `/api/spaces/{id}` | — | `null` |
| `GET` | `/api/spaces/{id}/concepts` | — | `ConceptResponse[]` |
| `POST` | `/api/spaces/{id}/concepts` | `ConceptRequest` | `ConceptResponse` |

**`LearningSpaceRequest`**

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `name` | string | 是 | 最长 100 |
| `description` | string \| null | 否 | |
| `visibility` | `SpaceVisibility \| null` | 否 | 创建时缺省 `PRIVATE`；更新时 `null` 表示不改 |

- `GET /api/spaces` 按 `createdAt` 倒序。
- `PUT` 不传 `description` 会**清空**描述。
- 创建成功 `message = "创建成功"`；删除成功 `message = "删除成功"`。

### 4.4 ConceptController — `/api/concepts`

| 方法 | 路径 | 请求体 | 响应 `data` |
| --- | --- | --- | --- |
| `GET` | `/api/concepts/{id}` | — | `ConceptResponse` |
| `PUT` | `/api/concepts/{id}` | `ConceptRequest` | `ConceptResponse` |
| `DELETE` | `/api/concepts/{id}` | — | `null` |

**`ConceptRequest`**

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `canonicalName` | string | 是 | 最长 255 |
| `displayName` | string \| null | 否 | 最长 100 |
| `description` | string \| null | 否 | |
| `difficultyLevel` | number \| null | 否 | 1~5 |

- 知识点列表与创建在空间子资源下（见 4.3）。
- `DELETE` 是软删除，已产生的理解与证据不受影响。
- `PUT` 不传 `difficultyLevel` 会**清空**难度。

### 4.5 ConversationController

| 方法 | 完整路径 | 请求体 | 响应 `data` |
| --- | --- | --- | --- |
| `GET` | `/api/spaces/{spaceId}/conversations` | — | `ConversationResponse[]` |
| `POST` | `/api/spaces/{spaceId}/conversations` | `ConversationRequest`（**可省略**） | `ConversationResponse` |
| `GET` | `/api/conversations/{id}` | — | `ConversationResponse` |
| `PUT` | `/api/conversations/{id}` | `ConversationRequest`（可省略） | `ConversationResponse` |
| `POST` | `/api/conversations/{id}/archive` | — | `ConversationResponse` |
| `DELETE` | `/api/conversations/{id}` | — | `null` |

**`ConversationRequest`**：`title?: string | null`（最长 200，可空 → AI 自动总结）。

- `POST` 创建时可以不传请求体；`title` 为 `null` 表示由系统生成标题。
- `PUT` 只更新 `title`，且仅在 `title` 非空白时生效；不会更新其他字段。
- `POST /archive`：归档后对话不再接受新消息；历史与证据保留。成功 `message = "已归档"`。
- `DELETE` 是软删除。
- `GET /api/spaces/{spaceId}/conversations` 按 `createdAt` 倒序。
- 创建成功 `message = "创建成功"`。

**`ConversationResponse`**

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | number | |
| `learningSpaceId` | number | |
| `sessionId` | number \| null | **为 null 是正常状态**：刚创建或闲聊未命中知识点 |
| `title` | string \| null | |
| `status` | `ConversationStatus` | `ACTIVE` / `ARCHIVED` |
| `createdAt` / `updatedAt` | string | ISO 本地时间 |

### 4.6 ChatController — 消息

| 方法 | 路径 | 请求体 | 响应 |
| --- | --- | --- | --- |
| `GET` | `/api/conversations/{id}/messages` | — | `ApiResponse<MessageResponse[]>` |
| `POST` | `/api/conversations/{id}/messages` | `MessageRequest` | **SSE**（见第 3 节） |

- `GET` 返回该对话全部消息，按 `sequenceNo` 升序、`id` 升序。
- `MessageRequest`：`content: string`（必填，非空白）。
- 用户消息 `role = "USER"`；助手消息 `role = "ASSISTANT"`；测试 / 事件消息由 `messageType` 区分。

**`MessageResponse`**

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | number | |
| `conversationId` | number | |
| `role` | `MessageRole` | `USER` / `ASSISTANT` / `SYSTEM` / `TOOL` |
| `messageType` | `MessageType` | `TEXT` / `TEST` / `EVENT` |
| `content` | string | Markdown 文本 |
| `tokens` | number \| null | |
| `sequenceNo` | number \| null | 对话内严格递增 |
| `createdAt` | string | |
| `truncated` | boolean | 助手回复是否因 Token 上限被截断 |
| `citations` | `CitationResponse[]` | 永不为 null；无引用时为 `[]` |

### 4.7 DocumentController

| 方法 | 完整路径 | 请求 | 响应 `data` |
| --- | --- | --- | --- |
| `POST` | `/api/spaces/{spaceId}/documents` | `multipart/form-data`，字段名 **`file`** | `DocumentUploadResponse` |
| `GET` | `/api/spaces/{spaceId}/documents` | — | `DocumentResponse[]` |
| `GET` | `/api/spaces/{spaceId}/documents/ready-ids` | query（见下） | `ReadyDocument[]` |
| `GET` | `/api/documents/{id}` | — | `DocumentResponse` |
| `POST` | `/api/documents/{id}/reindex` | — | `DocumentResponse` |
| `DELETE` | `/api/documents/{id}` | — | `null` |

#### 上传

```
POST /api/spaces/{spaceId}/documents
Content-Type: multipart/form-data
file: <File>
```

- 仅支持 **`.md` / `.txt`**，必须 UTF-8 文本、不能含 NUL 字节。
- 应用层单文件上限 **10 MiB**（`ails.document.storage.max-size-bytes`）；Spring multipart 上限 20 MB。
- 超限 → `413`，`message = "文件超过大小上限（10 MB）"` 或 Controller 层 `"文件超过大小上限"`。
- 同空间内**内容哈希相同**的文件视为重复：不新建记录，返回已有文档，`duplicate = true`，`message = "文件已存在"`。
- 上传立即返回，索引异步执行；响应中的 `status` 是**真实状态**（通常是 `UPLOADING` 或 `PROCESSING`，不会伪造 `READY`）。前端需要轮询 `GET /api/documents/{id}` 或列表接口观察 `status`。
- 成功 `message = "上传成功"`。

#### 文档状态机

```
UPLOADING → PROCESSING → READY
     │           │
     └───────────┴──► FAILED（可通过 reindex 重试）
READY/PROCESSING ──DELETE──► DELETING → DELETED
```

- `FAILED` 时 `indexError` 有原因，`indexAttempts` 为尝试次数。
- `DELETING` 文档**仍会出现在列表里**（软删还没完成，状态为 `DELETING`）；只有向量清理成功、写入 `deletedAt` 并置 `DELETED` 后，普通列表查询才会把它过滤掉。前端看到 `DELETING` 应显示「删除中」并禁用操作。

#### `ready-ids`（Agent 检索白名单）

Query 参数（均可选）：

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `embeddingModel` | string | 当前生效的 Embedding 模型 |
| `embeddingProvider` | string | |
| `embeddingDimension` | number | 必须为正整数，否则 `400` |
| `embeddingFingerprint` | string | 传了 fingerprint 就必须同时传 model，否则 `400` |

返回 `ReadyDocument[]`：`{ id: number, indexVersion: number }`。这是给 Python Agent 用的接口，**前端通常不需要调用**。

#### 其他

- `GET /api/spaces/{spaceId}/documents` 按 `createdAt`、`id` 倒序。
- `POST /api/documents/{id}/reindex`：READY 文档在重建期间保持 READY（新旧版本切换原子完成）；`DELETING` 时 → `409`。成功 `message = "已提交重建"`。
- `DELETE /api/documents/{id}`：先切 `DELETING` 退出检索白名单，异步清理向量与文件；**重复删除幂等成功**。成功 `message = "已提交删除"`。
- `DocumentResponse` **不返回** `storageUri`、`deletedAt`。

**`DocumentResponse`**

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | number | |
| `learningSpaceId` | number | |
| `userId` | number | |
| `name` | string | 展示名（由文件名推导，可能去掉扩展名） |
| `fileName` | string | 原始文件名（已做路径清洗） |
| `fileType` | string | 由扩展名推导：`"md"` / `"txt"` |
| `size` | number | 字节数 |
| `contentHash` | string | SHA-256 |
| `status` | `DocumentStatus` | 见上 |
| `chunkCount` | number \| null | |
| `indexAttempts` | number \| null | |
| `indexError` | string \| null | 最近一次索引失败原因 |
| `embeddingModel` | string \| null | |
| `indexVersion` | number \| null | 活跃索引版本 |
| `indexedAt` | string \| null | |
| `createdAt` / `updatedAt` | string | |
| `embeddingProvider` | string \| null | |
| `embeddingDimension` | number \| null | |
| `embeddingFingerprint` | string \| null | |
| `indexStartedAt` | string \| null | |
| `deleteError` | string \| null | 删除清理失败原因 |

**`DocumentUploadResponse`**：`{ document: DocumentResponse, duplicate: boolean }`。

### 4.8 HighlightController

| 方法 | 路径 | 请求体 | 响应 `data` |
| --- | --- | --- | --- |
| `POST` | `/api/messages/{messageId}/highlights` | `HighlightRequest` | `HighlightResponse` |
| `GET` | `/api/highlights` | — | `HighlightResponse[]` |
| `GET` | `/api/messages/{messageId}/highlights` | — | `HighlightResponse[]` |

**`HighlightRequest`**

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `markType` | `HighlightType` | 是 | **字段名是 `markType`，不是 `type`** |
| `selectedText` | string | 是 | 选中文本，非空白 |
| `startOffset` | number \| null | 建议 | 选区内起始偏移 |
| `endOffset` | number \| null | 建议 | 选区内结束偏移 |
| `note` | string \| null | 否 | 用户补充说明 |

- `HighlightType`：`IMPORTANT` / `QUESTION` / `MY_UNDERSTANDING` / `MISCONCEPTION_CANDIDATE` / `CONCEPT_LINK`。
- 创建批注**可能产生学习证据**（取决于类型），例如 `MY_UNDERSTANDING` 会推进理解状态。
- `GET /api/highlights` 返回当前用户全部批注，按 `createdAt` 倒序；按消息查询时按 `startOffset`、`id` 升序。
- 成功 `message = "标记成功"`。

### 4.9 UnderstandingController + EvidenceController

#### 理解（只读）

| 方法 | 路径 | Query | 响应 `data` |
| --- | --- | --- | --- |
| `GET` | `/api/understandings` | `spaceId`（**必填**） | `UnderstandingResponse[]` |
| `GET` | `/api/understandings/{conceptId}` | — | `UnderstandingResponse` |
| `GET` | `/api/understandings/{conceptId}/revisions` | — | `UnderstandingRevisionResponse[]` |

- 没有写接口。理解只能通过新证据演进。
- `GET /api/understandings` 按 `updatedAt` 倒序；`revisions` 按 `revisionNo` 升序。
- `GET /api/understandings/{conceptId}`：知识点不存在 / 无权访问 → `404`；知识点存在但**尚未建立理解** → HTTP 200 且 `data: null`（不是 404）。

**`UnderstandingResponse`**

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | number | |
| `conceptId` | number | |
| `conceptName` | string \| null | |
| `summary` | string \| null | |
| `knownParts` | string[] | |
| `misconceptions` | `Misconception[]` | **仅当前仍成立**的误区（ACTIVE / PENDING） |
| `uncertainParts` | string[] | |
| `latestUserExplanation` | string \| null | |
| `status` | `UnderstandingStatus` | `UNKNOWN` / `EXPOSED` / `PARTIAL` / `DEMONSTRATED` / `MISCONCEPTION` |
| `currentRevisionId` | number \| null | |
| `updatedAt` | string | |

**`Misconception`**

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `text` | string | 误区内容 |
| `status` | `MisconceptionStatus` | `ACTIVE` / `PENDING` |
| `firstDetectedAt` | string | **本轮**首次发现时间 |
| `lastEvidenceAt` | string \| null | |
| `reversalEvidenceIds` | number[] | 已采信的反向正确证据 id |

**`UnderstandingRevisionResponse`**

| 字段 | 类型 |
| --- | --- |
| `id` | number |
| `revisionNo` | number |
| `summary` | string \| null |
| `knownParts` | string[] |
| `misconceptions` | `Misconception[]` |
| `uncertainParts` | string[] |
| `userExplanation` | string \| null |
| `status` | `UnderstandingStatus` |
| `triggerEvidenceId` | number \| null |
| `createdAt` | string |

#### 证据（只读）

| 方法 | 路径 | 响应 `data` |
| --- | --- | --- |
| `GET` | `/api/understandings/{conceptId}/evidence` | `EvidenceResponse[]` |
| `GET` | `/api/evidence/{evidenceId}` | `EvidenceResponse` |

- Evidence 是 **append-only**：没有 PUT / DELETE。
- 列表按 `createdAt`、`id` 倒序。
- `sources` 可跳回原始消息 / 批注 / 测试回答，按 `id` 升序。

**`EvidenceResponse`**

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | number | |
| `conceptId` | number | |
| `understandingId` | number | |
| `evidenceType` | `EvidenceType` | `USER_EXPLANATION` / `OBSERVATION` / `TEST_RESULT` / `USER_CORRECTION` / `HIGHLIGHT_SIGNAL` |
| `observation` | string | 用户原话或紧贴原话的短句 |
| `correctness` | `EvidenceCorrectness` | `CORRECT` / `INCORRECT` / `PARTIAL` / `UNKNOWN` |
| `reasoningQuality` | number \| null | 内部分析信号，第一版不展示 |
| `specificity` | number \| null | 同上 |
| `aiConfidence` | number \| null | 同上 |
| `needFollowUp` | boolean \| null | |
| `followUpStatus` | `EvidenceFollowUpStatus` | `NONE` / `PENDING` / `RESOLVED` |
| `createdAt` | string | |
| `sources` | `EvidenceSourceResponse[]` | |

**`EvidenceSourceResponse`**：`{ sourceType: 'MESSAGE' | 'HIGHLIGHT' | 'TEST_ANSWER', sourceId: number }`。

### 4.10 LearningStateController — `/api/learning-state`

| 方法 | 路径 | Query | 响应 `data` |
| --- | --- | --- | --- |
| `GET` | `/api/learning-state/overview` | — | `LearningStateResponse[]` |
| `GET` | `/api/learning-state/concepts/{conceptId}` | — | `LearningStateResponse` |
| `GET` | `/api/learning-state/attention` | `limit`（默认 10） | `AttentionItem[]` |
| `GET` | `/api/learning-state/topic-coverage` | `spaceId`（**必填**） | `TopicCoverageItem[]` |

**只有查询接口，没有修改入口。** 状态由 Understanding 物化派生，想让状态变化只能产生新证据。

- `overview`：按 `reviewPriority` 倒序、`updatedAt` 倒序。
- `attention`：推荐关注项，排序优先级为「误解 > 久未验证 > 到期待复习」；按 `reviewPriority` 倒序、`nextReviewAt` 升序。
- `topic-coverage`：按 `conceptId` 升序；`spaceId` 缺失 → `400`。
- 状态是 5 个离散值，**没有掌握度分数**；想看依据去查 Understanding 的 revisions。

**`LearningStateResponse`**

| 字段 | 类型 |
| --- | --- |
| `userId` | number |
| `conceptId` | number |
| `conceptName` | string \| null |
| `status` | `UnderstandingStatus` |
| `lastEvidenceAt` | string \| null |
| `nextReviewAt` | string \| null |
| `reviewPriority` | number \| null |
| `updatedAt` | string |

**`AttentionItem`**

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `conceptId` | number | |
| `conceptName` | string \| null | |
| `status` | `UnderstandingStatus` | |
| `reason` | `AttentionReason` | `MISCONCEPTION` / `MISCONCEPTION_RESOLVING` / `LONG_UNVERIFIED` / `NEEDS_REVIEW` |
| `lastEvidenceAt` | string \| null | |
| `nextReviewAt` | string \| null | |
| `reviewPriority` | number \| null | |

**`TopicCoverageItem`**

| 字段 | 类型 |
| --- | --- |
| `conceptId` | number |
| `conceptName` | string \| null |
| `status` | `UnderstandingStatus` |
| `evidenceCount` | number |
| `revisionCount` | number |
| `lastEvidenceAt` | string \| null |

### 4.11 TestController — `/api/tests`

| 方法 | 路径 | 请求体 | 响应 `data` |
| --- | --- | --- | --- |
| `POST` | `/api/tests` | `TestTaskRequest` | `TestTaskResponse` |
| `GET` | `/api/tests/pending` | — | `TestTaskResponse[]` |
| `GET` | `/api/tests/{testId}` | — | `TestTaskResponse` |
| `POST` | `/api/tests/{testId}/answer` | `AnswerRequest` | `AnswerResult` |

**`TestTaskRequest`**

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `conceptId` | number | 是 | |
| `conversationId` | number \| null | 否 | 测试嵌入的对话 |
| `questionType` | `QuestionType \| null` | 否 | 缺省 `SHORT_ANSWER` |
| `questionText` | string \| null | 否 | 不传则由本地规则生成 |
| `correctAnswer` | string \| null | 否 | |
| `evaluationRubric` | string \| null | 否 | **JSON 字符串**，不是对象 |

- 出题成功 `message = "出题成功"`。
- `pending` 只返回未过期且未作答的任务，按 `createdAt` 倒序。
- 提交答案后判分结果会成为一条 Evidence；重复提交 → `409`；过期 → `409`。

**`TestTaskResponse`**

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | number | |
| `conceptId` | number | |
| `conceptName` | string \| null | |
| `conversationId` | number \| null | |
| `questionType` | `QuestionType` | |
| `questionText` | string \| null | |
| `options` | string[] | 选择题选项 |
| `hint` | string \| null | |
| `status` | `TestTaskStatus` | `PENDING` / `ANSWERED` / `EXPIRED` |
| `expiredAt` | string \| null | |
| `createdAt` | string | |

> **不下发 `correctAnswer`**，答案只在服务端参与判分。

**`AnswerRequest`**：`userAnswer: string`（必填）。

**`AnswerResult`**

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `isCorrect` | boolean \| null | |
| `feedback` | string \| null | |
| `evidenceId` | number \| null | 本次作答产生的证据 |
| `revisionId` | number \| null | 理解最新 revision |
| `status` | `UnderstandingStatus \| null` | 作答后的最新理解状态 |

### 4.12 MemoryController — `/api/memories`

| 方法 | 路径 | 请求 / Query | 响应 `data` |
| --- | --- | --- | --- |
| `GET` | `/api/memories` | `spaceId?`, `conceptId?`, `status?` | `MemoryResponse[]` |
| `GET` | `/api/memories/search` | `spaceId?`, `conceptId?`, `keyword?` | `MemoryResponse[]` |
| `GET` | `/api/memories/search/hybrid` | `spaceId?`, `conceptId?`, `keyword?`, `limit?` | `HybridMemorySearchResponse` |
| `GET` | `/api/memories/{id}` | — | `MemoryResponse` |
| `POST` | `/api/memories` | `MemoryRequest` | `MemoryResponse` |
| `PATCH` | `/api/memories/{id}` | `MemoryRequest` | `MemoryResponse` |
| `DELETE` | `/api/memories/{id}` | — | `null` |

#### 两个检索接口的区别

- **`/search`（精确过滤 + 关键词）**：沿用旧返回结构 `MemoryResponse[]`，供 Python Agent 继续使用。
  - 可见性规则：`GLOBAL` 记忆对任意 `spaceId` 都可见；不给 `conceptId` 时只返回 `GLOBAL` 与当前 `SPACE` 的记忆。
- **`/search/hybrid`（推荐前端使用）**：MySQL FULLTEXT + Agent 向量召回，RRF 融合后回 MySQL 复核。
  - `limit` 缺省 **10**，上限 **200**（超出自动截断）。
  - `keyword` 为空 / 全空白 → `status = "BLANK_QUERY"`，`items = []`。
  - **必须结合 `status` 使用**：`UNAVAILABLE` 表示检索不可用，不是「没有记忆」。

| `status` | 含义 |
| --- | --- |
| `OK` | 两路都成功 |
| `DEGRADED_VECTOR` | 词法成功、向量路失败 |
| `DEGRADED_KEYWORD` | 向量成功、词法路失败 |
| `UNAVAILABLE` | 两路都失败或回源复核失败，`items` 为空 |
| `BLANK_QUERY` | 查询为空，`items` 为空 |

- `GET /api/memories` 按 `importance` 倒序、`updatedAt` 倒序。
- `POST` 创建的用户记忆固定为 `ACTIVE`；`status` 字段在创建时被忽略。
- `PATCH` 可推进生命周期（`CANDIDATE → ACTIVE → ARCHIVED`），但 **`memoryType` 与 `content` 是必填的**（DTO 上的 `@NotNull` / `@NotBlank` 仍会校验），即使只想改 `status` 也要把它们原样带回（见第 7 节）。
- `DELETE` 语义是**归档**，不是物理删除；不影响 Evidence 与 Understanding。成功 `message = "已归档"`。

**`MemoryRequest`**

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `learningSpaceId` | number \| null | 否 | 作用域推导：给了 `conceptId` → `CONCEPT`；只给 `spaceId` → `SPACE`；都不给 → `GLOBAL` |
| `conceptId` | number \| null | 否 | |
| `memoryType` | `MemoryType` | **是** | `USER` / `LEARNING` / `PREFERENCE` |
| `memoryKey` | string \| null | 否 | 同作用域内标识；缺省由内容哈希兜底 |
| `content` | string | **是** | 非空白 |
| `importance` | number \| null | 否 | **PATCH 不传会清空** |
| `status` | `MemoryStatus \| null` | 仅 PATCH | 创建时忽略，固定 `ACTIVE` |

**`MemoryResponse`**

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | number | |
| `scopeType` | `MemoryScopeType` | `GLOBAL` / `SPACE` / `CONCEPT` |
| `learningSpaceId` | number \| null | GLOBAL 时为 null |
| `conceptId` | number \| null | 非 CONCEPT 时为 null |
| `memoryType` | `MemoryType` | |
| `memoryKey` | string \| null | |
| `content` | string | |
| `status` | `MemoryStatus` | |
| `importance` | number \| null | |
| `createdAt` / `updatedAt` | string | |

**`HybridMemorySearchResponse`**

| 字段 | 类型 |
| --- | --- |
| `status` | `MemoryHybridStatus` |
| `items` | `HybridMemoryHit[]` |
| `warnings` | string[] |
| `elapsedMs` | number |

**`HybridMemoryHit`**

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `memory` | `MemoryResponse` | MySQL 回源后的权威内容 |
| `rrfScore` | number | RRF 融合分 |
| `lexicalRank` | number \| null | 词法排名（1-based），未命中为 null |
| `vectorRank` | number \| null | 向量排名（1-based），未命中为 null |
| `vectorScore` | number \| null | 余弦相似度，未命中为 null |

### 4.13 SessionController — `/api/sessions`

| 方法 | 路径 | Query / 请求体 | 响应 `data` |
| --- | --- | --- | --- |
| `GET` | `/api/sessions/current` | `spaceId?` | `SessionResponse \| null` |
| `GET` | `/api/sessions` | `spaceId?`, `status?` | `SessionResponse[]` |
| `GET` | `/api/sessions/{id}` | — | `SessionResponse` |
| `POST` | `/api/sessions` | `SessionRequest` | `SessionResponse` |
| `PATCH` | `/api/sessions/{id}` | `SessionUpdateRequest` | `SessionResponse` |
| `POST` | `/api/sessions/{id}/complete` | — | `SessionResponse` |

**`SessionRequest`**

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `learningSpaceId` | number | 是 | |
| `goal` | string | 是 | 学习目标，非空白 |
| `title` | string \| null | 否 | 不传则由 goal 生成标题 |

**`SessionUpdateRequest`**（全部可空，只改给了值的字段）

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `status` | `SessionStatus \| null` | `ACTIVE` / `PAUSED` / `COMPLETED` / `ABANDONED` |
| `title` | string \| null | |
| `goal` | string \| null | |
| `currentFocus` | string \| null | **传 `""` 表示清空焦点，传 `null` 表示不改** |

**`SessionResponse`**

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | number | |
| `learningSpaceId` | number | |
| `title` | string \| null | |
| `goal` | string \| null | 长期目标 |
| `currentFocus` | string \| null | 当前卡点 |
| `status` | `SessionStatus` | |
| `startedAt` | string \| null | |
| `lastActiveAt` | string \| null | |
| `completedAt` | string \| null | |

- **`/current` 在没有进行中的 Session 时返回 `data: null`，不是 404。** 新用户第一次打开就是这种状态，前端按「还没开始学」处理即可。
- 列表按 `lastActiveAt` 倒序。
- Session 绝大多数是**懒创建**的：新对话的 `sessionId` 为 null，用户发出第一条有效学习消息后由系统决定归属。`POST /api/sessions` 只用于用户明确「另起一段」。
- `POST /{id}/complete` 成功 `message = "已完成"`；结束 Session 不会删除 Evidence / Understanding / Memory。
- `PATCH` 是唯一能把状态改为 `PAUSED` / `ABANDONED` 的入口。

### 4.14 未接入的骨架接口

以下接口存在但**一律抛 `501`**，前端不要接：

| 方法 | 路径 | 错误消息 |
| --- | --- | --- |
| `POST` | `/api/export` | 导出能力尚未接入 |
| `POST` | `/api/imports` | 导入能力尚未接入 |
| `GET` | `/api/imports/{id}` | 导入能力尚未接入 |
| `GET` | `/api/learning-plans` | 学习计划能力尚未接入 |
| `POST` | `/api/learning-plans` | 学习计划能力尚未接入 |
| `PATCH` | `/api/learning-plans/{id}` | 学习计划能力尚未接入 |

---

## 5. TypeScript 类型定义

可直接保存为 `web/src/types/api.ts`。

```ts
/* ============================================================
 * 统一响应
 * ============================================================ */

export interface ApiResponse<T> {
  /** 0 = 成功；非 0 时与 HTTP 状态码一致 */
  code: number
  message: string
  data: T | null
}

/* ============================================================
 * 枚举（后端序列化为大写常量名）
 * ============================================================ */

export type SpaceVisibility = 'PRIVATE' | 'SHARED'
export type ConversationStatus = 'ACTIVE' | 'ARCHIVED'
export type SessionStatus = 'ACTIVE' | 'PAUSED' | 'COMPLETED' | 'ABANDONED'
export type DocumentStatus =
  | 'UPLOADING'
  | 'PROCESSING'
  | 'READY'
  | 'FAILED'
  | 'DELETING'
  | 'DELETED'
export type MessageRole = 'USER' | 'ASSISTANT' | 'SYSTEM' | 'TOOL'
export type MessageType = 'TEXT' | 'TEST' | 'EVENT'
export type HighlightType =
  | 'IMPORTANT'
  | 'QUESTION'
  | 'MY_UNDERSTANDING'
  | 'MISCONCEPTION_CANDIDATE'
  | 'CONCEPT_LINK'
export type EvidenceType =
  | 'USER_EXPLANATION'
  | 'OBSERVATION'
  | 'TEST_RESULT'
  | 'USER_CORRECTION'
  | 'HIGHLIGHT_SIGNAL'
export type EvidenceCorrectness = 'CORRECT' | 'INCORRECT' | 'PARTIAL' | 'UNKNOWN'
export type EvidenceFollowUpStatus = 'NONE' | 'PENDING' | 'RESOLVED'
export type EvidenceSourceType = 'MESSAGE' | 'HIGHLIGHT' | 'TEST_ANSWER'
export type UnderstandingStatus =
  | 'UNKNOWN'
  | 'EXPOSED'
  | 'PARTIAL'
  | 'DEMONSTRATED'
  | 'MISCONCEPTION'
export type MisconceptionStatus = 'ACTIVE' | 'PENDING'
export type AttentionReason =
  | 'MISCONCEPTION'
  | 'MISCONCEPTION_RESOLVING'
  | 'LONG_UNVERIFIED'
  | 'NEEDS_REVIEW'
export type QuestionType = 'SINGLE_CHOICE' | 'TRUE_FALSE' | 'SHORT_ANSWER'
export type TestTaskStatus = 'PENDING' | 'ANSWERED' | 'EXPIRED'
export type MemoryScopeType = 'GLOBAL' | 'SPACE' | 'CONCEPT'
export type MemoryStatus = 'CANDIDATE' | 'ACTIVE' | 'ARCHIVED'
export type MemoryType = 'USER' | 'LEARNING' | 'PREFERENCE'
export type MemoryHybridStatus =
  | 'OK'
  | 'DEGRADED_VECTOR'
  | 'DEGRADED_KEYWORD'
  | 'UNAVAILABLE'
  | 'BLANK_QUERY'

/* ============================================================
 * 通用
 * ============================================================ */

/** 后端 LocalDateTime：ISO-8601 本地时间，无时区，如 "2026-11-07T22:36:00" */
export type LocalDateTime = string

/* ============================================================
 * Auth / User
 * ============================================================ */

export interface RegisterRequest {
  username: string
  password: string
  email?: string | null
}

export interface LoginRequest {
  username: string
  password: string
}

export interface UserDTO {
  id: number
  username: string
  email: string | null
  createdAt: LocalDateTime
}

export interface AuthResponse {
  token: string
  /** 固定 "Bearer" */
  tokenType: string
  user: UserDTO
}

export interface UpdateProfileRequest {
  email?: string | null
}

export interface ChangePasswordRequest {
  oldPassword: string
  newPassword: string
}

/* ============================================================
 * Learning Space / Concept
 * ============================================================ */

export interface LearningSpaceRequest {
  name: string
  description?: string | null
  visibility?: SpaceVisibility | null
}

export interface LearningSpaceResponse {
  id: number
  userId: number
  name: string
  description: string | null
  visibility: SpaceVisibility
  createdAt: LocalDateTime
  updatedAt: LocalDateTime
}

export interface ConceptRequest {
  canonicalName: string
  displayName?: string | null
  description?: string | null
  /** 1~5 */
  difficultyLevel?: number | null
}

export interface ConceptResponse {
  id: number
  learningSpaceId: number
  canonicalName: string
  displayName: string | null
  description: string | null
  difficultyLevel: number | null
  createdAt: LocalDateTime
  updatedAt: LocalDateTime
}

/* ============================================================
 * Conversation / Message
 * ============================================================ */

export interface ConversationRequest {
  title?: string | null
}

export interface ConversationResponse {
  id: number
  learningSpaceId: number
  /** 为 null 是正常状态：尚未归属任何学习活动 */
  sessionId: number | null
  title: string | null
  status: ConversationStatus
  createdAt: LocalDateTime
  updatedAt: LocalDateTime
}

export interface CitationResponse {
  documentId: number
  chunkId: number
  chunkIndex: number | null
  sourceTitle: string
  /** 当前是否仍可打开（文档删除 / 重建后会变成 false） */
  available: boolean
}

export interface MessageRequest {
  content: string
}

export interface MessageResponse {
  id: number
  conversationId: number
  role: MessageRole
  messageType: MessageType
  content: string
  tokens: number | null
  sequenceNo: number | null
  createdAt: LocalDateTime
  truncated: boolean
  citations: CitationResponse[]
}

/* ============================================================
 * SSE（POST /api/conversations/{id}/messages）
 * ============================================================ */

export interface SseMessageStart {
  runId: number
  conversationId: number
}

export interface SseMessageDelta {
  delta: string
}

export interface SseMessageEnd {
  messageId: number
  runId: number
  citations: CitationResponse[]
  /** 仅在回答因 Token 上限截断时出现 */
  truncated?: true
  finishReason?: string | null
}

export interface SseMessageError {
  runId: number
  code: number
  message: string
}

/* ============================================================
 * Document
 * ============================================================ */

export interface DocumentResponse {
  id: number
  learningSpaceId: number
  userId: number
  name: string
  fileName: string
  /** "md" | "txt" */
  fileType: string
  size: number
  contentHash: string
  status: DocumentStatus
  chunkCount: number | null
  indexAttempts: number | null
  indexError: string | null
  embeddingModel: string | null
  indexVersion: number | null
  indexedAt: LocalDateTime | null
  createdAt: LocalDateTime
  updatedAt: LocalDateTime
  embeddingProvider: string | null
  embeddingDimension: number | null
  embeddingFingerprint: string | null
  indexStartedAt: LocalDateTime | null
  deleteError: string | null
}

export interface DocumentUploadResponse {
  document: DocumentResponse
  /** true 表示命中同空间内内容相同的已有文档，未新建 */
  duplicate: boolean
}

export interface ReadyDocument {
  id: number
  indexVersion: number
}

/* ============================================================
 * Highlight
 * ============================================================ */

export interface HighlightRequest {
  markType: HighlightType
  selectedText: string
  startOffset?: number | null
  endOffset?: number | null
  note?: string | null
}

export interface HighlightResponse {
  id: number
  messageId: number
  markType: HighlightType
  selectedText: string
  startOffset: number | null
  endOffset: number | null
  note: string | null
  createdAt: LocalDateTime
}

/* ============================================================
 * Understanding / Evidence
 * ============================================================ */

export interface Misconception {
  text: string
  status: MisconceptionStatus
  firstDetectedAt: LocalDateTime
  lastEvidenceAt: LocalDateTime | null
  reversalEvidenceIds: number[]
}

export interface UnderstandingResponse {
  id: number
  conceptId: number
  conceptName: string | null
  summary: string | null
  knownParts: string[]
  /** 仅当前仍成立的误区 */
  misconceptions: Misconception[]
  uncertainParts: string[]
  latestUserExplanation: string | null
  status: UnderstandingStatus
  currentRevisionId: number | null
  updatedAt: LocalDateTime
}

export interface UnderstandingRevisionResponse {
  id: number
  revisionNo: number
  summary: string | null
  knownParts: string[]
  misconceptions: Misconception[]
  uncertainParts: string[]
  userExplanation: string | null
  status: UnderstandingStatus
  triggerEvidenceId: number | null
  createdAt: LocalDateTime
}

export interface EvidenceSourceResponse {
  sourceType: EvidenceSourceType
  sourceId: number
}

export interface EvidenceResponse {
  id: number
  conceptId: number
  understandingId: number
  evidenceType: EvidenceType
  observation: string
  correctness: EvidenceCorrectness
  reasoningQuality: number | null
  specificity: number | null
  aiConfidence: number | null
  needFollowUp: boolean | null
  followUpStatus: EvidenceFollowUpStatus
  createdAt: LocalDateTime
  sources: EvidenceSourceResponse[]
}

/* ============================================================
 * Learning State
 * ============================================================ */

export interface LearningStateResponse {
  userId: number
  conceptId: number
  conceptName: string | null
  status: UnderstandingStatus
  lastEvidenceAt: LocalDateTime | null
  nextReviewAt: LocalDateTime | null
  reviewPriority: number | null
  updatedAt: LocalDateTime
}

export interface AttentionItem {
  conceptId: number
  conceptName: string | null
  status: UnderstandingStatus
  reason: AttentionReason
  lastEvidenceAt: LocalDateTime | null
  nextReviewAt: LocalDateTime | null
  reviewPriority: number | null
}

export interface TopicCoverageItem {
  conceptId: number
  conceptName: string | null
  status: UnderstandingStatus
  evidenceCount: number
  revisionCount: number
  lastEvidenceAt: LocalDateTime | null
}

/* ============================================================
 * Test
 * ============================================================ */

export interface TestTaskRequest {
  conceptId: number
  conversationId?: number | null
  questionType?: QuestionType | null
  questionText?: string | null
  correctAnswer?: string | null
  /** JSON 字符串，不是对象 */
  evaluationRubric?: string | null
}

export interface TestTaskResponse {
  id: number
  conceptId: number
  conceptName: string | null
  conversationId: number | null
  questionType: QuestionType
  questionText: string | null
  options: string[]
  hint: string | null
  status: TestTaskStatus
  expiredAt: LocalDateTime | null
  createdAt: LocalDateTime
}

export interface AnswerRequest {
  userAnswer: string
}

export interface AnswerResult {
  isCorrect: boolean | null
  feedback: string | null
  evidenceId: number | null
  revisionId: number | null
  status: UnderstandingStatus | null
}

/* ============================================================
 * Memory
 * ============================================================ */

export interface MemoryRequest {
  learningSpaceId?: number | null
  conceptId?: number | null
  memoryType: MemoryType
  memoryKey?: string | null
  content: string
  importance?: number | null
  /** 仅 PATCH 生效；创建时忽略 */
  status?: MemoryStatus | null
}

export interface MemoryResponse {
  id: number
  scopeType: MemoryScopeType
  learningSpaceId: number | null
  conceptId: number | null
  memoryType: MemoryType
  memoryKey: string | null
  content: string
  status: MemoryStatus
  importance: number | null
  createdAt: LocalDateTime
  updatedAt: LocalDateTime
}

export interface HybridMemoryHit {
  memory: MemoryResponse
  rrfScore: number
  lexicalRank: number | null
  vectorRank: number | null
  vectorScore: number | null
}

export interface HybridMemorySearchResponse {
  status: MemoryHybridStatus
  items: HybridMemoryHit[]
  warnings: string[]
  elapsedMs: number
}

/* ============================================================
 * Learning Session
 * ============================================================ */

export interface SessionRequest {
  learningSpaceId: number
  goal: string
  title?: string | null
}

export interface SessionUpdateRequest {
  status?: SessionStatus | null
  title?: string | null
  goal?: string | null
  /** 传 "" 清空焦点；传 null / 省略表示不改 */
  currentFocus?: string | null
}

export interface SessionResponse {
  id: number
  learningSpaceId: number
  title: string | null
  goal: string | null
  currentFocus: string | null
  status: SessionStatus
  startedAt: LocalDateTime | null
  lastActiveAt: LocalDateTime | null
  completedAt: LocalDateTime | null
}
```

### 5.1 建议的请求层封装

```ts
// 伪代码：axios / fetch 均可
interface RequestOptions {
  method?: 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE'
  body?: unknown
  query?: Record<string, string | number | boolean | null | undefined>
  signal?: AbortSignal
}

async function request<T>(path: string, opts: RequestOptions = {}): Promise<T> {
  const url = new URL(path, import.meta.env.VITE_API_BASE_URL)
  for (const [k, v] of Object.entries(opts.query ?? {})) {
    if (v !== undefined && v !== null) url.searchParams.set(k, String(v))
  }

  const res = await fetch(url, {
    method: opts.method ?? 'GET',
    headers: {
      ...(opts.body !== undefined ? { 'Content-Type': 'application/json' } : {}),
      Authorization: `Bearer ${tokenStore.get()}`,
    },
    body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
    signal: opts.signal,
  })

  const json = (await res.json()) as ApiResponse<T>
  if (json.code !== 0) throw new ApiError(json.code, json.message)
  return json.data as T
}
```

> `data` 为 `null` 的成功响应（如 `DELETE`、`SessionResponse | null`）需要允许返回 `null`，因此上面断言为 `T`，调用方对可空接口自行声明 `T | null`。

---

## 6. DTO 字段清单

### 6.1 前端使用的 DTO（19 个）

| DTO | 字段（Java 类型） |
| --- | --- |
| `RegisterRequest` | `username: String`, `password: String`, `email: String` |
| `LoginRequest` | `username: String`, `password: String` |
| `AuthResponse` | `token: String`, `tokenType: String`, `user: UserDTO` |
| `UserDTO` | `id: Long`, `username: String`, `email: String`, `createdAt: LocalDateTime` |
| `UpdateProfileRequest` | `email: String` |
| `ChangePasswordRequest` | `oldPassword: String`, `newPassword: String` |
| `LearningSpaceRequest` | `name: String`, `description: String`, `visibility: SpaceVisibility` |
| `LearningSpaceResponse` | `id: Long`, `userId: Long`, `name: String`, `description: String`, `visibility: SpaceVisibility`, `createdAt`, `updatedAt` |
| `ConceptRequest` | `canonicalName: String`, `displayName: String`, `description: String`, `difficultyLevel: Integer` |
| `ConceptResponse` | `id: Long`, `learningSpaceId: Long`, `canonicalName: String`, `displayName: String`, `description: String`, `difficultyLevel: Integer`, `createdAt`, `updatedAt` |
| `ConversationRequest` | `title: String` |
| `ConversationResponse` | `id: Long`, `learningSpaceId: Long`, `sessionId: Long`, `title: String`, `status: ConversationStatus`, `createdAt`, `updatedAt` |
| `MessageRequest` | `content: String` |
| `MessageResponse` | `id: Long`, `conversationId: Long`, `role: MessageRole`, `messageType: MessageType`, `content: String`, `tokens: Integer`, `sequenceNo: Integer`, `createdAt: LocalDateTime`, `truncated: boolean`, `citations: List<CitationResponse>` |
| `CitationResponse` | `documentId: Long`, `chunkId: Long`, `chunkIndex: Integer`, `sourceTitle: String`, `available: boolean` |
| `DocumentResponse` | 见 4.7（22 个字段） |
| `DocumentUploadResponse` | `document: DocumentResponse`, `duplicate: boolean` |
| `ReadyDocument` | `id: Long`, `indexVersion: Integer` |
| `HighlightRequest` | `markType: HighlightType`, `selectedText: String`, `startOffset: Integer`, `endOffset: Integer`, `note: String` |
| `HighlightResponse` | `id: Long`, `messageId: Long`, `markType: HighlightType`, `selectedText: String`, `startOffset: Integer`, `endOffset: Integer`, `note: String`, `createdAt: LocalDateTime` |
| `EvidenceSourceResponse` | `sourceType: EvidenceSourceType`, `sourceId: Long` |
| `EvidenceResponse` | `id: Long`, `conceptId: Long`, `understandingId: Long`, `evidenceType: EvidenceType`, `observation: String`, `correctness: EvidenceCorrectness`, `reasoningQuality: Float`, `specificity: Float`, `aiConfidence: Float`, `needFollowUp: Boolean`, `followUpStatus: EvidenceFollowUpStatus`, `createdAt: LocalDateTime`, `sources: List<EvidenceSourceResponse>` |
| `Misconception`（model） | `text: String`, `status: MisconceptionStatus`, `firstDetectedAt: LocalDateTime`, `lastEvidenceAt: LocalDateTime`, `reversalEvidenceIds: List<Long>` |
| `UnderstandingResponse` | `id: Long`, `conceptId: Long`, `conceptName: String`, `summary: String`, `knownParts: List<String>`, `misconceptions: List<Misconception>`, `uncertainParts: List<String>`, `latestUserExplanation: String`, `status: UnderstandingStatus`, `currentRevisionId: Long`, `updatedAt: LocalDateTime` |
| `UnderstandingRevisionResponse` | `id: Long`, `revisionNo: Integer`, `summary: String`, `knownParts: List<String>`, `misconceptions: List<Misconception>`, `uncertainParts: List<String>`, `userExplanation: String`, `status: UnderstandingStatus`, `triggerEvidenceId: Long`, `createdAt: LocalDateTime` |
| `LearningStateResponse` | `userId: Long`, `conceptId: Long`, `conceptName: String`, `status: UnderstandingStatus`, `lastEvidenceAt`, `nextReviewAt`, `reviewPriority: Integer`, `updatedAt` |
| `AttentionItem` | `conceptId: Long`, `conceptName: String`, `status: UnderstandingStatus`, `reason: AttentionReason`, `lastEvidenceAt`, `nextReviewAt`, `reviewPriority: Integer` |
| `TopicCoverageItem` | `conceptId: Long`, `conceptName: String`, `status: UnderstandingStatus`, `evidenceCount: Integer`, `revisionCount: Integer`, `lastEvidenceAt: LocalDateTime` |
| `TestTaskRequest` | `conceptId: Long`, `conversationId: Long`, `questionType: QuestionType`, `questionText: String`, `correctAnswer: String`, `evaluationRubric: String` |
| `TestTaskResponse` | `id: Long`, `conceptId: Long`, `conceptName: String`, `conversationId: Long`, `questionType: QuestionType`, `questionText: String`, `options: List<String>`, `hint: String`, `status: TestTaskStatus`, `expiredAt: LocalDateTime`, `createdAt: LocalDateTime` |
| `AnswerRequest` | `userAnswer: String` |
| `AnswerResult` | `isCorrect: Boolean`, `feedback: String`, `evidenceId: Long`, `revisionId: Long`, `status: UnderstandingStatus` |
| `MemoryRequest` | `learningSpaceId: Long`, `conceptId: Long`, `memoryType: MemoryType`, `memoryKey: String`, `content: String`, `importance: Float`, `status: MemoryStatus` |
| `MemoryResponse` | `id: Long`, `scopeType: MemoryScopeType`, `learningSpaceId: Long`, `conceptId: Long`, `memoryType: MemoryType`, `memoryKey: String`, `content: String`, `status: MemoryStatus`, `importance: Float`, `createdAt: LocalDateTime`, `updatedAt: LocalDateTime` |
| `HybridMemoryHit` | `memory: MemoryResponse`, `rrfScore: double`, `lexicalRank: Integer`, `vectorRank: Integer`, `vectorScore: Double` |
| `HybridMemorySearchResponse` | `status: MemoryHybridStatus`, `items: List<HybridMemoryHit>`, `warnings: List<String>`, `elapsedMs: long` |
| `SessionRequest` | `learningSpaceId: Long`, `goal: String`, `title: String` |
| `SessionUpdateRequest` | `status: SessionStatus`, `title: String`, `goal: String`, `currentFocus: String` |
| `SessionResponse` | `id: Long`, `learningSpaceId: Long`, `title: String`, `goal: String`, `currentFocus: String`, `status: SessionStatus`, `startedAt`, `lastActiveAt`, `completedAt` |

> 未标注日期类型的字段均为 `LocalDateTime`。上表省略了每个 DTO 的 `createdAt/updatedAt` 完整类型，统一为 `LocalDateTime`。

### 6.2 后端 ↔ Agent 内部 DTO（前端**不接触**）

以下 DTO 只用于 Spring 与 Python Agent 之间的内部调用，或内部消息队列，**不会出现在前端接口的请求 / 响应中**。列出仅为完整性，生成前端类型时应忽略。

| DTO | 字段 |
| --- | --- |
| `agent/AgentCitation` | `documentId: Long`, `chunkId: Long`, `chunkIndex: Integer`, `sourceTitle: String` |
| `agent/ChatHistoryMessage` | `role: MessageRole`, `content: String` |
| `agent/ChatAgentRequest` | `runId: Long`, `traceId: String`, `userId: Long`, `conversationId: Long`, `learningSpaceId: Long`, `userMessage: String`, `history: List<ChatHistoryMessage>`, `session: SessionContext`, `authToken: String` |
| `agent/ChatAgentRequest.SessionContext` | `id: Long`, `title: String`, `goal: String`, `currentFocus: String` |
| `agent/AnalyzeAgentRequest` | `runId`, `traceId`, `userId`, `conversationId`, `learningSpaceId`, `messageId`, `userMessage`, `assistantMessage`, `history`, `session`, `authToken` |
| `agent/AnalyzeAgentResult` | `evidenceCandidates: List<EvidenceCandidate>`, `understandingProposal: UnderstandingProposal`, `memoryCandidates: List<MemoryCandidate>`, `verificationDecision: VerificationDecision`, `analyzerVersion: String`, `detectedConcepts: List<String>` |
| `agent/AnalyzeAgentResult.EvidenceCandidate` | `conceptId`, `evidenceType`, `observation`, `correctness`, `reasoningQuality`, `specificity`, `aiConfidence`, `needFollowUp`, `userExplanation` |
| `agent/AnalyzeAgentResult.UnderstandingProposal` | `conceptId`, `knownParts`, `uncertainParts`, `misconceptions`, `latestUserExplanation` |
| `agent/AnalyzeAgentResult.MemoryCandidate` | `scopeType`, `learningSpaceId`, `conceptId`, `memoryType`, `memoryKey`, `content`, `importance`, `action` |
| `agent/AnalyzeAgentResult.VerificationDecision` | `action`, `trigger`, `conceptId`, `reason`, `questionType`, `questionText`, `correctAnswer` |
| `document/agent/AgentIndexRequest` | `documentId`, `userId`, `spaceId`, `documentName`, `fileName`, `fileType`, `indexVersion`, `chunks` |
| `document/agent/AgentIndexChunk` | `chunkId`, `chunkIndex`, `content`, `metadata: Map<String,Object>` |
| `document/agent/AgentIndexResponse` | `collection`, `embeddingModel`, `embeddingProvider`, `embeddingDimension`, `embeddingFingerprint`, `vectors` |
| `document/agent/AgentVectorRef` | `chunkId`, `pointId` |
| `document/agent/AgentParseResponse` | `parserVersion`, `chunks` |
| `document/agent/AgentParsedChunk` | `chunkIndex`, `content`, `tokenCount`, `tokenCountEstimated`, `metadata` |
| `document/agent/AgentDeleteRequest` | `documentId`, `collection`, `embeddingModel`, `embeddingProvider`, `embeddingDimension`, `embeddingFingerprint`, `keepIndexVersion`, `indexVersions`, `deleteLegacy` |
| `memory/agent/MemoryEventPayload` | `memoryId`, `userId`, `content`, `contentHash`, `memoryVersion`, `scopeType`, `scopeKey`, `learningSpaceId`, `conceptId`, `memoryType`, `status`, `importance`, `updatedAt` |
| `memory/agent/MemorySyncRequest` | `eventId`, `operation`, `memoryId`, `userId`, `content`, `contentHash`, `memoryVersion`, `scopeType`, `learningSpaceId`, `conceptId`, `memoryType`, `importance`, `status` |
| `memory/agent/MemorySyncResponse` | `pointId`, `collection`, `skipped` |
| `memory/agent/MemoryVectorSearchRequest` | `requestId`, `userId`, `spaceId`, `conceptId`, `query`, `recallK` |
| `memory/agent/MemoryVectorHit` | `memoryId`, `score`, `contentHash` |
| `conversation/CitationMetadata` | `documentId`, `chunkId`, `chunkIndex`, `sourceTitle`（持久化快照，前端看到的是 `CitationResponse`） |

---

## 7. 接入注意事项与已知差异

### 7.1 容易踩的坑

1. **SSE 不能用 `EventSource`。** 接口是 POST + 自定义 `Authorization` 头，必须用 `fetch` 流式读取或 `@microsoft/fetch-event-source`。详见第 3.5 节。
2. **SSE 的建流前错误是 JSON，不是事件。** 先判断 `Content-Type`：`text/event-stream` 才按 SSE 解析，否则按 `ApiResponse` 处理 400/404。
3. **`PATCH /api/memories/{id}` 的 DTO 校验是「全量必填」。** `memoryType` 是 `@NotNull`、`content` 是 `@NotBlank`，即使只想推进 `status` 也必须带上；`importance` 不传会被置空。建议前端编辑记忆时发送完整表单。
4. **`PUT` 会清空未传的可空字段。** 例如 `PUT /api/spaces/{id}` 不传 `description` 会清空描述；`PUT /api/concepts/{id}` 不传 `difficultyLevel` 会清空难度。编辑表单应回填原值再提交。
5. **`GET /api/sessions/current` 可能返回 `data: null`。** 这不是错误，新用户 / 尚无学习活动时就是这种状态。
6. **`GET /api/memories/search/hybrid` 必须看 `status`。** `UNAVAILABLE` 与「没有相关记忆」是两回事，UI 应区分提示。
7. **`ConversationResponse.sessionId` 为 null 是常态。** 新对话、闲聊（未命中知识点）都不会归属 Session，不要渲染成异常。
8. **文档上传后状态是异步推进的。** 上传成功 ≠ 可检索；需要轮询 `status` 直到 `READY`，`FAILED` 可调 `reindex`。
9. **时间字段没有时区。** 直接 `new Date(value)` 会按浏览器本地时区解析，与后端语义一致；不要拼 `Z`。
10. **枚举是大小写敏感的大写字符串。** 查询参数里的 `status`（如 `GET /api/memories?status=ACTIVE`）同理，传小写会 `400`。
11. **成功判断用 `code === 0`。** 某些成功响应的 `message` 是中文提示而不是 `"success"`，不要用 `message` 判断。
12. **没有 logout / refresh token 接口。** 退出登录由前端清除本地 token 完成；token 过期后任何请求返回 401。

### 7.2 与旧文档的差异

`docs/database&API_design.md` 第 31 节列出的 11 个「公共 API」已与实现脱节，例如：

| 旧文档路径 | 当前实现 |
| --- | --- |
| `GET /api/spaces/{spaceId}/learning-overview` | 已拆分为 `GET /api/learning-state/overview`、`/attention`、`/topic-coverage` |
| `GET /api/spaces/{spaceId}/concepts/{conceptId}/learning` | 已改为 `GET /api/understandings/{conceptId}` + `/revisions` |
| `POST /api/test-tasks/{taskId}/answer` | 已改为 `POST /api/tests/{testId}/answer` |

**以本文档（源码审计结果）为准。**

### 7.3 本次审计未覆盖的内容

- 各 Service 的业务规则细节（如 Evidence 去重、Understanding 事务、Memory Outbox 重试策略）。契约层面已足够前端开发；需要理解「为什么状态会变」时再读 `docs/design/` 下的设计文档。
- Python Agent 的内部 HTTP 契约（`ChatAgentRequest` / `AnalyzeAgentRequest` 等），前端不接触。
- 数据库表结构（见 `backend/backend/src/main/resources/schema.sql`）。
