# AI Learning Space · Web

Vue 3 + TypeScript + Vite 前端工程。

## 技术栈

- Vue 3（`<script setup>`）
- TypeScript + Vite
- Vue Router
- Pinia
- Axios
- Element Plus
- marked（Markdown 渲染，待接入对话页）

## 快速开始

```bash
npm install
npm run dev
```

默认地址：<http://localhost:5173>

后端默认地址：<http://localhost:8080>，可通过 `web/.env.development` 的
`VITE_API_BASE_URL` 覆盖。

## 目录结构

```
src/
├── api/          # axios 实例与业务 API 模块（当前只有 http.ts）
├── components/   # 通用组件
├── layouts/      # 页面布局（MainLayout.vue）
├── router/       # 路由与侧边栏导航定义
├── stores/       # Pinia store
├── types/        # TypeScript 类型（api.ts 对应后端契约）
├── views/        # 页面
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
