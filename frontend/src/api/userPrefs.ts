// frontend/src/api/userPrefs.ts
// 用户偏好设置 API（替代 localStorage，按登录用户隔离）

import { apiFetch } from '@/api/client'

export interface UserPrefs {
  themeMode: 'light' | 'dark'
  themeAccent: string
  themeRadius: number
  themeFontSize: string
  thinking: boolean
  autoRead: boolean
  autoSwitch: boolean
  enableProfile: boolean
  activeModelId: string
  activeProfileId: number | null
}

let _prefs: Partial<UserPrefs> | null = null

/** 从后端加载当前用户的偏好（登录后调用一次） */
export async function loadUserPrefs(): Promise<Partial<UserPrefs>> {
  const res = await apiFetch('/api/user/prefs')
  _prefs = await res.json()
  return _prefs!
}

/** 增量保存偏好（只传变更的字段） */
export async function saveUserPrefs(delta: Partial<UserPrefs>): Promise<void> {
  // 更新本地缓存
  if (_prefs) Object.assign(_prefs, delta)
  await apiFetch('/api/user/prefs', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ prefs: delta }),
  }).catch(() => {})
}

/** 获取已缓存的偏好值（可能为空，需先 loadUserPrefs） */
export function getUserPrefs(): Partial<UserPrefs> | null {
  return _prefs
}
