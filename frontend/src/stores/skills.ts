import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { useAuthStore } from '@/stores/auth'
import { apiFetch } from '@/api/client'

export type SkillType = 'prompt' | 'code'

export interface Skill {
  id: number
  name: string
  title: string
  description: string
  skill_type: SkillType
  enabled: boolean
  instruction: string
  tools: string[]
  code: string
  parameters: Record<string, any>
  isolated: boolean
}

export type SkillPayload = Omit<Skill, 'id'>

export const useSkillStore = defineStore('skill', () => {
  const skills = ref<Skill[]>([])
  const loading = ref(false)

  /** 当前用户身份（由 authStore 提供） */
  const userRole = computed<'admin' | 'user'>(() => {
    const auth = useAuthStore()
    return auth.user?.role === 'admin' ? 'admin' : 'user'
  })

  const isAdmin = () => {
    const auth = useAuthStore()
    return auth.isAdmin
  }

  async function loadSkills() {
    loading.value = true
    try {
      const res = await apiFetch('/api/skills')
      const data = await res.json()
      skills.value = data.skills || []
    } catch (e) {
      console.warn('获取技能列表失败', e)
    } finally {
      loading.value = false
    }
  }

  /** @deprecated 身份由 authStore 统一管理，保留以兼容旧代码 */
  async function loadUserRole() {
    // 不再从后端获取，authStore.init() 中已处理
  }

  /** @deprecated 身份由 authStore 统一管理，普通用户无法切换 */
  async function setUserRole(_role: 'admin' | 'user') {
    // 不再支持本地切换，需要通过登录系统
  }

  async function saveSkill(payload: SkillPayload, id?: number): Promise<Skill> {
    const url = id ? `/api/skills/${id}` : '/api/skills'
    const method = id ? 'PUT' : 'POST'
    const res = await apiFetch(url, {
      method,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    })
    const data = await res.json()
    if (!res.ok) throw new Error(data.detail || '保存失败')
    await loadSkills()
    return data
  }

  async function toggleSkill(id: number, enabled: boolean): Promise<void> {
    const res = await apiFetch(`/api/skills/${id}/toggle`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled })
    })
    if (!res.ok) {
      const data = await res.json().catch(() => ({}))
      throw new Error(data.detail || '操作失败')
    }
    await loadSkills()
  }

  async function deleteSkill(id: number): Promise<void> {
    const res = await apiFetch(`/api/skills/${id}`, { method: 'DELETE' })
    if (!res.ok) {
      const data = await res.json().catch(() => ({}))
      throw new Error(data.detail || '删除失败')
    }
    await loadSkills()
  }

  // 上传本地 skill 压缩包注册技能。name 冲突时抛出 conflict=true，供上层询问是否覆盖。
  async function importSkillPackage(file: File, overwrite = false): Promise<Skill> {
    const form = new FormData()
    form.append('file', file)
    const res = await apiFetch(`/api/skills/import?overwrite=${overwrite}`, {
      method: 'POST',
      body: form
    })
    const data = await res.json().catch(() => ({}))
    if (!res.ok) {
      const err = new Error(data.detail || '导入失败') as Error & { conflict?: boolean }
      err.conflict = res.status === 409
      throw err
    }
    await loadSkills()
    return data
  }

  // 导出技能为压缩包的下载地址。
  function exportSkillUrl(id: number): string {
    return `/api/skills/${id}/export`
  }

  return {
    skills, loading, userRole, isAdmin,
    loadSkills, loadUserRole, setUserRole,
    saveSkill, toggleSkill, deleteSkill,
    importSkillPackage, exportSkillUrl
  }
})
