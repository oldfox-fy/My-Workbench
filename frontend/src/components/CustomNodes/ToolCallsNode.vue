<template>
  <div class="toolcalls-block" :class="{ 'streaming': isLoading }">
    <div class="toolcalls-summary no-select" @click="toggleExpand">
      <span class="summary-icon">
        <m-svg name="tools" />
      </span>
      <span class="summary-text">{{ title }}</span>
      <span class="summary-count" v-if="callIds.length > 0">
        {{ callIds.length }}个
      </span>
      <span class="expand-icon" :class="{ 'expanded': expanded }">
        <m-svg name="chevron-down" />
      </span>
    </div>
    
    <div v-show="expanded" class="toolcalls-list">
      <div 
        v-for="callId in callIds" 
        :key="callId"
        class="toolcall-item"
        @click.stop="openDetail(callId)"
      >
        <span class="item-status" :class="getStatusClass(callId)">
          <m-svg :name="getStatusIcon(callId)" />
        </span>
        <span class="item-name">{{ getToolName(callId) }}</span>
        <span class="item-arrow">
          <m-svg name="chevron-right" />
        </span>
      </div>
    </div>
  </div>

  <ToolCallDetail
    v-model:visible="detailVisible"
    :call-id="selectedCallId"
  />
</template>

<script setup lang="ts">
import { ref, computed } from 'vue'
import MSvg from '@/components/MSvg.vue'
import ToolCallDetail from './ToolCallDetail.vue'

const props = defineProps<{
  node: {
    type: 'toolcalls'
    content?: string
    loading?: boolean
    attrs?: Record<string, any>
  }
  customId?: string
  isDark?: boolean
}>()

const expanded = ref(false)
const detailVisible = ref(false)
const selectedCallId = ref('')

const callIds = computed<string[]>(() => {
  try {
    const content = props.node.content || '{}'
    const parsed = JSON.parse(content)
    return parsed.call_ids || []
  } catch {
    return []
  }
})

const isLoading = computed(() => props.node.loading || false)

const title = computed(() => {
  if (isLoading.value) {
    return callIds.value.length > 0 ? '工具调用中…' : '工具调用中…'
  }
  return '工具调用'
})

function toggleExpand() {
  expanded.value = !expanded.value
}

function openDetail(callId: string) {
  selectedCallId.value = callId
  detailVisible.value = true
}

// 状态可以从全局 store 获取，这里简化处理
function getStatusClass(callId: string): string {
  return 'status-success'
}

function getStatusIcon(callId: string): string {
  return 'check'
}

function getToolName(callId: string): string {
  return `工具 #${callId.slice(-6)}`
}
</script>

<style scoped>
.toolcalls-block {
  border: 1px solid var(--border-color);
  border-radius: 8px;
  background: var(--bg-secondary);
  overflow: hidden;
  margin: 8px 0;
}

.toolcalls-block.streaming {
  border-left: 3px solid var(--primary-color, #1890ff);
  animation: pulse 2s infinite;
}

@keyframes pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.7; }
}

.toolcalls-summary {
  display: flex;
  align-items: center;
  padding: 12px 16px;
  cursor: pointer;
  user-select: none;
  gap: 10px;
}

.toolcalls-summary:hover {
  background: rgba(99, 102, 241, 0.05);
}

.summary-icon {
  width: 20px;
  height: 20px;
  color: var(--primary-color, #3b82f6);
  flex-shrink: 0;
}

.summary-text {
  font-weight: 600;
  font-size: 15px;
  color: var(--text-primary);
  flex: 1;
}

.summary-count {
  font-size: 13px;
  color: var(--text-secondary);
  background: var(--bg-tertiary);
  padding: 2px 8px;
  border-radius: 12px;
}

.expand-icon {
  width: 16px;
  height: 16px;
  color: var(--text-secondary);
  transition: transform 0.3s ease;
  flex-shrink: 0;
}

.expand-icon.expanded {
  transform: rotate(180deg);
}

.toolcalls-list {
  border-top: 1px solid var(--border-color);
  padding: 8px;
}

.toolcall-item {
  display: flex;
  align-items: center;
  padding: 10px 12px;
  border-radius: 6px;
  cursor: pointer;
  gap: 10px;
  transition: background 0.2s;
}

.toolcall-item:hover {
  background: rgba(99, 102, 241, 0.08);
}

.item-status {
  width: 18px;
  height: 18px;
  flex-shrink: 0;
}

.item-status.status-success { color: #52c41a; }
.item-status.status-error { color: #ff4d4f; }
.item-status.status-calling { 
  color: var(--primary-color, #1890ff);
  animation: spin 1s linear infinite;
}

@keyframes spin {
  from { transform: rotate(0deg); }
  to { transform: rotate(360deg); }
}

.item-name {
  flex: 1;
  font-size: 14px;
  color: var(--text-primary);
}

.item-arrow {
  width: 16px;
  height: 16px;
  color: var(--text-secondary);
  flex-shrink: 0;
}
</style>