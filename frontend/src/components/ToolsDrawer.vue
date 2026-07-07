<template>
  <n-drawer :show="show" :auto-focus="false" @update:show="(val: boolean) => emit('update:show', val)" width="400">
    <n-drawer-content title="工具列表" closable>
      <n-space vertical>
        <n-text depth="3" style="font-size: 0.8rem">
          以下为当前系统已含的所有工具（含本地工具与 MCP 服务提供的工具），共 {{ tools.length }} 个。
        </n-text>

        <n-divider />

        <n-spin :show="loading">
          <n-list hoverable>
            <n-list-item v-for="tool in tools" :key="tool.function.name">
              <div>
                <n-space align="center" :size="6">
                  <n-text strong>{{ tool.function.title || tool.function.name }}</n-text>
                  <n-tag size="tiny" round type="info">{{ tool.function.name }}</n-tag>
                </n-space>
                <n-text depth="3" tag="div" style="font-size: 0.78rem; margin-top: 4px; word-break: break-word; white-space: pre-wrap;">
                  {{ tool.function.description }}
                </n-text>
              </div>
            </n-list-item>
          </n-list>
          <p v-if="!loading && tools.length === 0" style="color: gray; font-size: 0.85rem;">
            暂无可用工具，请检查 MCP 服务或工具配置。
          </p>
        </n-spin>
      </n-space>
    </n-drawer-content>
  </n-drawer>
</template>

<script setup lang="ts">
import { ref, watch } from 'vue'
import { NDrawer, NDrawerContent, NSpace, NText, NDivider, NList, NListItem, NTag, NSpin } from 'naive-ui'

interface ToolItem {
  function: { name: string; title: string; description: string }
}

const props = defineProps<{ show: boolean }>()
const emit = defineEmits<{ 'update:show': [value: boolean] }>()

const tools = ref<ToolItem[]>([])
const loading = ref(false)

async function loadTools() {
  loading.value = true
  try {
    const res = await fetch('/api/tools')
    const data = await res.json()
    tools.value = data.tools || []
  } catch (e) {
    console.warn('获取工具列表失败', e)
  } finally {
    loading.value = false
  }
}

// 打开抽屉时刷新工具列表
watch(() => props.show, (val) => {
  if (val) loadTools()
})
</script>
