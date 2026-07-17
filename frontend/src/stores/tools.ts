import { defineStore } from 'pinia'
import { ref } from 'vue'
import { apiFetch } from '@/api/client'


export interface toolConfig {
  title: string        // 名称
  description: string  // 描述
  is_skill?: boolean   // 是否为技能（code 型 skill）
  isolated?: boolean   // 是否隔离执行
}

export const useToolStore = defineStore('tools', () => {
  const toolsInfo = ref<toolConfig[]>([])
    async function loadToolsInfo() {
        const res = await apiFetch('/api/tools-info')
        const data = await res.json()
        toolsInfo.value = data
    }

  return {
    loadToolsInfo,
    toolsInfo
  }
})