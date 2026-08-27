// frontend/src/stores/scheduledTasks.ts
// 定时任务状态（任务列表 + 增删改查）

import { defineStore } from 'pinia'
import { ref } from 'vue'
import {
  listTasks,
  createTask,
  updateTask,
  toggleTask,
  deleteTask,
  runNow,
  type ScheduledTask,
  type TaskPayload,
} from '@/api/scheduledTasks'

export const useScheduledTasksStore = defineStore('scheduledTasks', () => {
  const tasks = ref<ScheduledTask[]>([])
  const loading = ref(false)

  async function load() {
    loading.value = true
    try {
      tasks.value = await listTasks()
    } catch (e) {
      console.warn('加载定时任务失败', e)
      tasks.value = []
    } finally {
      loading.value = false
    }
  }

  async function create(payload: TaskPayload): Promise<ScheduledTask> {
    const task = await createTask(payload)
    tasks.value.unshift(task)
    return task
  }

  async function update(id: number, payload: Partial<TaskPayload>): Promise<ScheduledTask> {
    const task = await updateTask(id, payload)
    const idx = tasks.value.findIndex(t => t.id === id)
    if (idx !== -1) tasks.value[idx] = task
    return task
  }

  async function toggle(id: number): Promise<ScheduledTask> {
    const task = await toggleTask(id)
    const idx = tasks.value.findIndex(t => t.id === id)
    if (idx !== -1) tasks.value[idx] = task
    return task
  }

  async function remove(id: number) {
    await deleteTask(id)
    tasks.value = tasks.value.filter(t => t.id !== id)
  }

  async function run(id: number) {
    await runNow(id)
  }

  return { tasks, loading, load, create, update, toggle, remove, run }
})
