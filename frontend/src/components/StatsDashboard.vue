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

        <!-- TOP 工具 -->
        <n-card size="small" title=" 最常用工具" style="margin-top:12px">
          <div v-if="!data?.tool_top?.length" class="empty">暂无数据</div>
          <div v-for="(t, i) in data?.tool_top || []" :key="t.name" class="tool-row">
            <span class="tool-rank">{{ i + 1 }}</span>
            <n-tag size="small">{{ t.name }}</n-tag>
            <span class="tool-count">{{ t.count }} 次</span>
          </div>
        </n-card>

        <!-- 每日趋势 -->
        <n-card size="small" title=" 30 天趋势" style="margin-top:12px">
          <div v-if="!data?.daily_trend?.length" class="empty">暂无数据</div>
          <div class="trend-chart">
            <div v-for="d in data?.daily_trend || []" :key="d.date" class="trend-bar-wrap" :title="`${d.date}: ${d.count}次`">
              <div class="trend-bar" :style="{ height: trendHeight(d.count) + 'px' }"></div>
            </div>
          </div>
        </n-card>
      </div>
    </n-drawer-content>
  </n-drawer>
</template>

<script setup lang="ts">
import { ref, computed, watch } from 'vue'
import { NDrawer, NDrawerContent, NGrid, NGi, NCard, NTag, NSpin } from 'naive-ui'

const props = defineProps<{ show: boolean }>()
const emit = defineEmits<{ 'update:show': [v: boolean] }>()

interface StatsData {
  chats: number; messages: number
  tool_calls: { total: number; success: number; error: number }
  tool_top: { name: string; count: number }[]
  daily_trend: { date: string; count: number }[]
}

const data = ref<StatsData | null>(null)
const loading = ref(false)
const maxTrend = ref(1)

const successRate = computed(() => {
  const tc = data.value?.tool_calls
  if (!tc || tc.total === 0) return 100
  return Math.round((tc.success / tc.total) * 100)
})

function trendHeight(count: number) {
  return Math.max(2, Math.round((count / maxTrend.value) * 80))
}

watch(() => props.show, async (v) => {
  if (v) {
    loading.value = true
    try {
      const resp = await fetch('/api/tool-calls/stats')
      data.value = await resp.json()
      maxTrend.value = Math.max(1, ...(data.value?.daily_trend || []).map(d => d.count))
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
.trend-chart { display: flex; align-items: flex-end; gap: 2px; height: 90px; padding-top: 8px; }
.trend-bar-wrap { flex: 1; display: flex; align-items: flex-end; height: 100%; }
.trend-bar { width: 100%; background: var(--accent); border-radius: 2px 2px 0 0; transition: height .3s; min-width: 3px; }
</style>
