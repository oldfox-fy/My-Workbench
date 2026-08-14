// frontend/src/api/memory.ts
// 记忆管理 API：结构化长期记忆（user_memories）+ 原始会话记忆（session_memories）

import { apiFetch } from '@/api/client'

export interface StructuredMemory {
  id: number
  category: string
  content: string
  source_chat_id?: string
  source_message_id?: number | null
  created_at?: string
  updated_at?: string
}

export interface RawMemory {
  id: number
  chat_id: string
  message_id: number
  content: string
  role: string
  created_at?: string
}

export interface MemoryStats {
  structured: { total: number; by_category: Record<string, number> }
  raw: { total: number; by_role: Record<string, number> }
}

export interface CategoryMap {
  [key: string]: string
}

async function parseError(res: Response, fallback: string): Promise<never> {
  const err = await res.json().catch(() => ({}))
  throw new Error(err.detail || fallback)
}

// ---------- 结构化长期记忆 ----------

export async function listMemories(category = '', limit = 500): Promise<{
  memories: StructuredMemory[]
  categories: CategoryMap
}> {
  const qs = new URLSearchParams({ limit: String(limit) })
  if (category) qs.set('category', category)
  const res = await apiFetch(`/api/memory?${qs}`)
  if (!res.ok) return parseError(res, '获取记忆失败')
  return res.json()
}

export async function createMemory(content: string, category = 'fact'): Promise<StructuredMemory> {
  const res = await apiFetch('/api/memory', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ content, category }),
  })
  if (!res.ok) return parseError(res, '新增记忆失败')
  return res.json()
}

export async function updateMemory(
  id: number,
  patch: { content?: string; category?: string }
): Promise<void> {
  const res = await apiFetch(`/api/memory/${id}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(patch),
  })
  if (!res.ok) return parseError(res, '保存记忆失败')
}

export async function deleteMemory(id: number): Promise<void> {
  const res = await apiFetch(`/api/memory/${id}`, { method: 'DELETE' })
  if (!res.ok) return parseError(res, '删除记忆失败')
}

// ---------- 原始会话记忆 ----------

export async function listRawMemories(role = '', limit = 500): Promise<{ memories: RawMemory[] }> {
  const qs = new URLSearchParams({ limit: String(limit) })
  if (role) qs.set('role', role)
  const res = await apiFetch(`/api/memory/raw?${qs}`)
  if (!res.ok) return parseError(res, '获取会话记忆失败')
  return res.json()
}

export async function deleteRawMemory(id: number): Promise<void> {
  const res = await apiFetch(`/api/memory/raw/${id}`, { method: 'DELETE' })
  if (!res.ok) return parseError(res, '删除会话记忆失败')
}

// ---------- 统计 ----------

export async function getMemoryStats(): Promise<MemoryStats> {
  const res = await apiFetch('/api/memory/stats')
  if (!res.ok) return parseError(res, '获取记忆统计失败')
  return res.json()
}
