<template>
  <n-drawer :show="show" :width="420" placement="right" @update:show="emit('update:show', $event)">
    <n-drawer-content title=" 使用统计" closable>
      <div v-if="loading" style="text-align:center;padding:40px"><n-spin size="large" /></div>
      <div v-else class="stats-body">
        <!-- 概览卡片 -->
        <n-grid cols="2" :x-gap="8" :y-gap="8">
          <n-gi><n-card size="small" title="对话"><div class="stat-num">{{ data?.chats || 0 }}</div></n-card></n-gi>
          <n-gi><n-card size="small" title="消息"><div class="stat-num">{{ data?.messages || 0 }}</div></n-card></n-gi>
          <n-gi><n-card size="small" title="工具调用"><div class="stat-num">{{ data?.tool_calls?.total || 0 }}</div></n-card></n-gi>
          <n-gi><n-card size="small" title="成功率"><div class="stat-num" :style="{ color: successRate > 90 ? '#22c55e' : '#f59e0b' }">{{ successRate }}%</div></n-card></n-gi>
        </n-grid>

        <!-- Token 消耗 -->
        <n-card size="small" title=" Token 消耗" style="margin-top:12px">
          <div v-if="!data?.token_usage?.request_count" class="empty">暂无数据</div>
          <div v-else>
            <n-grid cols="2" :x-gap="8" :y-gap="8">
              <n-gi><div class="token-card">
                <div class="token-num">{{ fmtTokens(data.token_usage.total_tokens) }}</div>
                <div class="token-label">总 Token</div>
              </div></n-gi>
              <n-gi><div class="token-card">
                <div class="token-num">{{ fmtTokens(data.token_usage.prompt_tokens) }}</div>
                <div class="token-label">输入 Token</div>
              </div></n-gi>
              <n-gi><div class="token-card">
                <div class="token-num">{{ fmtTokens(data.token_usage.completion_tokens) }}</div>
                <div class="token-label">输出 Token</div>
              </div></n-gi>
              <n-gi><div class="token-card">
                <div class="token-num">{{ data.token_usage.request_count }}</div>
                <div class="token-label">请求次数</div>
              </div></n-gi>
            </n-grid>

            <!-- 按模型拆分 -->
            <div v-if="data.token_usage.by_model?.length" style="margin-top:12px">
              <n-text depth="3" style="font-size:12px">按模型消耗</n-text>
              <div v-for="m in data.token_usage.by_model" :key="m.model" class="model-row">
                <span class="model-name">{{ m.model }}</span>
                <span class="model-tokens">{{ fmtTokens(m.total_tokens) }}</span>
                <span class="model-count">{{ m.count }} 次</span>
              </div>
            </div>
          </div>
        </n-card>

        <!-- TOP 工具 -->
        <n-card size="small" title=" 最常用工具" style="margin-top:12px">
          <div v-if="!data?.tool_top?.length" class="empty">暂无数据</div>
          <div v-for="(t, i) in data?.tool_top || []" :key="t.name" class="tool-row">
            <span class="tool-rank">{{ i + 1 }}</span>
            <n-tag size="small">{{ t.name }}</n-tag>
            <span class="tool-count">{{ t.count }} 次</span>
          </div>
        </n-card>
      </div>
    </n-drawer-content>
  </n-drawer>
</template>

<script setup lang="ts">
import { ref, computed, watch } from 'vue'
import { NDrawer, NDrawerContent, NGrid, NGi, NCard, NTag, NSpin, NText } from 'naive-ui'
import { apiFetch } from '@/api/client'

const props = defineProps<{ show: boolean }>()
const emit = defineEmits<{ 'update:show': [v: boolean] }>()

interface TokenUsage {
  request_count: number
  prompt_tokens: number
  completion_tokens: number
  total_tokens: number
  by_model: { model: string; count: number; total_tokens: number }[]
}

interface StatsData {
  chats: number; messages: number
  tool_calls: { total: number; success: number; error: number }
  tool_top: { name: string; count: number }[]
  token_usage: TokenUsage
}

const data = ref<StatsData | null>(null)
const loading = ref(false)

const successRate = computed(() => {
  const tc = data.value?.tool_calls
  if (!tc || tc.total === 0) return 100
  return Math.round((tc.success / tc.total) * 100)
})

function fmtTokens(n: number | undefined): string {
  if (n == null) return '0'
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(1) + 'M'
  if (n >= 1_000) return (n / 1_000).toFixed(1) + 'K'
  return String(n)
}

watch(() => props.show, async (v) => {
  if (v) {
    loading.value = true
    try {
      const resp = await apiFetch('/api/tool-calls/stats')
      data.value = await resp.json()
    } catch { data.value = null }
    loading.value = false
  }
})
</script>

<style scoped>
.stats-body { padding-bottom: 40px; }
.stat-num { font-size: 28px; font-weight: 700; }
.empty { color: var(--text-secondary); font-size: 13px; text-align: center; padding: 16px; }
.tool-row { display: flex; align-items: center; gap: 8px; padding: 4px 0; }
.tool-rank { width: 20px; font-weight: 600; color: var(--accent); }
.tool-count { margin-left: auto; font-size: 13px; color: var(--text-secondary); }

.token-card {
  text-align: center; padding: 8px 4px;
  background: var(--action-color); border-radius: 6px;
}
.token-num { font-size: 20px; font-weight: 700; }
.token-label { font-size: 11px; color: var(--text-secondary); margin-top: 2px; }

.model-row { display: flex; align-items: center; gap: 8px; padding: 3px 0; font-size: 13px; }
.model-name { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.model-tokens { font-weight: 600; color: var(--accent); }
.model-count { color: var(--text-secondary); font-size: 12px; }
</style>
