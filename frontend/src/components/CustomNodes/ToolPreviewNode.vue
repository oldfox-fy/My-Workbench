<template>
  <div class="toolpreview-block" :class="{ 'streaming': isLoading }">
    <div class="toolpreview-summary no-select">
      <span class="summary-icon">
        <m-svg name="tools" />
      </span>
      <span class="summary-text">{{ title }}</span>
      <span class="loading-dots" v-if="isLoading">
        <span></span><span></span><span></span>
      </span>
    </div>
    
    <div class="toolpreview-list" v-if="tools.length > 0">
      <div 
        v-for="tool in tools" 
        :key="tool.call_id"
        class="preview-item"
        :class="{ 'streaming': tool.streaming }"
      >
        <span class="item-icon">
          <m-svg name="tool" />
        </span>
        <span class="item-name">{{ tool.name }}</span>
        <span class="item-status" v-if="tool.streaming">收集中…</span>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'

interface PreviewTool {
  call_id: string
  name: string
  streaming: boolean
}

interface PreviewData {
  tools: PreviewTool[]
  count: number
  loading: boolean
}

const props = defineProps<{
  node: {
    type: 'toolpreview'
    content?: string
    loading?: boolean
    attrs?: Record<string, any>
  }
  customId?: string
  isDark?: boolean
}>()

const data = computed<PreviewData>(() => {
  try {
    const content = props.node.content || '{}'
    return JSON.parse(content)
  } catch {
    return { tools: [], count: 0, loading: false }
  }
})

const tools = computed(() => data.value.tools || [])
const isLoading = computed(() => data.value.loading || props.node.loading || false)

const title = computed(() => {
  if (isLoading.value) {
    return tools.value.length > 0 ? `工具调用中…` : '工具调用中…'
  }
  return '工具调用预览'
})
</script>

<style scoped>
.toolpreview-block {
  border: 1px solid var(--border-color);
  border-radius: 8px;
  background: var(--bg-secondary);
  overflow: hidden;
  margin: 8px 0;
}

.toolpreview-block.streaming {
  border-left: 3px solid var(--primary-color, #1890ff);
  animation: pulse 2s infinite;
}

@keyframes pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.7; }
}

.toolpreview-summary {
  display: flex;
  align-items: center;
  padding: 12px 16px;
  gap: 10px;
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

.loading-dots {
  display: flex;
  gap: 4px;
}

.loading-dots span {
  width: 6px;
  height: 6px;
  background: var(--primary-color, #3b82f6);
  border-radius: 50%;
  animation: bounce 1.4s infinite ease-in-out both;
}

.loading-dots span:nth-child(1) { animation-delay: -0.32s; }
.loading-dots span:nth-child(2) { animation-delay: -0.16s; }

@keyframes bounce {
  0%, 80%, 100% { transform: scale(0); }
  40% { transform: scale(1); }
}

.toolpreview-list {
  border-top: 1px solid var(--border-color);
  padding: 8px;
}

.preview-item {
  display: flex;
  align-items: center;
  padding: 8px 12px;
  border-radius: 6px;
  gap: 10px;
}

.preview-item.streaming {
  background: rgba(59, 130, 246, 0.04);
}

.item-icon {
  width: 16px;
  height: 16px;
  color: #3b82f6;
  flex-shrink: 0;
}

.item-name {
  flex: 1;
  font-size: 14px;
  color: var(--text-primary);
}

.item-status {
  font-size: 12px;
  color: var(--primary-color, #3b82f6);
  font-style: italic;
}
</style>