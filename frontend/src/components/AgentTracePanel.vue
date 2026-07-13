<template>
  <n-drawer :show="show" :width="520" placement="right" @update:show="emit('update:show', $event)">
    <n-drawer-content title="📊 Agent 执行追踪" closable>
      <div v-if="loading" style="text-align:center;padding:40px"><n-spin size="large" /></div>
      <div v-else-if="error" class="trace-error">{{ error }}</div>
      <div v-else-if="!trace" class="trace-empty">暂无追踪数据</div>
      <div v-else class="trace-body">
        <!-- 概览 -->
        <n-grid cols="3" :x-gap="8" :y-gap="8" style="margin-bottom:12px">
          <n-gi><n-card size="small" title="总耗时"><div class="stat-num">{{ trace.total_time_ms }}ms</div></n-card></n-gi>
          <n-gi><n-card size="small" title="LLM 步骤"><div class="stat-num">{{ trace.total_steps }}</div></n-card></n-gi>
          <n-gi><n-card size="small" title="工具调用"><div class="stat-num">{{ trace.total_tool_calls }}</div></n-card></n-gi>
        </n-grid>

        <!-- 瀑布图 -->
        <n-card size="small" title="⏱ 执行时间线" style="margin-bottom:12px">
          <div class="waterfall">
            <div v-for="span in sortedSpans" :key="span.id" class="wf-row" @click="toggleSpan(span)">
              <span class="wf-icon">{{ typeIcon(span.span_type) }}</span>
              <span class="wf-name" :style="{ paddingLeft: (indentLevel(span) * 16) + 'px' }">{{ span.name }}</span>
              <span class="wf-bar-wrap">
                <span class="wf-bar" :class="'bar-' + span.status"
                  :style="{ width: barWidth(span) + '%', background: typeColor(span.span_type) }"
                ></span>
              </span>
              <span class="wf-dur">{{ span.duration_ms }}ms</span>
            </div>
          </div>
        </n-card>

        <!-- 选中 Span 详情 -->
        <n-card v-if="selected" size="small" title="详情">
          <div class="detail-row"><span class="dl">名称</span><span>{{ selected.name }}</span></div>
          <div class="detail-row"><span class="dl">类型</span><n-tag size="small">{{ selected.span_type }}</n-tag></div>
          <div class="detail-row"><span class="dl">状态</span><n-tag size="small" :type="selected.status === 'success' ? 'success' : 'error'">{{ selected.status }}</n-tag></div>
          <div class="detail-row"><span class="dl">耗时</span><span>{{ selected.duration_ms }}ms</span></div>
          <div v-if="selected.input_preview" class="detail-row">
            <span class="dl">输入</span>
            <pre class="detail-pre">{{ selected.input_preview }}</pre>
          </div>
          <div v-if="selected.output_preview" class="detail-row">
            <span class="dl">输出</span>
            <pre class="detail-pre">{{ selected.output_preview }}</pre>
          </div>
          <div v-if="selected.error_message" class="detail-row">
            <span class="dl">错误</span>
            <span class="error-msg">{{ selected.error_message }}</span>
          </div>
        </n-card>
      </div>
    </n-drawer-content>
  </n-drawer>
</template>

<script setup lang="ts">
import { ref, computed, watch } from 'vue'
import { NDrawer, NDrawerContent, NGrid, NGi, NCard, NTag, NSpin } from 'naive-ui'

const props = defineProps<{ show: boolean; messageId: number | null }>()
const emit = defineEmits<{ 'update:show': [v: boolean] }>()

interface SpanData {
  id: string; parent_span_id: string | null; span_type: string
  name: string; status: string; duration_ms: number
  input_preview: string; output_preview: string; error_message: string | null
}
interface TraceData {
  trace_id: string; status: string; total_steps: number
  total_tool_calls: number; total_time_ms: number; spans: SpanData[]
}

const trace = ref<TraceData | null>(null)
const loading = ref(false)
const error = ref('')
const selected = ref<SpanData | null>(null)

const sortedSpans = computed(() => {
  if (!trace.value) return []
  return [...trace.value.spans].sort((a, b) => a.duration_ms - b.duration_ms || 0)
})

const maxDuration = computed(() => {
  const s = sortedSpans.value
  if (!s.length) return 1
  return Math.max(...s.map(x => x.duration_ms || 0), 1)
})

function barWidth(span: SpanData): number {
  return Math.max(1, Math.round(((span.duration_ms || 0) / maxDuration.value) * 100))
}

function indentLevel(span: SpanData): number {
  return span.parent_span_id ? 1 : 0
}

function typeIcon(t: string): string {
  const icons: Record<string, string> = { trace: '🔍', step: '🔹', tool_call: '🔧', sub_agent: '🤖', approval: '⏳' }
  return icons[t] || '•'
}

function typeColor(t: string): string {
  const colors: Record<string, string> = { trace: '#6366f1', step: '#3b82f6', tool_call: '#22c55e', sub_agent: '#8b5cf6', approval: '#f59e0b' }
  return colors[t] || '#888'
}

function toggleSpan(span: SpanData) {
  selected.value = selected.value?.id === span.id ? null : span
}

watch(() => [props.show, props.messageId], async () => {
  if (props.show && props.messageId) {
    loading.value = true
    error.value = ''
    selected.value = null
    try {
      const resp = await fetch(`/api/tool-calls/message/${props.messageId}/trace`)
      if (resp.ok) trace.value = await resp.json()
      else if (resp.status === 404) error.value = '该消息未找到追踪数据'
      else error.value = '加载失败'
    } catch { error.value = '网络错误' }
    loading.value = false
  }
})
</script>

<style scoped>
.trace-body { padding-bottom: 40px; }
.trace-error { text-align: center; padding: 40px; color: #ff4d4f; }
.trace-empty { text-align: center; padding: 40px; color: var(--text-secondary); }
.stat-num { font-size: 24px; font-weight: 700; color: var(--primary-color); }

.waterfall { padding: 4px 0; }
.wf-row {
  display: flex; align-items: center; padding: 4px 8px; gap: 6px;
  cursor: pointer; border-radius: 4px; transition: background 0.15s;
  font-size: 13px;
}
.wf-row:hover { background: var(--bg-hover); }
.wf-icon { width: 20px; text-align: center; flex-shrink: 0; }
.wf-name { flex: 0 0 140px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.wf-bar-wrap { flex: 1; height: 8px; background: var(--bg-tertiary); border-radius: 4px; overflow: hidden; }
.wf-bar { height: 100%; border-radius: 4px; min-width: 2px; transition: width 0.3s; }
.wf-dur { flex: 0 0 50px; text-align: right; font-size: 12px; color: var(--text-secondary); }
.bar-success { opacity: 0.9; }
.bar-error { opacity: 0.6; }

.detail-row { margin-bottom: 8px; font-size: 13px; display: flex; gap: 8px; align-items: flex-start; }
.dl { font-weight: 600; color: var(--text-secondary); min-width: 50px; }
.detail-pre { font-size: 12px; background: var(--bg-secondary); padding: 6px 8px; border-radius: 4px; max-height: 200px; overflow: auto; white-space: pre-wrap; word-break: break-all; }
.error-msg { color: #ff4d4f; font-size: 12px; }
</style>
