# AI Learning Space · Web

Vue 3 + TypeScript + Vite 前端工程，是项目**官方推荐的客户端**。后端 API 与 Agent
通过 HTTP / SSE 对接，不依赖 Flutter 桌面端。

完整的本地启动、注册初始化与端到端演示步骤见
[`docs/design/LOCAL_DEMO_GUIDE.md`](../docs/design/LOCAL_DEMO_GUIDE.md)。

## 已实现功能

| 模块 | 能力 |
| --- | --- |
| 登录守卫 | `/login` 为公开路由；其余路由在 `router.beforeEach` 中校验 `ails_token`，未登录跳转 `/login?redirect=...`，401 时清除 token 并回到登录页 |
| SSE 对话 | 基于 `fetch` 流式读取，逐字渲染回答；支持停止生成、错误提示、会话历史加载 |
| 引用展示 | 回答中的引用标记与来源列表；Markdown 经 `marked` 渲染后由 `DOMPurify` 净化 |
| 文档管理 | 上传、列表与状态轮询（处理中 / 就绪 / 失败）、重新索引、删除 |
| 记忆管理 | 记忆列表、混合检索（关键词 / 概念过滤）、编辑、归档 |
| 学习状态 | 按概念查看掌握度与学习进度 |
| 概览 | 空间与学习数据汇总 |
| 学习空间 | `/spaces` 目前为占位页（见 [`docs/audit/KNOWN_LIMITATIONS.md`](../docs/audit/KNOWN_LIMITATIONS.md) 的 `WEB-01`） |

> 注册页尚未实现。新用户需先通过 `POST /api/auth/register` 注册、再通过
> `POST /api/spaces` 创建空间，命令见 `LOCAL_DEMO_GUIDE.md` 第 4 节。

## 技术栈

- Vue 3（`<script setup>`）
- TypeScript + Vite
- Vue Router（全局登录守卫）
- Pinia
- Axios（统一响应解包、401 处理）
- Element Plus
- marked + DOMPurify（Markdown 渲染与 XSS 净化）

## 快速开始

前置条件：Node.js 20.19+ 或 22.12+（Vite 8 要求），后端已在
`http://localhost:8080` 启动。

```bash
npm install
npm run dev
```

默认地址：<http://localhost:5173>

后端地址可通过 `web/.env.development` 的 `VITE_API_BASE_URL` 覆盖。首次登录前
请按 `LOCAL_DEMO_GUIDE.md` 完成注册与空间初始化。

## 目录结构

```
src/
├── api/          # axios 实例与业务 API 模块
├── components/   # 通用组件（聊天、引用等）
├── layouts/      # 页面布局（MainLayout.vue）
├── router/       # 路由、登录守卫与侧边栏导航
├── stores/       # Pinia store（认证等）
├── types/        # TypeScript 类型（api.ts 对应后端契约）
├── views/        # 页面（登录、概览、对话、文档、记忆、学习状态）
├── App.vue
└── main.ts
```

## 接口契约

- 契约文档：`../docs/design/FRONTEND_API_SPEC.md`
- 类型定义：`src/types/api.ts`
- 请求层：`src/api/http.ts`
  - 自动注入 `Authorization: Bearer <token>`
  - 自动解包统一响应体 `ApiResponse<T>`
  - 业务错误统一抛 `ApiError`
  - 401 时清除 token 并广播 `ails:unauthorized`

token 存储键为 `ails_token`（`localStorage`）。

## 构建

```bash
npm run build
npm run preview
```
