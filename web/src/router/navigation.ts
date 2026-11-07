/** 侧边栏导航。后续接入业务页面时保持 path 不变即可。 */
export interface NavItem {
  path: string
  label: string
  name: string
}

export const navigation: NavItem[] = [
  { path: '/', label: '概览', name: 'home' },
  { path: '/spaces', label: '学习空间', name: 'spaces' },
  { path: '/conversations', label: '对话', name: 'conversations' },
  { path: '/documents', label: '文档', name: 'documents' },
  { path: '/learning-state', label: '学习状态', name: 'learning-state' },
  { path: '/memories', label: '记忆', name: 'memories' },
]
