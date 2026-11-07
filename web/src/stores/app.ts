import { defineStore } from 'pinia'
import { ref } from 'vue'

/** 全局 UI 状态。业务域 store（auth / space / conversation ...）后续按模块新增。 */
export const useAppStore = defineStore('app', () => {
  const sidebarCollapsed = ref(false)

  function toggleSidebar() {
    sidebarCollapsed.value = !sidebarCollapsed.value
  }

  return { sidebarCollapsed, toggleSidebar }
})
