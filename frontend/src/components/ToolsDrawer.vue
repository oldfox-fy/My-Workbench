<template>
  <n-drawer :show="show" :auto-focus="false" @update:show="(val: boolean) => emit('update:show', val)" width="800">
    <n-drawer-content title="工具列表" closable>
      <n-space vertical>
        <n-text depth="3" style="font-size: 0.8rem">
          以下为当前系统已含的所有工具（含本地工具与 MCP 服务提供的工具），共 {{ tools.length }} 个。
        </n-text>

        <n-divider />

        <n-spin :show="loading">
          <n-grid :cols="2" :x-gap="12" :y-gap="12">
            <n-gi v-for="tool in pagedTools" :key="tool.function.name">
              <n-card size="small" hoverable class="item-card">
                <n-space align="center" :size="6">
                  <n-text strong>{{ tool.function.title || tool.function.name }}</n-text>
                  <n-tag size="tiny" round type="info">{{ tool.function.name }}</n-tag>
                </n-space>
                <n-text depth="3" tag="div" class="item-desc">
                  {{ tool.function.description }}
                </n-text>
              </n-card>
            </n-gi>
          </n-grid>
          <div v-if="tools.length > PAGE_SIZE" style="display: flex; justify-content: center; margin-top: 12px;">
            <n-pagination v-model:page="toolPage" :page-size="PAGE_SIZE" :item-count="tools.length" />
          </div>
          <p v-if="!loading && tools.length === 0" style="color: gray; font-size: 0.85rem;">
            暂无可用工具，请检查 MCP 服务或工具配置。
          </p>
        </n-spin>

        <!-- ========== 技能列表 ========== -->
        <n-divider />
        <n-text depth="3" style="font-size: 0.8rem">
          已注册的技能（共 {{ skillStore.skills.length }} 个），可在「设置 → 技能管理」中维护。
        </n-text>
        <n-grid :cols="2" :x-gap="12" :y-gap="12">
          <n-gi v-for="skill in pagedSkills" :key="skill.id">
            <n-card size="small" hoverable class="item-card">
              <n-space align="center" :size="6">
                <n-text strong>{{ skill.title }}</n-text>
                <n-tag size="tiny" round :type="skill.skill_type === 'code' ? 'warning' : 'info'">
                  {{ skill.skill_type === 'code' ? '代码' : '提示词' }}
                </n-tag>
                <n-tag size="tiny" round :type="skill.enabled ? 'success' : 'default'">
                  {{ skill.enabled ? '已启用' : '已禁用' }}
                </n-tag>
              </n-space>
              <n-text depth="3" tag="div" class="item-desc">
                {{ skill.name }}{{ skill.description ? ' · ' + skill.description : '' }}
              </n-text>
            </n-card>
          </n-gi>
        </n-grid>
        <div v-if="skillStore.skills.length > PAGE_SIZE" style="display: flex; justify-content: center; margin-top: 12px;">
          <n-pagination v-model:page="skillPage" :page-size="PAGE_SIZE" :item-count="skillStore.skills.length" />
        </div>
        <p v-if="skillStore.skills.length === 0" style="color: gray; font-size: 0.85rem;">
          暂无已注册技能，可在「设置 → 技能管理」中注册。
        </p>
      </n-space>
    </n-drawer-content>
  </n-drawer>
</template>

<script setup lang="ts">
import { ref, watch, computed } from 'vue'
import { NDrawer, NDrawerContent, NSpace, NText, NDivider, NGrid, NGi, NCard, NTag, NSpin, NPagination } from 'naive-ui'
import { useSkillStore } from '@/stores/skills'

interface ToolItem {
  function: { name: string; title: string; description: string }
}

const props = defineProps<{ show: boolean }>()
const emit = defineEmits<{ 'update:show': [value: boolean] }>()

// 每行 2 个，每页 2 行 = 每页 4 个
const PAGE_SIZE = 4

const skillStore = useSkillStore()
const tools = ref<ToolItem[]>([])
const loading = ref(false)

const toolPage = ref(1)
const skillPage = ref(1)

const pagedTools = computed(() =>
  tools.value.slice((toolPage.value - 1) * PAGE_SIZE, toolPage.value * PAGE_SIZE)
)
const pagedSkills = computed(() =>
  skillStore.skills.slice((skillPage.value - 1) * PAGE_SIZE, skillPage.value * PAGE_SIZE)
)

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

// 打开抽屉时刷新工具列表与技能列表，并重置分页
watch(() => props.show, (val) => {
  if (val) {
    toolPage.value = 1
    skillPage.value = 1
    loadTools()
    skillStore.loadSkills()
  }
})
</script>

<style scoped>
.item-card {
  height: 100%;
}
.item-desc {
  font-size: 0.78rem;
  margin-top: 4px;
  word-break: break-word;
  white-space: pre-wrap;
}
</style>
