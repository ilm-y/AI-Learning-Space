<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { TOKEN_STORAGE_KEY, UNAUTHORIZED_EVENT } from '@/api/http'
import { navigation } from '@/router/navigation'
import { useAppStore } from '@/stores/app'

const route = useRoute()
const router = useRouter()
const appStore = useAppStore()

function handleLogout() {
  localStorage.removeItem(TOKEN_STORAGE_KEY)
  ElMessage.success('已退出登录')
  void router.replace({ name: 'login' })
}

/** 请求层遇到 401 时会广播该事件；这里统一回到登录页。 */
function handleUnauthorized() {
  void router.replace({ name: 'login' })
}

onMounted(() => {
  window.addEventListener(UNAUTHORIZED_EVENT, handleUnauthorized)
})

onBeforeUnmount(() => {
  window.removeEventListener(UNAUTHORIZED_EVENT, handleUnauthorized)
})

/** 子路由高亮：/spaces/1 也能匹配到 /spaces。 */
const activePath = computed(() => {
  const matched = navigation.find(
    (item) => item.path !== '/' && route.path.startsWith(item.path),
  )
  return matched?.path ?? '/'
})
</script>

<template>
  <el-container class="main-layout">
    <el-aside
      :width="appStore.sidebarCollapsed ? '64px' : '220px'"
      class="sidebar"
    >
      <div class="brand">
        <span class="brand-mark">AI</span>
        <span v-show="!appStore.sidebarCollapsed" class="brand-text">
          Learning Space
        </span>
      </div>

      <el-menu
        :default-active="activePath"
        :collapse="appStore.sidebarCollapsed"
        :collapse-transition="false"
        router
        class="nav"
      >
        <el-menu-item
          v-for="item in navigation"
          :key="item.path"
          :index="item.path"
        >
          <span class="nav-dot" />
          <span v-show="!appStore.sidebarCollapsed" class="nav-label">
            {{ item.label }}
          </span>
        </el-menu-item>
      </el-menu>

      <div v-show="!appStore.sidebarCollapsed" class="sidebar-footer">
        API · localhost:8080
      </div>
    </el-aside>

    <el-container class="content">
      <el-header class="header">
        <el-button link class="collapse-btn" @click="appStore.toggleSidebar">
          {{ appStore.sidebarCollapsed ? '展开' : '收起' }}
        </el-button>
        <span class="header-title">
          {{ route.meta.title ?? 'AI Learning Space' }}
        </span>
        <div class="header-actions" style="display: flex; align-items: center; gap: 12px">
          <el-tag type="info" effect="plain">Vue 3 · Vite</el-tag>
          <el-button link type="danger" @click="handleLogout">退出登录</el-button>
        </div>
      </el-header>

      <el-main class="main">
        <router-view />
      </el-main>
    </el-container>
  </el-container>
</template>

<style scoped>
.main-layout {
  height: 100vh;
  overflow: hidden;
}

.sidebar {
  display: flex;
  flex-direction: column;
  background: #1f2937;
  color: #e5e7eb;
  transition: width 0.2s ease;
  overflow: hidden;
}

.brand {
  display: flex;
  align-items: center;
  gap: 10px;
  height: 60px;
  padding: 0 18px;
  flex-shrink: 0;
}

.brand-mark {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 30px;
  height: 30px;
  border-radius: 8px;
  background: #3b82f6;
  color: #fff;
  font-size: 13px;
  font-weight: 700;
  letter-spacing: 0.5px;
}

.brand-text {
  font-size: 15px;
  font-weight: 600;
  white-space: nowrap;
}

.nav {
  flex: 1;
  border-right: none;
  background: transparent;
  padding: 8px;
}

.nav :deep(.el-menu-item) {
  color: #cbd5e1;
  border-radius: 8px;
  margin-bottom: 4px;
  height: 44px;
}

.nav :deep(.el-menu-item:hover) {
  background: rgba(255, 255, 255, 0.08);
  color: #fff;
}

.nav :deep(.el-menu-item.is-active) {
  background: #2563eb;
  color: #fff;
}

.nav-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: currentColor;
  opacity: 0.7;
  margin-right: 12px;
  flex-shrink: 0;
}

.nav-label {
  white-space: nowrap;
}

.sidebar-footer {
  padding: 14px 18px;
  font-size: 12px;
  color: #94a3b8;
  border-top: 1px solid rgba(255, 255, 255, 0.08);
  white-space: nowrap;
}

.content {
  min-width: 0;
  background: #f5f7fa;
}

.header {
  display: flex;
  align-items: center;
  gap: 16px;
  background: #fff;
  border-bottom: 1px solid #e5e7eb;
}

.collapse-btn {
  color: #6b7280;
}

.header-title {
  font-size: 16px;
  font-weight: 600;
  color: #111827;
}

.header-actions {
  margin-left: auto;
}

.main {
  padding: 24px;
  overflow: auto;
}
</style>
