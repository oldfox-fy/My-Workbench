import { defineStore } from 'pinia'
import { ref, computed } from 'vue'

export interface Profile {
  id: number
  name: string
  tools: string[]
  profile_prompt: string
  temperature: number
  top_p: number
  top_k: number
  frequency_penalty: number
  presence_penalty: number
  skills: string[]
}

export const VIRTUAL_PROFILE_ID = 0

export const useProfileStore = defineStore('profile', () => {
  const profiles = ref<Profile[]>([])
  const activeProfileId = ref<number | null>(null)

  async function loadProfiles() {
    const res = await fetch('/api/profiles/')
    let data = await res.json()
    // 确保每个角色都有生成参数默认值（后端可能尚未返回这些字段）
    data = data.map((p: any) => ({
      ...p,
      temperature: p.temperature ?? 1,
      top_p: p.top_p ?? 1,
      top_k: p.top_k ?? 40,
      frequency_penalty: p.frequency_penalty ?? 0,
      presence_penalty: p.presence_penalty ?? 0,
      skills: p.skills ?? [],
    }))
    profiles.value = data

    // 校验/恢复 activeProfileId
    const pid = localStorage.getItem('activeProfileId')
    const savedId = pid ? Number(pid) : null

    if (savedId && profiles.value.find(p => p.id === savedId)) {
      activeProfileId.value = savedId
    } else if (profiles.value.length > 0) {
      // 默认选中第一个角色（管理员看到全能助手在首位）
      activeProfileId.value = profiles.value[0].id
    } else {
      activeProfileId.value = null
    }

    // 持久化
    if (activeProfileId.value != null) {
      localStorage.setItem('activeProfileId', String(activeProfileId.value))
    } else {
      localStorage.removeItem('activeProfileId')
    }
  }

  async function createProfile(
    name: string,
    tools: string[] = [],
    profile_prompt: string = '',
    temperature: number = 1,
    top_p: number = 1,
    top_k: number = 40,
    frequency_penalty: number = 0,
    presence_penalty: number = 0,
    skills: string[] = []
  ) {
    const res = await fetch('/api/profiles/', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, tools, profile_prompt, temperature, top_p, top_k, frequency_penalty, presence_penalty, skills })
    })
    const newProfile = await res.json()
    // 补全默认值（以防后端未完整返回）
    const completeProfile: Profile = {
      ...newProfile,
      temperature: newProfile.temperature ?? 1,
      top_p: newProfile.top_p ?? 1,
      top_k: newProfile.top_k ?? 40,
      frequency_penalty: newProfile.frequency_penalty ?? 0,
      presence_penalty: newProfile.presence_penalty ?? 0,
      skills: newProfile.skills ?? [],
    }
    profiles.value.push(completeProfile)
    activeProfileId.value = completeProfile.id
    return completeProfile
  }

  async function updateProfile(
    id: number,
    name: string,
    tools: string[],
    profile_prompt: string = '',
    temperature?: number,
    top_p?: number,
    top_k?: number,
    frequency_penalty?: number,
    presence_penalty?: number,
    skills?: string[]
  ) {
    if (id === VIRTUAL_PROFILE_ID) {
      console.warn('内置角色不可编辑')
      return
    }
    await fetch(`/api/profiles/${id}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, tools, profile_prompt, temperature, top_p, top_k, frequency_penalty, presence_penalty, skills: skills ?? [] })
    })
    const profile = profiles.value.find(p => p.id === id)
    if (profile) {
      profile.name = name
      profile.tools = tools
      profile.profile_prompt = profile_prompt
      if (temperature !== undefined) profile.temperature = temperature
      if (top_p !== undefined) profile.top_p = top_p
      if (top_k !== undefined) profile.top_k = top_k
      if (frequency_penalty !== undefined) profile.frequency_penalty = frequency_penalty
      if (presence_penalty !== undefined) profile.presence_penalty = presence_penalty
      if (skills !== undefined) profile.skills = skills
    }
  }

  async function deleteProfile(id: number) {
    if (id === VIRTUAL_PROFILE_ID) {
      console.warn('内置角色不可删除')
      return
    }
    await fetch(`/api/profiles/${id}`, { method: 'DELETE' })
    profiles.value = profiles.value.filter(p => p.id !== id)
    if (activeProfileId.value === id) {
      activeProfileId.value = profiles.value[0]?.id ?? null
      localStorage.setItem('activeProfileId', activeProfileId.value?.toString() ?? '')
    }
  }

  const activeProfile = computed(() => profiles.value.find(p => p.id === activeProfileId.value))

  const activeToolsSet = computed(() => {
    const p = activeProfile.value
    return p ? new Set(p.tools) : new Set<string>()
  })

  return { profiles, activeProfileId, activeProfile, activeToolsSet, loadProfiles, createProfile, updateProfile, deleteProfile }
})