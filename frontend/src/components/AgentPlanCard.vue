<template>
  <div class="plan-card" :class="{ 'completed': isCompleted }">
    <div class="plan-header no-select" @click="collapsed = !collapsed">
      <span class="plan-icon">{{ isCompleted ? '✅' : '📋' }}</span>
      <span class="plan-title">任务计划</span>
      <span class="plan-summary">{{ summary }}</span>
      <span class="plan-toggle">{{ collapsed ? '▶' : '▼' }}</span>
    </div>
    <div v-if="!collapsed" class="plan-steps">
      <div
        v-for="step in steps"
        :key="step.id"
        class="plan-step"
        :class="'step-' + step.status"
      >
        <span class="step-icon">{{ statusIcon(step.status) }}</span>
        <span class="step-title">{{ step.title }}</span>
        <span class="step-status-text">{{ statusText(step.status) }}</span>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed } from 'vue'

export interface PlanStep {
  id: string
  title: string
  status: 'pending' | 'in_progress' | 'completed' | 'cancelled'
}

const props = defineProps<{
  tasks?: PlanStep[]
  summary?: string
}>()

const collapsed = ref(false)

const steps = computed(() => props.tasks || [])
const summary = computed(() => props.summary || '')

const isCompleted = computed(() => {
  const s = steps.value
  return s.length > 0 && s.every(t => t.status === 'completed')
})

function statusIcon(status: string): string {
  const icons: Record<string, string> = {
    pending: '⬜',
    in_progress: '🔄',
    completed: '✅',
    cancelled: '❌',
  }
  return icons[status] || '⬜'
}

function statusText(status: string): string {
  const texts: Record<string, string> = {
    pending: '待执行',
    in_progress: '执行中',
    completed: '已完成',
    cancelled: '已取消',
  }
  return texts[status] || status
}
</script>

<style scoped>
.plan-card {
  border: 1px solid var(--border-color);
  border-radius: 8px;
  background: var(--bg-secondary);
  overflow: hidden;
  margin: 8px 0;
}
.plan-card.completed {
  opacity: 0.7;
}
.plan-header {
  display: flex;
  align-items: center;
  padding: 10px 14px;
  cursor: pointer;
  user-select: none;
  gap: 8px;
  transition: background 0.2s;
}
.plan-header:hover {
  background: rgba(99, 102, 241, 0.05);
}
.plan-icon { font-size: 16px; }
.plan-title { font-weight: 600; color: var(--text-primary); font-size: 14px; }
.plan-summary { flex: 1; font-size: 13px; color: var(--text-secondary); }
.plan-toggle { font-size: 12px; color: var(--text-tertiary); }
.plan-steps {
  border-top: 1px solid var(--border-color);
  padding: 6px 0;
}
.plan-step {
  display: flex;
  align-items: center;
  padding: 6px 14px;
  gap: 8px;
  font-size: 13px;
  transition: background 0.15s;
}
.plan-step:hover {
  background: var(--bg-hover);
}
.plan-step.step-completed {
  color: var(--text-secondary);
}
.plan-step.step-in_progress {
  color: var(--primary-color, #6366f1);
  font-weight: 500;
}
.step-icon { width: 20px; text-align: center; }
.step-title { flex: 1; }
.step-status-text {
  font-size: 11px;
  padding: 1px 8px;
  border-radius: 10px;
  background: var(--bg-tertiary);
  color: var(--text-secondary);
}
.step-in_progress .step-status-text {
  background: rgba(99, 102, 241, 0.12);
  color: var(--primary-color, #6366f1);
}
.step-completed .step-status-text {
  background: rgba(34, 197, 94, 0.12);
  color: #22c55e;
}
</style>
