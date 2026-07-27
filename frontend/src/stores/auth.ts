// frontend/src/stores/auth.ts
// 用户认证状态管理（Pinia Composition API）

import { defineStore } from 'pinia'
import { ref, computed } from 'vue'

export interface UserInfo {
  id: number
  username: string
  role: 'admin' | 'user'
  status: 'active' | 'pending' | 'disabled'
  kb_path: string
  workspace_path: string
  standalone?: boolean
}

export interface AuditLogEntry {
  id: number
  user_id: number | null
  username: string
  action: string
  target: string
  detail: string
  ip_address: string
  created_at: string
}

const TOKEN_KEY = 'mywb_token'

export const useAuthStore = defineStore('auth', () => {
  // ── State ──
  const token = ref<string>(localStorage.getItem(TOKEN_KEY) || '')
  const user = ref<UserInfo | null>(null)
  const initialized = ref(false)
  const users = ref<UserInfo[]>([])
  const pendingCount = ref(0)
  const auditLogs = ref<AuditLogEntry[]>([])

  // ── Getters ──
  const isLoggedIn = computed(() => !!token.value && !!user.value)
  const isAdmin = computed(() => user.value?.role === 'admin')

  // ── Helpers ──
  function authHeaders(): Record<string, string> {
    return token.value ? { 'Authorization': `Bearer ${token.value}` } : {}
  }

  // ── Actions ──

  /** 初始化：校验本地 token 是否有效 */
  async function init(): Promise<boolean> {
    if (initialized.value) return isLoggedIn.value
    initialized.value = true

    if (!token.value) return false

    try {
      const res = await fetch('/api/auth/me', {
        headers: authHeaders(),
      })
      if (res.ok) {
        user.value = await res.json()
        // 如果是管理员，获取待审批数量
        if (user.value?.role === 'admin') {
          fetchPendingCount()
        }
        return true
      } else {
        // token 无效，清除
        logoutLocal()
        return false
      }
    } catch {
      // 网络错误，保留 token 供重试
      return !!user.value
    }
  }

  /** 登录 */
  async function login(username: string, password: string, rememberMe: boolean): Promise<void> {
    const res = await fetch('/api/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password, remember_me: rememberMe }),
    })
    const data = await res.json()
    if (!res.ok) throw new Error(data.detail || '登录失败')

    token.value = data.token
    user.value = data.user
    localStorage.setItem(TOKEN_KEY, data.token)

    if (data.user.role === 'admin') {
      fetchPendingCount()
    }
  }

  /** 注册 */
  async function register(username: string, password: string): Promise<{ message: string }> {
    const res = await fetch('/api/auth/register', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password }),
    })
    const data = await res.json()
    if (!res.ok) throw new Error(data.detail || '注册失败')
    return data
  }

  /** 登出 */
  async function logout(): Promise<void> {
    try {
      await fetch('/api/auth/logout', {
        method: 'POST',
        headers: authHeaders(),
      })
    } catch { /* ignore */ }
    logoutLocal()
  }

  function logoutLocal(): void {
    token.value = ''
    user.value = null
    localStorage.removeItem(TOKEN_KEY)
  }

  /** 修改密码 */
  async function changePassword(oldPassword: string, newPassword: string): Promise<void> {
    const res = await fetch('/api/auth/change-password', {
      method: 'POST',
      headers: { ...authHeaders(), 'Content-Type': 'application/json' },
      body: JSON.stringify({ old_password: oldPassword, new_password: newPassword }),
    })
    const data = await res.json()
    if (!res.ok) throw new Error(data.detail || '修改密码失败')
  }

  // ── 管理员功能 ──

  /** 加载用户列表 */
  async function loadUsers(): Promise<void> {
    const res = await fetch('/api/admin/users', {
      headers: authHeaders(),
    })
    if (!res.ok) throw new Error('获取用户列表失败')
    users.value = await res.json()
  }

  /** 审批通过用户 */
  async function approveUser(userId: number): Promise<void> {
    const res = await fetch(`/api/admin/users/${userId}/approve`, {
      method: 'POST',
      headers: authHeaders(),
    })
    const data = await res.json()
    if (!res.ok) throw new Error(data.detail || '操作失败')
    await loadUsers()
    await fetchPendingCount()
  }

  /** 禁用用户 */
  async function disableUser(userId: number): Promise<void> {
    const res = await fetch(`/api/admin/users/${userId}/disable`, {
      method: 'POST',
      headers: authHeaders(),
    })
    const data = await res.json()
    if (!res.ok) throw new Error(data.detail || '操作失败')
    await loadUsers()
  }

  /** 启用用户 */
  async function enableUser(userId: number): Promise<void> {
    const res = await fetch(`/api/admin/users/${userId}/enable`, {
      method: 'POST',
      headers: authHeaders(),
    })
    const data = await res.json()
    if (!res.ok) throw new Error(data.detail || '操作失败')
    await loadUsers()
  }

  /** 删除用户 */
  async function deleteUser(userId: number): Promise<void> {
    const res = await fetch(`/api/admin/users/${userId}`, {
      method: 'DELETE',
      headers: authHeaders(),
    })
    const data = await res.json()
    if (!res.ok) throw new Error(data.detail || '操作失败')
    await loadUsers()
  }

  /** 重置用户密码 */
  async function resetUserPassword(userId: number, newPassword: string): Promise<void> {
    const res = await fetch(`/api/admin/users/${userId}/reset-password`, {
      method: 'POST',
      headers: { ...authHeaders(), 'Content-Type': 'application/json' },
      body: JSON.stringify({ new_password: newPassword }),
    })
    const data = await res.json()
    if (!res.ok) throw new Error(data.detail || '操作失败')
  }

  /** 获取待审批用户数 */
  async function fetchPendingCount(): Promise<void> {
    try {
      const res = await fetch('/api/auth/pending-count', {
        headers: authHeaders(),
      })
      if (res.ok) {
        const data = await res.json()
        pendingCount.value = data.count || 0
      }
    } catch { /* ignore */ }
  }

  /** 加载审计日志 */
  async function loadAuditLogs(action?: string, limit = 100, offset = 0): Promise<void> {
    let url = `/api/admin/audit-logs?limit=${limit}&offset=${offset}`
    if (action) url += `&action=${encodeURIComponent(action)}`
    const res = await fetch(url, { headers: authHeaders() })
    if (!res.ok) throw new Error('获取审计日志失败')
    auditLogs.value = await res.json()
  }

  return {
    // state
    token, user, initialized, users, pendingCount, auditLogs,
    // getters
    isLoggedIn, isAdmin,
    // actions
    init, login, register, logout, logoutLocal, changePassword,
    loadUsers, approveUser, disableUser, enableUser, deleteUser, resetUserPassword,
    fetchPendingCount, loadAuditLogs,
  }
})
