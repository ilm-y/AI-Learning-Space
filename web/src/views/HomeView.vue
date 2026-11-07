<script setup lang="ts">
const apiBaseUrl = import.meta.env.VITE_API_BASE_URL ?? 'http://localhost:8080'

const sections = [
  { title: '学习空间', path: '/spaces', hint: '空间与知识点' },
  { title: '对话', path: '/conversations', hint: 'SSE 流式聊天' },
  { title: '文档', path: '/documents', hint: '上传与索引状态' },
  { title: '学习状态', path: '/learning-state', hint: '理解与证据链' },
  { title: '记忆', path: '/memories', hint: '长期记忆检索' },
]
</script>

<template>
  <div class="home">
    <el-card shadow="never" class="welcome">
      <h1>AI Learning Space</h1>
      <p>
        登录后即可使用对话、文档、学习状态与记忆模块。接口契约见
        <code>docs/design/FRONTEND_API_SPEC.md</code>，类型定义见
        <code>src/types/api.ts</code>。
      </p>
      <el-descriptions :column="1" border class="meta">
        <el-descriptions-item label="API Base URL">
          {{ apiBaseUrl }}
        </el-descriptions-item>
        <el-descriptions-item label="请求层">
          src/api/http.ts（自动注入 Bearer token、解包 ApiResponse）
        </el-descriptions-item>
        <el-descriptions-item label="当前阶段">
          登录 / 对话 / 文档 / 学习状态 / 记忆已接入
        </el-descriptions-item>
      </el-descriptions>
    </el-card>

    <div class="sections">
      <router-link
        v-for="section in sections"
        :key="section.path"
        :to="section.path"
        class="section-link"
      >
        <el-card shadow="hover">
          <div class="section-title">{{ section.title }}</div>
          <div class="section-hint">{{ section.hint }}</div>
        </el-card>
      </router-link>
    </div>
  </div>
</template>

<style scoped>
.home {
  display: flex;
  flex-direction: column;
  gap: 20px;
}

.welcome h1 {
  margin: 0 0 8px;
  font-size: 24px;
  color: #111827;
}

.welcome p {
  margin: 0 0 18px;
  color: #6b7280;
}

.welcome code {
  padding: 2px 6px;
  border-radius: 4px;
  background: #f3f4f6;
  color: #374151;
  font-size: 13px;
}

.meta {
  max-width: 640px;
}

.sections {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
  gap: 16px;
}

.section-link {
  text-decoration: none;
}

.section-title {
  font-size: 15px;
  font-weight: 600;
  color: #111827;
  margin-bottom: 6px;
}

.section-hint {
  font-size: 13px;
  color: #9ca3af;
}
</style>
