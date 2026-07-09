import { defineStore } from 'pinia'
import { ref } from 'vue'

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
  const userRole = ref<'admin' | 'user'>('admin')

  const isAdmin = () => userRole.value === 'admin'

  async function loadSkills() {
    loading.value = true
    try {
      const res = await fetch('/api/skills')
      const data = await res.json()
      skills.value = data.skills || []
    } catch (e) {
      console.warn('获取技能列表失败', e)
    } finally {
      loading.value = false
    }
  }

  async function loadUserRole() {
    try {
      const res = await fetch('/api/skills/user-role')
      const data = await res.json()
      userRole.value = data.role || 'admin'
    } catch (e) {
      console.warn('获取身份失败', e)
    }
  }

  async function setUserRole(role: 'admin' | 'user') {
    const res = await fetch('/api/skills/user-role', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ role })
    })
    const data = await res.json()
    userRole.value = data.role || role
  }

  async function saveSkill(payload: SkillPayload, id?: number): Promise<Skill> {
    const url = id ? `/api/skills/${id}` : '/api/skills'
    const method = id ? 'PUT' : 'POST'
    const res = await fetch(url, {
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
    const res = await fetch(`/api/skills/${id}/toggle`, {
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
    const res = await fetch(`/api/skills/${id}`, { method: 'DELETE' })
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
    const res = await fetch(`/api/skills/import?overwrite=${overwrite}`, {
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
