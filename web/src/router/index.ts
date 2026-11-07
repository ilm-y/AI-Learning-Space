import { createRouter, createWebHistory } from 'vue-router'
import MainLayout from '@/layouts/MainLayout.vue'
import { TOKEN_STORAGE_KEY } from '@/api/http'

export { navigation } from './navigation'

declare module 'vue-router' {
  interface RouteMeta {
    /** 页面标题，用于侧边栏与浏览器标题 */
    title?: string
    /** 占位页说明 */
    description?: string
    /** 无需登录即可访问（目前只有登录页） */
    public?: boolean
  }
}

const PlaceholderView = () => import('@/views/PlaceholderView.vue')

const router = createRouter({
  history: createWebHistory(import.meta.env.BASE_URL),
  routes: [
    {
      path: '/login',
      name: 'login',
      component: () => import('@/views/LoginView.vue'),
      meta: { title: '登录', public: true },
    },
    {
      path: '/',
      component: MainLayout,
      children: [
        {
          path: '',
          name: 'home',
          component: () => import('@/views/HomeView.vue'),
          meta: { title: '概览' },
        },
        {
          path: 'spaces',
          name: 'spaces',
          component: PlaceholderView,
          meta: { title: '学习空间', description: '学习空间与知识点的管理页面将在这里实现。' },
        },
        {
          path: 'conversations',
          name: 'conversations',
          component: () => import('@/views/chat/ChatView.vue'),
          meta: { title: '对话' },
        },
        {
          path: 'documents',
          name: 'documents',
          component: () => import('@/views/documents/DocumentsView.vue'),
          meta: { title: '文档' },
        },
        {
          path: 'learning-state',
          name: 'learning-state',
          component: () => import('@/views/learning/LearningView.vue'),
          meta: { title: '学习状态' },
        },
        {
          path: 'memories',
          name: 'memories',
          component: () => import('@/views/memories/MemoriesView.vue'),
          meta: { title: '记忆' },
        },
      ],
    },
    {
      path: '/:pathMatch(.*)*',
      redirect: '/',
    },
  ],
})

router.beforeEach((to) => {
  const token = localStorage.getItem(TOKEN_STORAGE_KEY)
  if (!to.meta.public && !token) {
    return { name: 'login', query: { redirect: to.fullPath } }
  }
  if (to.name === 'login' && token) {
    return { path: '/' }
  }
  return true
})

router.afterEach((to) => {
  const title = to.meta.title
  document.title = title ? `${title} · AI Learning Space` : 'AI Learning Space'
})

export default router
