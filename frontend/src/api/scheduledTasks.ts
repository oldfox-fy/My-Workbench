// frontend/src/api/scheduledTasks.ts
// AI 定时自动化任务 API（按登录用户隔离）

import { apiFetch } from '@/api/client'

export type ScheduleType = 'once' | 'daily' | 'workdays' | 'weekly' | 'monthly' | 'on_startup'

export interface ScheduledTask {
  id: number
  user_id: number
  title: string
  prompt: string
  schedule_type: ScheduleType
  time_of_day: string
  weekday: number | null
  monthday: number | null
  run_at: string | null
  next_run_at: string | null
  status: 'active' | 'paused' | 'completed'
  model_config: string | Record<string, any>
  enable_tools: number
  profile_id: number | null
  auto_approve: number
  create_chat: number
  created_at: string
  updated_at: string
  human_readable?: string
  last_run_status?: string | null
  last_run_at?: string | null
}

export interface TaskRun {
  id: number
  task_id: number
  status: 'running' | 'success' | 'failed'
  scheduled_for: string | null
  started_at: string
  finished_at: string | null
  output: string
  error: string
}

export interface ParseResult {
  title: string
  prompt: string
  schedule_type: ScheduleType
  time_of_day: string
  weekday: number | null
  monthday: number | null
  run_at: string | null
  human_readable: string
}

export interface TaskPayload {
  prompt: string
  schedule_type?: ScheduleType
  time_of_day?: string
  weekday?: number | null
  monthday?: number | null
  run_at?: string | null
  title?: string
  model_config?: Record<string, any>
  enable_tools?: boolean
  profile_id?: number | null
  auto_approve?: boolean
  create_chat?: boolean
}

async function _json<T>(res: Response): Promise<T> {
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }))
    throw new Error(err.detail || '请求失败')
  }
  return res.json() as Promise<T>
}

function _post(url: string, body: any) {
  return apiFetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
}

export async function parseTask(text: string, modelConfig?: Record<string, any>): Promise<ParseResult> {
  const res = await _post('/api/scheduled-tasks/parse', { text, model_config: modelConfig })
  return _json<ParseResult>(res)
}

export async function listTasks(): Promise<ScheduledTask[]> {
  const res = await apiFetch('/api/scheduled-tasks')
  const data = await _json<{ tasks: ScheduledTask[] }>(res)
  return data.tasks
}

export async function getTask(id: number): Promise<ScheduledTask> {
  const res = await apiFetch(`/api/scheduled-tasks/${id}`)
  return _json<ScheduledTask>(res)
}

export async function createTask(payload: TaskPayload): Promise<ScheduledTask> {
  const res = await _post('/api/scheduled-tasks', payload)
  return _json<ScheduledTask>(res)
}

export async function updateTask(id: number, payload: Partial<TaskPayload>): Promise<ScheduledTask> {
  const res = await apiFetch(`/api/scheduled-tasks/${id}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
  return _json<ScheduledTask>(res)
}

export async function toggleTask(id: number): Promise<ScheduledTask> {
  const res = await _post(`/api/scheduled-tasks/${id}/toggle`, {})
  return _json<ScheduledTask>(res)
}

export async function deleteTask(id: number): Promise<void> {
  const res = await apiFetch(`/api/scheduled-tasks/${id}`, { method: 'DELETE' })
  await _json(res)
}

export async function listRuns(id: number): Promise<TaskRun[]> {
  const res = await apiFetch(`/api/scheduled-tasks/${id}/runs`)
  const data = await _json<{ runs: TaskRun[] }>(res)
  return data.runs
}

export async function runNow(id: number): Promise<void> {
  const res = await _post(`/api/scheduled-tasks/${id}/run-now`, {})
  await _json(res)
}
